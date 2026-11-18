"""Atomic bounded setup recovery after all expired members clean up.

The provider supplies attributed cleanup for each exact old lease. Core records
it and transfers the same already-charged allocations, without a free-capacity
window or an intervening promotion. No expired row is resurrected and no host,
worker, inference or timeout policy is selected here.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets

from fastapi import HTTPException

import lease_groups as groups
from lease_admission import holds_on_expiry


def _intent(request):
    key = request['request_key']
    if not key.strip():
        raise HTTPException(422, 'An explicit successor request key is required')
    value = {**request, 'members': sorted(request['members'], key=lambda m: m['lease_id'])}
    raw = groups._json(value)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def _predecessors(conn, group, proof, *, total_floor, total_hard, now):
    members = groups._members(conn, group['id'])
    intent = json.loads(group['intent_json'])
    groups._validate_resources(conn, intent, total_hard)
    supplied = {item['lease_id']: item for item in proof}
    if (len(supplied) != len(proof) or len(members) != len(intent['members'])
            or set(supplied) != {m['lease_id'] for m in members}):
        raise HTTPException(409, 'Cleanup must name every exact member once')
    checked = []
    for member, requested in zip(members, intent['members']):
        lease = groups._lease(conn, member)
        resource = conn.execute('SELECT * FROM resources WHERE id=?',
                                (member['resource_id'],)).fetchone()
        ttl = requested['ttl_seconds']
        bound = min(max(requested['max_total_s'] or 0, ttl, total_floor), total_hard)
        try:
            granted = datetime.fromisoformat(lease['granted_at'])
            expiry = datetime.fromisoformat(lease['expires_at'])
            expired = (granted < expiry <= now
                       and expiry <= granted + timedelta(seconds=bound))
        except (TypeError, ValueError):
            expired = False
        if (not lease or not resource or not holds_on_expiry(resource)
                or member['resource_id'] != requested['resource_id']
                or lease['resource_id'] != member['resource_id']
                or lease['agent_id'] != group['owner_id']
                or lease['service_id'] != requested['service_id']
                or lease['memory_mb'] != requested['memory_mb']
                or lease['ttl_s'] != ttl or lease['max_total_s'] != bound
                or not lease['exclusive'] or lease['priority'] != 'normal'
                or lease['status'] != 'expired' or not expired
                or lease['released_at'] is not None or lease['settled_at'] is not None
                or member['cleanup_evidence'] or member['release_receipt']):
            raise HTTPException(409, 'Exact expired, unsettled HOLD member required')
        # An exclusive predecessor must still be the whole charged allocation.
        # Queued callers keep their order, but a conflicting live/held allocation
        # is never hidden by crediting our predecessor.
        other = conn.execute("SELECT id FROM leases WHERE resource_id=? AND id!=? AND "
                             "(status='active' OR (status='expired' AND settled_at IS NULL))",
                             (lease['resource_id'], lease['id'])).fetchone()
        if other:
            raise HTTPException(409, 'Another allocation is charged on a member resource')
        checked.append((member, requested, lease, supplied[lease['id']]))
    return intent, checked


def continue_after_cleanup(db, group_id, request, actor, *, default_ttl, total_floor, total_hard):
    """Retain the pool atomically; return current successor state on replay.

    This is a fresh bounded operation, not the production active-session renewal
    contract. The original intent and old rows remain immutable history. The
    successor retains the exact dimensions, TTLs and effective exposure bounds.
    """
    raw, digest = _intent(request)
    effects = groups.Effects(actor)
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        group = groups._group(conn, group_id)
        prior = conn.execute('SELECT * FROM lease_group_continuations WHERE predecessor_id=?',
                             (group_id,)).fetchone()
        if prior:
            if prior['intent_sha256'] != digest:
                raise HTTPException(409, {'code': 'group_continuation_conflict'})
            groups._refresh(conn, effects, default_ttl)
            return groups._view(conn, prior['successor_id']), effects
        groups._generation(group, request['generation'])
        if (group['release_requested'] or group['phase'] in groups.TERMINAL
                or group['serving_evidence'] is not None):
            raise HTTPException(409, 'Only a never-serving setup group can continue before release')
        if conn.execute('SELECT id FROM lease_groups WHERE owner_id=? AND request_key=?',
                        (group['owner_id'], request['request_key'])).fetchone():
            raise HTTPException(409, {'code': 'group_key_conflict'})
        groups._refresh(conn, effects, default_ttl)
        group = groups._group(conn, group_id)
        if group['phase'] != 'uncertain':
            raise HTTPException(409, 'Only an entirely expired HOLD group can continue')
        now = datetime.now(timezone.utc)
        intent, checked = _predecessors(conn, group, request['members'],
                                        total_floor=total_floor, total_hard=total_hard, now=now)
        at = now.isoformat()
        for member, _wanted, lease, proof in checked:
            evidence = {key: proof[key] for key in ('evidence_ref', 'evidence_sha256')}
            evidence.update(generation=group['generation'], lease_id=lease['id'], attested_by=actor)
            changed = conn.execute("UPDATE leases SET settled_at=? WHERE id=? "
                                   "AND status='expired' AND settled_at IS NULL AND released_at IS NULL",
                                   (at, lease['id'])).rowcount
            if changed != 1:
                raise HTTPException(409, 'Predecessor settlement changed')
            receipt = {'lease_id': lease['id'], 'status': 'expired', 'settled_at': at,
                       'released_at': None}
            conn.execute('UPDATE lease_group_members SET cleanup_evidence=?,release_receipt=? '
                         'WHERE group_id=? AND ordinal=?',
                         (groups._json(evidence), groups._json(receipt), group_id, member['ordinal']))
            effects.releases.append((lease, 'settled'))
        # Fence every old command and end the old group before inserting the
        # singleton successor. No external observer sees this intermediate state.
        conn.execute('UPDATE lease_groups SET generation=generation+1 WHERE id=?', (group_id,))
        groups._event(conn, group_id, 'lease_group.fenced', effects,
                      reason='explicit_cleanup_continuation')
        groups._begin_release(conn, group_id, effects, default_ttl)
        if groups._group(conn, group_id)['phase'] != 'released':
            raise HTTPException(409, 'All predecessor settlements must be confirmed')
        successor = 'group_' + secrets.token_hex(12)
        intent = {**intent, 'request_key': request['request_key']}
        canonical = groups._json(intent)
        conn.execute('INSERT INTO lease_groups(id,owner_id,request_key,intent_sha256,intent_json,'
                     "phase,created_at,updated_at) VALUES (?,?,?,?,?,'admitted',?,?)",
                     (successor, group['owner_id'], request['request_key'],
                      hashlib.sha256(canonical.encode()).hexdigest(), canonical, at, at))
        for member, wanted, lease, _proof in checked:
            ident = 'lease_' + secrets.token_hex(8)
            expires = (now + timedelta(seconds=lease['ttl_s'])).isoformat()
            conn.execute('INSERT INTO leases(id,resource_id,service_id,agent_id,memory_mb,exclusive,'
                         'priority,status,granted_at,expires_at,created_at,max_total_s,ttl_s) '
                         "VALUES (?,?,?,?,?,1,'normal','active',?,?,?,?,?)",
                         (ident, lease['resource_id'], lease['service_id'], lease['agent_id'],
                          lease['memory_mb'], at, expires, at, lease['max_total_s'], lease['ttl_s']))
            conn.execute('INSERT INTO lease_group_members(group_id,ordinal,resource_id,lease_id) '
                         'VALUES (?,?,?,?)', (successor, member['ordinal'], member['resource_id'], ident))
            req = groups.MemberRequest(**{key: wanted[key] for key in
                ('service_id', 'memory_mb', 'ttl_seconds', 'max_total_s')})
            body = {'lease_id': ident, 'status': 'active', 'granted_at': at,
                    'expires_at': expires, 'max_total_s': lease['max_total_s']}
            effects.reservations.append((lease['resource_id'], req, body,
                                         {'max_total_clamped': None, 'ttl_defaulted': False}))
        conn.execute('INSERT INTO lease_group_continuations VALUES(?,?,?,?,?,?)',
                     (group_id, successor, digest, raw, actor, at))
        groups._event(conn, group_id, 'lease_group.continued', effects, successor_id=successor)
        groups._event(conn, successor, 'lease_group.created', effects, predecessor_id=group_id)
        # Only now may ordinary reconciliation run: replacements already charge
        # all the same resources, so no intervening waiter can take the pool.
        groups._refresh(conn, effects, default_ttl)
        return groups._view(conn, successor), effects
