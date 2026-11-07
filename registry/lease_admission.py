"""Atomic registry reservation and the queue behind it — not physical-memory
telemetry and not a job scheduler.

Serialize reaping/checking/inserting on one connection. Publish only after this
transaction commits. A request that cannot be granted now is queued in order
(`wait_s`), and the registry promotes the head of the queue inside the same
transaction that frees the bytes (release, expiry, a reap on any read). Providers
own execution: queueing allocates nothing, a grant starts the TTL clock.
"""
from datetime import datetime, timedelta, timezone
import json
import secrets
import sqlite3

from fastapi import HTTPException

# A legacy shared request over the bytes (no `wait_s`) still queues, as it always
# did — but bounded now: a waiter that never comes back frees its place.
DEFAULT_WAIT_S = 3600
PRIORITY_RANK = {'critical': 0, 'high': 1, 'normal': 2, 'low': 3}
QUEUE_ORDER = ("ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
               "WHEN 'low' THEN 3 ELSE 2 END, created_at, id")


def capacity_of(resource) -> int:
    """Declared bytes of a resource row, or raise the 409 the caller expects."""
    specs = json.loads(resource['specs']) if resource['specs'] else {}
    if not isinstance(specs, dict):
        raise HTTPException(409, {'code': 'invalid_resource_capacity'})
    total = (specs.get('vram_mb') or specs.get('gpu_memory_mb') or
             specs.get('unified_memory_mb') or specs.get('ram_mb') or 0)
    if type(total) is not int or total < 0:
        raise HTTPException(409, {'code': 'invalid_resource_capacity'})
    return total


def valid_claim(row) -> bool:
    """A row whose dimensions can be trusted before they touch the ledger: memory
    null or an int >= 0, requested TTL null or an int >= 0. A legacy or corrupt
    queued row is not more trusted than an active one (codex, PR 100 review)."""
    m = row['memory_mb']
    if m is not None and (type(m) is not int or m < 0):
        return False
    t = row['ttl_s'] if 'ttl_s' in row.keys() else None
    if t is not None and (type(t) is not int or t < 0):
        return False
    return True


def holds_on_expiry(resource) -> bool:
    """The resource's expiry policy. `free` (the default): a holder past its TTL stops
    charging the ledger at once. `hold`: it keeps charging until its holder or an admin
    settles it (DELETE) — the registry cannot see that the memory is free, so on a host
    where two services share one pool it does not pretend to (task 868)."""
    policy = resource['expiry_policy'] if 'expiry_policy' in resource.keys() else 'free'
    return (policy or 'free') == 'hold'


UNSETTLED = ("status='expired' AND expiry_reason='ttl_expired' AND settled_at IS NULL")


def occupying(conn, resource):
    """The rows that charge the resource now: the active ones and, under `hold`, the
    expired-but-unsettled ones."""
    if holds_on_expiry(resource):
        return conn.execute("SELECT * FROM leases WHERE resource_id=? AND (status='active' OR ("
                            + UNSETTLED + "))", (resource['id'],)).fetchall()
    return conn.execute("SELECT * FROM leases WHERE resource_id=? AND status='active'",
                        (resource['id'],)).fetchall()


def unsettled_view(rows) -> list:
    """Expired holders nobody has settled, for the resource view: what blocks a
    `hold` pool, and who has to reconcile it."""
    return [{'lease_id': r['id'], 'service_id': r.get('service_id'), 'agent_id': r.get('agent_id'),
             'memory_mb': r.get('memory_mb'), 'exclusive': bool(r.get('exclusive')),
             'expired_at': r.get('expires_at')}
            for r in (dict(x) for x in rows)
            if r.get('status') == 'expired' and r.get('expiry_reason') == 'ttl_expired'
            and not r.get('settled_at')]


def reap(conn, now: str):
    """Expire active rows past `expires_at` and queued rows past their wait
    deadline. Returns (resource ids touched, the waits that expired)."""
    rows = conn.execute("SELECT id, resource_id, status, service_id, agent_id, memory_mb, "
                        "exclusive FROM leases WHERE status IN ('active','queued') AND "
                        "expires_at IS NOT NULL AND expires_at < ?", (now,)).fetchall()
    expired_waits, expired_holds = [], []
    for r in rows:
        if r['status'] == 'active':
            # `ttl_expired` is the registry giving up on a holder that stopped
            # renewing. It is not proof the holder's processes or memory are gone:
            # a reader sees `expired`, never `released`, and acts on that difference.
            conn.execute("UPDATE leases SET status='expired', expiry_reason='ttl_expired' "
                         "WHERE id=? AND status='active'", (r['id'],))
            expired_holds.append(dict(r))
        else:
            conn.execute("UPDATE leases SET status='expired', expiry_reason='wait_expired', "
                         "queue_position=NULL WHERE id=? AND status='queued'", (r['id'],))
            expired_waits.append(dict(r))
    return {r['resource_id'] for r in rows}, expired_waits, expired_holds


def _renumber(conn, resource_id):
    waiting = conn.execute("SELECT id FROM leases WHERE resource_id=? AND status='queued' "
                           + QUEUE_ORDER, (resource_id,)).fetchall()
    for position, row in enumerate(waiting, start=1):
        conn.execute('UPDATE leases SET queue_position=? WHERE id=?', (position, row['id']))
    return len(waiting)


def promote(conn, resource, now_dt, *, default_ttl, invalid=None):
    """Grant queued rows in order while the head fits. Strict head of line: the
    first waiter that does not fit stops the walk, so a large exclusive waiter is
    never starved by smaller shared ones arriving behind it. A queued row whose
    dimensions cannot be trusted never mints capacity: it is expired with
    `invalid_claim` (appended to `invalid` when a list is given) and the walk goes
    on. Returns the rows promoted (dicts), already active in this transaction."""
    resource_id = resource['id']
    try:
        total = capacity_of(resource)
    except HTTPException:
        return []                       # malformed capacity: nothing is promoted
    now = now_dt.isoformat()
    # What charges the ledger: active rows and, on a `hold` resource, expired rows
    # nobody has settled — an expired holder is not proof its memory is free.
    active = occupying(conn, resource)
    if any(type(r['memory_mb']) is not int or r['memory_mb'] < 0
           for r in active if r['memory_mb'] is not None):
        return []
    exclusive_active = any(r['exclusive'] for r in active)
    available = total - sum(r['memory_mb'] or 0 for r in active)
    waiting = conn.execute("SELECT * FROM leases WHERE resource_id=? AND status='queued' "
                           + QUEUE_ORDER, (resource_id,)).fetchall()
    promoted = []
    dropped = 0
    for q in waiting:
        if not valid_claim(q):
            conn.execute("UPDATE leases SET status='expired', expiry_reason='invalid_claim', "
                         "queue_position=NULL WHERE id=? AND status='queued'", (q['id'],))
            dropped += 1
            if invalid is not None:
                invalid.append(dict(q))
            continue
        exclusive = bool(q['exclusive']) or resource['allocation_mode'] == 'exclusive'
        if exclusive_active:
            break
        if exclusive and (active or promoted):
            break
        if q['memory_mb'] and q['memory_mb'] > available:
            break
        ttl = q['ttl_s']
        if exclusive and not ttl:
            ttl = default_ttl
        expires = (now_dt + timedelta(seconds=ttl)).isoformat() if ttl else None
        conn.execute("UPDATE leases SET status='active', granted_at=?, expires_at=?, "
                     "queue_position=NULL, exclusive=? WHERE id=? AND status='queued'",
                     (now, expires, int(exclusive), q['id']))
        row = dict(q)
        row.update(status='active', granted_at=now, expires_at=expires, exclusive=int(exclusive),
                   waited_s=int((now_dt - _parse(q['created_at'])).total_seconds()))
        promoted.append(row)
        available -= q['memory_mb'] or 0
        if exclusive:
            exclusive_active = True
    if promoted or dropped:
        _renumber(conn, resource_id)
    return promoted


def _parse(ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def queue_view(rows) -> list:
    """The waiting rows of one resource with live positions, for readers."""
    waiting = [dict(r) for r in rows if r['status'] == 'queued']
    waiting.sort(key=lambda r: (PRIORITY_RANK.get(r.get('priority') or 'normal', 2),
                                r.get('created_at') or '', r['id']))
    return [{'lease_id': r['id'], 'position': i, 'service_id': r.get('service_id'),
             'agent_id': r.get('agent_id'), 'memory_mb': r.get('memory_mb'),
             'exclusive': bool(r.get('exclusive')), 'priority': r.get('priority'),
             'queued_at': r.get('created_at'), 'wait_until': r.get('expires_at')}
            for i, r in enumerate(waiting, start=1)]


def reap_and_promote(db, *, default_ttl):
    """The reaper every read path runs: expire what is past, then grant the
    waiters that now fit, in one write transaction. Returns what to publish."""
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        now_dt = datetime.now(timezone.utc)
        touched, expired_waits, expired_holds = reap(conn, now_dt.isoformat())
        if expired_waits:
            for rid in {w['resource_id'] for w in expired_waits}:
                _renumber(conn, rid)
        promoted, invalid = [], []
        for rid in touched:
            resource = conn.execute('SELECT * FROM resources WHERE id=?', (rid,)).fetchone()
            if resource is not None:
                promoted.extend(promote(conn, resource, now_dt, default_ttl=default_ttl,
                                        invalid=invalid))
    return {'promoted': promoted, 'wait_expired': expired_waits, 'expired': expired_holds,
            'invalid': invalid}


def release(db, lease, *, default_ttl):
    """Release a holder, cancel a waiter, or settle an expired holder — then promote
    the waiters that now fit, all in one transaction.

    The reap runs first, inside the same transaction: a waiter whose deadline passed
    is expired, never resurrected by a release that happens to free its bytes, and
    a holder past its TTL is marked before the ledger is read (codex, PR 100
    review). On a `hold` resource an expired holder keeps charging until this call
    settles it; the settlement is the explicit, authorized act that frees the bytes.
    """
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        touched, expired_waits, expired_holds = reap(conn, now)
        for rid in {w['resource_id'] for w in expired_waits}:
            _renumber(conn, rid)
        cur = conn.execute("SELECT status, expiry_reason, settled_at FROM leases WHERE id=?",
                           (lease['id'],)).fetchone()
        status = cur['status'] if cur is not None else lease.get('status')
        # A holder releases; a waiter cancels; an expired holder is settled. Three
        # words for three facts a reader of the row needs to tell apart.
        changed = 0
        if status == 'queued':
            final = 'cancelled'
            changed = conn.execute("UPDATE leases SET status='cancelled', released_at=?, "
                                   "queue_position=NULL WHERE id=? AND status='queued'",
                                   (now, lease['id'])).rowcount
            _renumber(conn, lease['resource_id'])
        elif status == 'active':
            final = 'released'
            changed = conn.execute("UPDATE leases SET status='released', released_at=? "
                                   "WHERE id=? AND status='active'", (now, lease['id'])).rowcount
        elif (status == 'expired' and cur is not None and cur['expiry_reason'] == 'ttl_expired'
              and not cur['settled_at']):
            final = 'settled'
            changed = conn.execute("UPDATE leases SET settled_at=?, released_at=? WHERE id=? "
                                   "AND status='expired' AND settled_at IS NULL",
                                   (now, now, lease['id'])).rowcount
        else:
            final = status or 'released'        # already over: idempotent, nothing moves
        resource = conn.execute('SELECT * FROM resources WHERE id=?',
                                (lease['resource_id'],)).fetchone()
        promoted, invalid = [], []
        if resource is not None:
            promoted = promote(conn, resource, now_dt, default_ttl=default_ttl, invalid=invalid)
    return {'released': bool(changed), 'status': final, 'promoted': promoted,
            'wait_expired': expired_waits, 'expired': expired_holds, 'invalid': invalid}


def reserve(db, resource_id, req, agent_id, *, default_ttl, total_floor, total_hard):
    """Grant now, queue in order, or refuse — one write transaction.

    Returns (body, metadata). `metadata['promoted']` names the waiters this call's
    reap granted first; the caller publishes them after commit.
    """
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        # The clock reads inside the serialized section, so created_at orders the
        # queue the way the writers were serialized: positions handed back are
        # unique and never reshuffle under a racer that read the clock earlier.
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        resource = conn.execute('SELECT * FROM resources WHERE id=?', (resource_id,)).fetchone()
        if resource is None:
            raise HTTPException(404, f'Resource {resource_id} not found')
        # #492: reap expired rows before checking the next reservation. The
        # waiters that now fit are granted first — they were here before us.
        expired_holds = conn.execute(
            "SELECT id, resource_id, service_id, agent_id, memory_mb, exclusive FROM leases "
            "WHERE resource_id=? AND status='active' AND expires_at IS NOT NULL AND expires_at < ?",
            (resource_id, now)).fetchall()
        for h in expired_holds:
            conn.execute("UPDATE leases SET status='expired', expiry_reason='ttl_expired' "
                         "WHERE id=?", (h['id'],))
        expired_waits = conn.execute(
            "SELECT id, resource_id, service_id, agent_id FROM leases WHERE resource_id=? AND "
            "status='queued' AND expires_at IS NOT NULL AND expires_at < ?",
            (resource_id, now)).fetchall()
        for w in expired_waits:
            conn.execute("UPDATE leases SET status='expired', expiry_reason='wait_expired', "
                         "queue_position=NULL WHERE id=?", (w['id'],))
        invalid = []
        promoted = promote(conn, resource, now_dt, default_ttl=default_ttl, invalid=invalid)
        metadata = {'promoted': promoted, 'wait_expired': [dict(w) for w in expired_waits],
                    'expired': [dict(h) for h in expired_holds], 'invalid': invalid}
        # #849: an unknown service is a named 422, not an INSERT-time FK 500.
        if req.service_id and not conn.execute('SELECT 1 FROM services WHERE id=?',
                                                (req.service_id,)).fetchone():
            raise HTTPException(422, f"service_id '{req.service_id}' is not a registered "
                                 'service; omit it, or use an id from GET /api/v1/services')
        # Active rows and, on a `hold` resource, expired holders nobody has settled.
        active = occupying(conn, resource)
        if any(type(r['memory_mb']) is not int or r['memory_mb'] < 0
               for r in active if r['memory_mb'] is not None):
            raise HTTPException(409, {'code': 'invalid_existing_allocation'})
        total = capacity_of(resource)
        # #806: resource-declared exclusivity applies even if not requested.
        exclusive = bool(req.exclusive) or resource['allocation_mode'] == 'exclusive'
        if req.memory_mb and req.memory_mb > total:
            # Never queued: it could never be granted, and at the head of the
            # queue it would block everyone behind it until its wait ran out.
            raise HTTPException(409, {
                'code': 'exceeds_capacity', 'requested_mb': req.memory_mb, 'total_mb': total,
                'detail': f'{req.memory_mb} MB can never be granted on a {total} MB resource'})
        available = total - sum(r['memory_mb'] or 0 for r in active)
        waiting = conn.execute("SELECT COUNT(*) FROM leases WHERE resource_id=? AND "
                               "status='queued'", (resource_id,)).fetchone()[0]
        holder = next((r for r in active if r['exclusive']), None)
        refusal = None
        if holder is not None:
            refusal = HTTPException(409, {
                'detail': f"Exclusive lease held by {holder['service_id']}",
                'held_by': holder['service_id'], 'expires_at': holder['expires_at']})
        elif exclusive and active:
            refusal = HTTPException(409, 'Cannot grant exclusive lease — active leases exist')
        elif req.memory_mb and req.memory_mb > available:
            refusal = 'over_bytes'
        elif waiting:
            # It would fit, but somebody ahead did not. Strict order: a newcomer
            # does not jump a waiter.
            refusal = HTTPException(409, {
                'code': 'queue_ahead', 'queue_length': waiting,
                'detail': f'{waiting} request(s) are queued ahead on {resource_id}; '
                          f'pass wait_s to join the queue'})
        lease_id = f'lease_{secrets.token_hex(8)}'
        duration = req.effective_duration_s()
        asked_total = max(int(req.max_total_s or 0), int(duration or 0), total_floor)
        max_total = min(asked_total, total_hard)
        if refusal is not None:
            wait_s = req.wait_s
            # `wait: true` with no `wait_s` is a wait with no deadline
            # (no timeouts as a default at any layer). expires_at stays NULL, which the
            # reaper never selects; the row leaves the queue by promotion or by its
            # holder's DELETE. With `wait_s` as well, the deadline stands.
            indefinite = bool(getattr(req, 'wait', None)) and wait_s is None
            if wait_s is None and not indefinite and refusal == 'over_bytes':
                wait_s = DEFAULT_WAIT_S        # the legacy answer, now bounded
            if wait_s is None and not indefinite:
                raise refusal
            wait_until = None if indefinite else (now_dt + timedelta(seconds=wait_s)).isoformat()
            position = waiting + 1
            conn.execute('INSERT INTO leases (id,resource_id,service_id,agent_id,memory_mb,'
                         'exclusive,priority,status,queue_position,created_at,expires_at,'
                         'ttl_s,max_total_s) '
                         "VALUES (?,?,?,?,?,?,?,'queued',?,?,?,?,?)",
                         (lease_id, resource_id, req.service_id, agent_id, req.memory_mb,
                          int(exclusive), req.priority, position, now, wait_until,
                          duration, max_total))
            _renumber(conn, resource_id)
            position = conn.execute('SELECT queue_position FROM leases WHERE id=?',
                                    (lease_id,)).fetchone()[0]
            return ({'lease_id': lease_id, 'status': 'queued', 'queue_position': position,
                     'wait_until': wait_until, 'available_mb': available,
                     'requested_mb': req.memory_mb}, metadata)
        defaulted = exclusive and not duration
        # #827: default finite mutex TTL; preserve the grant's renewal ceiling.
        if defaulted:
            duration = default_ttl
        expires = (now_dt + timedelta(seconds=duration)).isoformat() if duration else None
        try:
            conn.execute('INSERT INTO leases (id,resource_id,service_id,agent_id,memory_mb,'
                         'exclusive,priority,status,granted_at,expires_at,created_at,'
                         'max_total_s,ttl_s) '
                         "VALUES (?,?,?,?,?,?,?,'active',?,?,?,?,?)",
                         (lease_id, resource_id, req.service_id, agent_id, req.memory_mb,
                          int(exclusive), req.priority, now, expires, now, max_total,
                          duration))
        except sqlite3.IntegrityError as exc:
            # #806/#849: retain the schema backstop's 409 contract. SQLite names
            # the column, not the index. Do not mask other integrity failures.
            if str(exc) != 'UNIQUE constraint failed: leases.resource_id':
                raise
            holder = conn.execute("SELECT service_id,expires_at FROM leases WHERE "
                                  "resource_id=? AND exclusive=1 AND status='active'",
                                  (resource_id,)).fetchone()
            holder = dict(holder) if holder is not None else {}
            raise HTTPException(409, {
                'detail': f"Exclusive lease held by {holder.get('service_id', 'unknown')}",
                'held_by': holder.get('service_id'),
                'expires_at': holder.get('expires_at')}) from None
        metadata.update({'ttl_defaulted': defaulted,
                         'max_total_clamped': asked_total if asked_total > total_hard else None})
        return ({'lease_id': lease_id, 'status': 'active', 'granted_at': now,
                 'expires_at': expires, 'max_total_s': max_total,
                 'resource_available_mb': available - (req.memory_mb or 0)},
                metadata)
