"""Member admission can share a group transaction without publishing or committing it."""
import pytest
from fastapi import HTTPException

import db
import main
from lease_admission import reserve_in_transaction
from test_lease_admission import resource  # noqa: F401
from test_registry import client  # noqa: F401


def reserve(conn, resource_id='admission-host/memory', **values):
    return reserve_in_transaction(
        conn, resource_id, main.LeaseRequest(**values), 'test-group-owner',
        default_ttl=600, total_floor=3600, total_hard=21600)


def test_requires_an_outer_transaction(client, resource):
    conn = db.get_connection()
    try:
        with pytest.raises(RuntimeError, match='caller-owned write transaction'):
            reserve(conn, memory_mb=100)
    finally:
        conn.close()
    assert db.query_one('SELECT COUNT(*) AS n FROM leases')['n'] == 0


def test_later_member_refusal_rolls_back_the_first_lease(client, resource):
    with pytest.raises(HTTPException) as caught:
        with db.transaction() as conn:
            conn.execute('BEGIN IMMEDIATE')
            first, _ = reserve(conn, exclusive=True, wait=True)
            assert first['status'] == 'active'
            reserve(conn, 'missing-host/memory', exclusive=True, wait=True)
    assert caught.value.status_code == 404
    assert db.query_one('SELECT COUNT(*) AS n FROM leases')['n'] == 0


def test_preserves_ordinary_queue_order_and_does_not_publish(client, resource, monkeypatch):
    holder = client.post(resource + '/leases', json={'exclusive': True}).json()
    older = client.post(resource + '/leases', json={
        'exclusive': True, 'wait': True}).json()
    events = []
    monkeypatch.setattr(main.event_bus, 'emit', lambda *args, **kw: events.append(args))
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        member, moves = reserve(conn, exclusive=True, wait=True)
        assert conn.in_transaction
        assert member['status'] == 'queued'
        assert member['queue_position'] == 2
        assert member['wait_until'] is None
        assert moves['promoted'] == []
        # A second reader sees neither a partial member nor a committed group.
        assert db.query_one('SELECT id FROM leases WHERE id=?', (member['lease_id'],)) is None
    assert events == []
    assert db.query_one('SELECT status FROM leases WHERE id=?', (member['lease_id'],))['status'] == 'queued'
    result = client.delete('/api/v1/leases/' + holder['lease_id']).json()
    assert result['promoted'] == [older['lease_id']]
    assert db.query_one('SELECT status FROM leases WHERE id=?', (member['lease_id'],))['status'] == 'queued'


def test_rollback_undoes_reaping_and_promotion_as_well_as_new_member(client, resource):
    holder = client.post(resource + '/leases', json={'exclusive': True}).json()
    older = client.post(resource + '/leases', json={
        'exclusive': True, 'wait': True}).json()
    db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
               (holder['lease_id'],))
    with pytest.raises(RuntimeError, match='group record refused'):
        with db.transaction() as conn:
            conn.execute('BEGIN IMMEDIATE')
            member, moves = reserve(conn, exclusive=True, wait=True)
            assert [row['id'] for row in moves['promoted']] == [older['lease_id']]
            assert member['queue_position'] == 1
            raise RuntimeError('group record refused')
    states = {row['id']: row['status'] for row in db.query('SELECT id,status FROM leases')}
    assert states == {holder['lease_id']: 'active', older['lease_id']: 'queued'}


def test_hold_expiry_does_not_admit_a_member_over_unknown_work(client, resource):
    db.execute("UPDATE resources SET expiry_policy='hold' WHERE id='admission-host/memory'")
    holder = client.post(resource + '/leases', json={'exclusive': True}).json()
    db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
               (holder['lease_id'],))
    with db.transaction() as conn:
        conn.execute('BEGIN IMMEDIATE')
        member, moves = reserve(conn, exclusive=True, wait=True)
        assert member['status'] == 'queued'
        assert member['wait_until'] is None
        assert moves['promoted'] == []
        assert [row['id'] for row in moves['expired']] == [holder['lease_id']]
    old = db.query_one('SELECT * FROM leases WHERE id=?', (holder['lease_id'],))
    assert old['status'] == 'expired' and old['settled_at'] is None
    assert db.query_one('SELECT status FROM leases WHERE id=?', (member['lease_id'],))['status'] == 'queued'
