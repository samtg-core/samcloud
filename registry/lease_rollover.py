"""Atomic continuation of an existing shared-memory allocation.

Separate from renew: this is an explicit replacement grant, not a heartbeat
that bypasses an exclusive/job lease's absolute ceiling. Only its holder (or
admin), also authorized to manage the resource, can continue the same bytes.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

import db


class RolloverRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=512)
    ttl_seconds: int = Field(strict=True, ge=1)

    @field_validator('key', mode='before')
    @classmethod
    def valid_key_encoding(cls, value):
        # Reject before Pydantic's str validation: its ordinary error embeds the
        # invalid surrogate in `input`, which the JSON error renderer cannot
        # encode. Never echo an opaque operation key in that failure response.
        if isinstance(value, str):
            try:
                value.encode('utf-8')
            except UnicodeEncodeError:
                raise HTTPException(422, 'Rollover key must be valid UTF-8') from None
        return value


def _response(row, previous_id):
    return {'lease_id':row['id'], 'status':row['status'],
            'resource_id':row['resource_id'], 'memory_mb':row['memory_mb'],
            'granted_at':row['granted_at'], 'expires_at':row['expires_at'],
            'max_total_s':row['max_total_s'], 'replaces_lease_id':previous_id}


def rollover(lease_id, request, auth, *, require_manage, default_max_total, emit):
    """Credit the predecessor only inside the same serialized transaction.

    No byte count, holder or resource is taken from the request. Replays read
    the linked row's CURRENT state; they never resurrect an expired/released
    replacement or return a cached 'active' receipt. A conflicting replacement
    intent refuses, including a changed TTL under the same key.
    """
    if not request.key.strip():
        raise HTTPException(422, 'A persisted rollover key is required')
    predecessor = db.query_one('SELECT resource_id FROM leases WHERE id=?', (lease_id,))
    if not predecessor:
        raise HTTPException(404, 'Lease not found')
    require_manage(auth, predecessor['resource_id'], manage=True)
    key_hash = hashlib.sha256(request.key.encode()).hexdigest()
    replay = False
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = conn.execute('SELECT * FROM leases WHERE id=?', (lease_id,)).fetchone()
        if previous is None:
            raise HTTPException(404, 'Lease not found')
        if not auth.is_admin() and previous['agent_id'] != auth.owner_id:
            raise HTTPException(403, 'A residency allocation is continued by its holder')
        resource = conn.execute('SELECT allocation_mode FROM resources WHERE id=?',
                                (previous['resource_id'],)).fetchone()
        if (resource is None or resource['allocation_mode'] == 'exclusive' or
                previous['exclusive'] or not previous['memory_mb'] or previous['memory_mb'] <= 0):
            raise HTTPException(422, 'Rollover is only for shared positive-memory allocations')
        now = datetime.now(timezone.utc)
        linked = conn.execute('SELECT * FROM lease_rollovers WHERE previous_id=?',
                              (lease_id,)).fetchone()
        if linked:
            if linked['key_hash'] != key_hash or linked['ttl_seconds'] != request.ttl_seconds:
                raise HTTPException(409, {'code':'rollover_intent_conflict'})
            row = conn.execute('SELECT * FROM leases WHERE id=?', (linked['next_id'],)).fetchone()
            if row['status'] == 'active' and row['expires_at'] is not None:
                if datetime.fromisoformat(row['expires_at']) <= now:
                    conn.execute("UPDATE leases SET status='expired' WHERE id=?", (row['id'],))
                    row = conn.execute('SELECT * FROM leases WHERE id=?', (row['id'],)).fetchone()
            result = _response(row, lease_id)
            replay = True
        else:
            if previous['status'] != 'active' or previous['expires_at'] is None:
                raise HTTPException(404, 'A finite active predecessor is required')
            expiry = datetime.fromisoformat(previous['expires_at'])
            if expiry <= now:
                raise HTTPException(404, 'The predecessor has expired; reconcile ownership')
            maximum = previous['max_total_s'] or default_max_total
            if request.ttl_seconds > maximum:
                raise HTTPException(422, 'Rollover cannot increase the predecessor exposure bound')
            next_id = 'lease_' + secrets.token_hex(8)
            expires_at = max(expiry, now + timedelta(seconds=request.ttl_seconds)).isoformat()
            conn.execute("UPDATE leases SET status='released',released_at=? WHERE id=?",
                         (now.isoformat(), lease_id))
            conn.execute('''INSERT INTO leases
                (id,resource_id,service_id,agent_id,memory_mb,exclusive,priority,status,
                 granted_at,expires_at,created_at,max_total_s)
                VALUES (?,?,?,?,?,0,?,'active',?,?,?,?)''',
                (next_id,previous['resource_id'],previous['service_id'],previous['agent_id'],
                 previous['memory_mb'],previous['priority'],now.isoformat(),expires_at,
                 now.isoformat(),maximum))
            conn.execute('INSERT INTO lease_rollovers VALUES(?,?,?,?,?,?)',
                         (lease_id,next_id,key_hash,request.ttl_seconds,auth.owner_id,now.isoformat()))
            conn.execute('INSERT INTO audit_log(actor,action,target,detail) VALUES(?,?,?,?)',
                (auth.owner_id,'lease.rolled_over',next_id,json.dumps({
                    'previous_id':lease_id,'resource_id':previous['resource_id'],
                    'memory_mb':previous['memory_mb']})))
            row = conn.execute('SELECT * FROM leases WHERE id=?', (next_id,)).fetchone()
            result = _response(row, lease_id)
    if not replay:
        emit('lease.rolled_over', result['lease_id'], {
            'lease_id':result['lease_id'], 'previous_id':lease_id,
            'resource_id':result['resource_id'], 'memory_mb':result['memory_mb']})
    return result
