"""Durable group intent over the existing lease queues, not a process supervisor.

All state and member bindings commit together. External effects (audit/SSE) are
returned to the HTTP layer and published only after commit. Providers remain
responsible for real cleanup; evidence here is their attributed attestation.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import secrets

from fastapi import HTTPException

from lease_admission import (capacity_of, holds_on_expiry,
                             reap_and_promote_in_transaction,
                             release_in_transaction, reserve_in_transaction)

TERMINAL = {'released', 'cancelled'}
OCCUPYING = {'acquiring', 'admitted', 'serving', 'releasing', 'uncertain'}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Effects:
    actor: str
    moves: list = field(default_factory=list)
    reservations: list = field(default_factory=list)
    releases: list = field(default_factory=list)
    events: list = field(default_factory=list)


@dataclass
class MemberRequest:
    service_id: str | None
    memory_mb: int | None
    ttl_seconds: int
    max_total_s: int | None
    exclusive: bool = True
    priority: str = 'normal'
    wait: bool = True
    wait_s: None = None

    def effective_duration_s(self):
        return self.ttl_seconds


def _group(conn, group_id):
    row = conn.execute('SELECT * FROM lease_groups WHERE id=?', (group_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Lease group not found')
    return dict(row)


def _members(conn, group_id):
    return [dict(row) for row in conn.execute(
        'SELECT * FROM lease_group_members WHERE group_id=? ORDER BY ordinal', (group_id,))]


def _lease(conn, member):
    if member['lease_id'] is None:
        return None
    row = conn.execute('SELECT * FROM leases WHERE id=?', (member['lease_id'],)).fetchone()
    return dict(row) if row else None


def _event(conn, group_id, kind, effects, **data):
    group = _group(conn, group_id)
    at = _now()
    payload = {'group_id': group_id, 'generation': group['generation'],
               'phase': group['phase'], **data}
    cur = conn.execute(
        'INSERT INTO lease_group_events(group_id,generation,kind,phase,actor,data,created_at) '
        'VALUES (?,?,?,?,?,?,?)', (group_id, group['generation'], kind, group['phase'],
                                 effects.actor, _json(payload), at))
    effects.events.append({'sequence': cur.lastrowid, 'kind': kind, 'group_id': group_id,
                           'owner_id': group['owner_id'], 'data': payload})


def _phase(conn, group_id, phase, effects, reason=None):
    old = _group(conn, group_id)
    if old['phase'] == phase and old['reason'] == reason:
        return
    conn.execute('UPDATE lease_groups SET phase=?,reason=?,updated_at=? WHERE id=?',
                 (phase, reason, _now(), group_id))
    _event(conn, group_id, 'lease_group.changed', effects, reason=reason)


def _ended(lease):
    return lease['status'] in TERMINAL or (
        lease['status'] == 'expired' and lease.get('settled_at') is not None)


def _sync(conn, group_id, effects):
    group = _group(conn, group_id)
    if group['phase'] in TERMINAL or group['phase'] == 'pending':
        return
    members = _members(conn, group_id)
    linked = [(member, _lease(conn, member)) for member in members]
    if group['release_requested']:
        missing = any(member['lease_id'] is not None and lease is None
                      for member, lease in linked)
        if missing:
            _phase(conn, group_id, 'uncertain', effects, 'member_receipt_missing')
            return
        has_granted = any(lease and lease.get('granted_at') for _, lease in linked)
        all_ended = all(lease is None or _ended(lease) for _, lease in linked)
        proof_missing = any(lease and lease.get('granted_at') and
                            (not member['cleanup_evidence'] or not member['release_receipt'])
                            for member, lease in linked)
        if all_ended and not proof_missing:
            _phase(conn, group_id, 'released' if has_granted else 'cancelled', effects)
        elif any(lease and lease['status'] == 'expired' and not _ended(lease)
                 for _, lease in linked) or (all_ended and proof_missing):
            _phase(conn, group_id, 'uncertain', effects, 'cleanup_or_settlement_required')
        else:
            _phase(conn, group_id, 'releasing', effects)
        return
    if any(member['lease_id'] is not None and
           (lease is None or lease['status'] not in ('active', 'queued'))
           for member, lease in linked):
        _phase(conn, group_id, 'uncertain', effects, 'member_authority_lost')
    elif all(lease and lease['status'] == 'active' for _, lease in linked):
        _phase(conn, group_id, 'serving' if group['phase'] == 'serving' else 'admitted', effects)
    else:
        _phase(conn, group_id, 'acquiring', effects)


def _refresh(conn, effects, default_ttl):
    effects.moves.append(reap_and_promote_in_transaction(conn, default_ttl=default_ttl))
    for row in conn.execute("SELECT id FROM lease_groups WHERE phase NOT IN "
                            "('pending','released','cancelled')").fetchall():
        _sync(conn, row['id'], effects)


def _generation(group, expected):
    if group['generation'] != expected:
        raise HTTPException(409, {'code': 'group_generation_changed',
                                  'generation': group['generation']})


def _validate_resources(conn, intent, total_hard):
    devices = set()
    for member in intent['members']:
        resource = conn.execute('SELECT * FROM resources WHERE id=?',
                                (member['resource_id'],)).fetchone()
        if resource is None:
            raise HTTPException(404, 'A member resource no longer exists')
        if not holds_on_expiry(resource):
            raise HTTPException(409, {'code': 'group_member_requires_hold',
                                      'resource_id': member['resource_id']})
        if resource['device_id'] in devices:
            raise HTTPException(422, 'A group has one capacity resource per host')
        devices.add(resource['device_id'])
        if member['memory_mb'] and member['memory_mb'] > capacity_of(resource):
            raise HTTPException(409, {'code': 'exceeds_capacity',
                                      'resource_id': member['resource_id']})
        if member['ttl_seconds'] > total_hard:
            raise HTTPException(422, 'Member TTL exceeds the registry renewal ceiling')
        if member['service_id'] and not conn.execute('SELECT 1 FROM services WHERE id=?',
                                                      (member['service_id'],)).fetchone():
            raise HTTPException(422, 'A member service_id is not registered')


def _advance(conn, group_id, effects, *, default_ttl, total_floor, total_hard):
    group = _group(conn, group_id)
    if group['phase'] in TERMINAL or group['release_requested']:
        raise HTTPException(409, 'A releasing or ended group cannot acquire again')
    if group['phase'] == 'uncertain':
        raise HTTPException(409, 'Reconcile and release the uncertain group before a new acquisition')
    intent = json.loads(group['intent_json'])
    _validate_resources(conn, intent, total_hard)
    if group['phase'] == 'pending':
        active = conn.execute("SELECT id FROM lease_groups WHERE phase IN "
                              "('acquiring','admitted','serving','releasing','uncertain')").fetchone()
        first = conn.execute("SELECT id FROM lease_groups WHERE phase='pending' "
                             'ORDER BY sequence LIMIT 1').fetchone()
        if active is not None or first is None or first['id'] != group_id:
            return
        _phase(conn, group_id, 'acquiring', effects)
    for member in _members(conn, group_id):
        if member['lease_id'] is not None:
            continue
        # Stage one is the first declared (expected longest-wait) host. Other
        # hosts keep doing ordinary work until that member is actually granted.
        first = _members(conn, group_id)[0]
        if member['ordinal'] and (_lease(conn, first) or {}).get('status') != 'active':
            break
        req = MemberRequest(**{key: intent['members'][member['ordinal']][key]
                               for key in ('service_id', 'memory_mb', 'ttl_seconds', 'max_total_s')})
        body, metadata = reserve_in_transaction(
            conn, member['resource_id'], req, group['owner_id'], default_ttl=default_ttl,
            total_floor=total_floor, total_hard=total_hard)
        conn.execute('UPDATE lease_group_members SET lease_id=? WHERE group_id=? AND ordinal=?',
                     (body['lease_id'], group_id, member['ordinal']))
        effects.reservations.append((member['resource_id'], req, body, metadata))
        _event(conn, group_id, 'lease_group.member_requested', effects,
               resource_id=member['resource_id'], lease_id=body['lease_id'])
    _sync(conn, group_id, effects)


def _view(conn, group_id):
    group = _group(conn, group_id)
    members = []
    for member in _members(conn, group_id):
        lease = _lease(conn, member)
        receipt = json.loads(member['release_receipt']) if member['release_receipt'] else None
        members.append({'ordinal': member['ordinal'], 'resource_id': member['resource_id'],
                        'lease_id': member['lease_id'],
                        'status': lease['status'] if lease else
                            ('missing' if member['lease_id'] else 'not_requested'),
                        'queue_position': lease.get('queue_position') if lease else None,
                        'expires_at': lease.get('expires_at') if lease else None,
                        'settled_at': lease.get('settled_at') if lease else None,
                        'cleanup_evidence': json.loads(member['cleanup_evidence'])
                            if member['cleanup_evidence'] else None,
                        'release_receipt': receipt})
    return {'id': group_id, 'owner_id': group['owner_id'], 'request_key': group['request_key'],
            'phase': group['phase'], 'generation': group['generation'],
            'release_requested': bool(group['release_requested']), 'reason': group['reason'],
            'intent_sha256': group['intent_sha256'], 'members': members,
            'created_at': group['created_at'], 'updated_at': group['updated_at'],
            'queue_position': 1 + conn.execute(
                "SELECT COUNT(*) FROM lease_groups WHERE phase='pending' AND sequence<?",
                (group['sequence'],)).fetchone()[0] if group['phase'] == 'pending' else None,
            'serving_evidence': json.loads(group['serving_evidence'])
                if group['serving_evidence'] else None,
            'last_event_sequence': conn.execute(
                'SELECT COALESCE(MAX(sequence),0) FROM lease_group_events WHERE group_id=?',
                (group_id,)).fetchone()[0]}


def create(db, intent, owner_id, *, default_ttl, total_floor, total_hard):
    effects = Effects(owner_id)
    canonical = _json(intent)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT * FROM lease_groups WHERE owner_id=? AND request_key=?',
                           (owner_id, intent['request_key'])).fetchone()
        if old is not None:
            if old['intent_sha256'] != digest:
                raise HTTPException(409, {'code': 'group_key_conflict'})
            _refresh(conn, effects, default_ttl)
            return _view(conn, old['id']), effects
        _validate_resources(conn, intent, total_hard)
        _refresh(conn, effects, default_ttl)
        group_id = 'group_' + secrets.token_hex(12)
        at = _now()
        conn.execute('INSERT INTO lease_groups(id,owner_id,request_key,intent_sha256,intent_json,'
                     "phase,created_at,updated_at) VALUES (?,?,?,?,?,'pending',?,?)",
                     (group_id, owner_id, intent['request_key'], digest, canonical, at, at))
        for ordinal, member in enumerate(intent['members']):
            conn.execute('INSERT INTO lease_group_members(group_id,ordinal,resource_id) VALUES (?,?,?)',
                         (group_id, ordinal, member['resource_id']))
        _event(conn, group_id, 'lease_group.created', effects)
        _advance(conn, group_id, effects, default_ttl=default_ttl,
                 total_floor=total_floor, total_hard=total_hard)
        return _view(conn, group_id), effects


def get(db, group_id, actor, *, default_ttl):
    effects = Effects(actor)
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _group(conn, group_id)
        _refresh(conn, effects, default_ttl)
        return _view(conn, group_id), effects


def advance(db, group_id, generation, actor, *, default_ttl, total_floor, total_hard):
    effects = Effects(actor)
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _generation(_group(conn, group_id), generation)
        _refresh(conn, effects, default_ttl)
        _advance(conn, group_id, effects, default_ttl=default_ttl,
                 total_floor=total_floor, total_hard=total_hard)
        return _view(conn, group_id), effects


def _begin_release(conn, group_id, effects, default_ttl):
    group = _group(conn, group_id)
    if group['phase'] in TERMINAL:
        return
    conn.execute('UPDATE lease_groups SET release_requested=1 WHERE id=?', (group_id,))
    # No resource is held for a pending group; move directly to terminal without
    # taking the singleton index away from the group currently using the hosts.
    if group['phase'] == 'pending':
        _phase(conn, group_id, 'cancelled', effects)
        return
    _phase(conn, group_id, 'releasing', effects)
    for member in _members(conn, group_id):
        lease = _lease(conn, member)
        if lease and lease['status'] == 'queued':
            moves = release_in_transaction(conn, lease, default_ttl=default_ttl)
            effects.moves.append(moves)
            effects.releases.append((lease, moves['status']))
            conn.execute('UPDATE lease_group_members SET release_receipt=? '
                         'WHERE group_id=? AND ordinal=?',
                         (_json({'lease_id': lease['id'], 'status': moves['status']}),
                          group_id, member['ordinal']))
    _sync(conn, group_id, effects)


def begin_release(db, group_id, generation, actor, *, default_ttl):
    effects = Effects(actor)
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _generation(_group(conn, group_id), generation)
        _refresh(conn, effects, default_ttl)
        _begin_release(conn, group_id, effects, default_ttl)
        return _view(conn, group_id), effects


def release_member(db, group_id, lease_id, generation, proof, actor, *, default_ttl):
    effects = Effects(actor)
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        group = _group(conn, group_id)
        _generation(group, generation)
        member = conn.execute('SELECT * FROM lease_group_members WHERE group_id=? AND lease_id=?',
                              (group_id, lease_id)).fetchone()
        if member is None:
            raise HTTPException(404, 'Lease is not a member of this group')
        member = dict(member)
        lease = _lease(conn, member)
        if lease is None or lease['agent_id'] != group['owner_id'] or not lease['exclusive']:
            raise HTTPException(409, 'Exact member lease receipt is missing or changed')
        saved = member['cleanup_evidence']
        evidence = {'generation': generation, 'lease_id': lease_id,
                    'attested_by': actor, **proof}
        if saved:
            # A coordinator fence cannot erase cleanup already acknowledged for
            # this exact lease. Reuse the original attester/epoch, but never let
            # a successor replace its evidence with a different account.
            prior = json.loads(saved)
            if any(prior.get(key) != value for key, value in proof.items()):
                raise HTTPException(409, 'Cleanup evidence is already bound to this member')
            evidence = prior
        _refresh(conn, effects, default_ttl)
        _begin_release(conn, group_id, effects, default_ttl)
        conn.execute('UPDATE lease_group_members SET cleanup_evidence=? WHERE group_id=? AND lease_id=?',
                     (_json(evidence), group_id, lease_id))
        moves = release_in_transaction(conn, lease, default_ttl=default_ttl)
        effects.moves.append(moves)
        if moves['released']:
            effects.releases.append((lease, moves['status']))
        receipt = conn.execute('SELECT * FROM leases WHERE id=?', (lease_id,)).fetchone()
        if receipt is None or not _ended(dict(receipt)):
            raise HTTPException(409, 'Member settlement is not confirmed')
        conn.execute('UPDATE lease_group_members SET release_receipt=? WHERE group_id=? AND lease_id=?',
                     (_json({'lease_id': lease_id, 'status': receipt['status'],
                             'settled_at': receipt['settled_at'], 'released_at': receipt['released_at']}),
                      group_id, lease_id))
        if not saved:
            _event(conn, group_id, 'lease_group.member_released', effects, lease_id=lease_id)
        _sync(conn, group_id, effects)
        return _view(conn, group_id), effects


def mark_serving(db, group_id, generation, proof, actor, *, default_ttl):
    effects = Effects(actor)
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        group = _group(conn, group_id)
        _generation(group, generation)
        _refresh(conn, effects, default_ttl)
        group = _group(conn, group_id)
        evidence = _json({'generation': generation, 'attested_by': actor, **proof})
        if group['phase'] == 'serving':
            if group['serving_evidence'] != evidence:
                raise HTTPException(409, 'Serving evidence is already bound')
        elif group['phase'] != 'admitted' or group['release_requested']:
            raise HTTPException(409, 'Every required member must be admitted before serving')
        else:
            conn.execute('UPDATE lease_groups SET serving_evidence=? WHERE id=?', (evidence, group_id))
            _phase(conn, group_id, 'serving', effects)
        return _view(conn, group_id), effects


def fence(db, group_id, generation, reason, actor, *, default_ttl):
    """Invalidate old coordinator commands; no process-death or retry inference."""
    effects = Effects(actor)
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        group = _group(conn, group_id)
        _generation(group, generation)
        if group['phase'] in TERMINAL:
            raise HTTPException(409, 'Group has already ended')
        _refresh(conn, effects, default_ttl)
        conn.execute('UPDATE lease_groups SET generation=generation+1 WHERE id=?', (group_id,))
        _event(conn, group_id, 'lease_group.fenced', effects, reason=reason)
        _begin_release(conn, group_id, effects, default_ttl)
        return _view(conn, group_id), effects
