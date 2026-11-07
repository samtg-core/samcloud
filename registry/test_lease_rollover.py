"""Same-allocation rollover: HTTP auth, transactions, idempotency and accounting."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading

import pytest

import db
from test_registry import (client, AuthedClient, _register_device, _invite_and_activate)


@pytest.fixture
def memory_resource(client):
    assert _register_device(client, 'rollover-host', 'rollover-host').status_code == 200
    response = client.post('/api/v1/resources', json={
        'device_id':'rollover-host', 'name':'gpu', 'type':'gpu', 'provider':'metal',
        'specs':{'unified_memory_mb':1000}, 'allocation_mode':'shared'})
    assert response.status_code == 200, response.text
    return '/api/v1/resources/rollover-host/gpu'


def allocate(client, resource, memory=800, **extra):
    response = client.post(resource+'/leases', json={
        'memory_mb':memory, 'ttl_seconds':3600, **extra})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'active', response.text
    return response.json()['lease_id']


def rotate(client, lease, key='persisted-rotation-1', ttl=3600):
    return client.post('/api/v1/leases/'+lease+'/rollover',
                       json={'key':key, 'ttl_seconds':ttl})


def test_full_resource_can_continue_without_second_allocation(client, memory_resource):
    lease = allocate(client, memory_resource)
    allocate(client, memory_resource, 200)
    ordinary = client.post(memory_resource+'/leases', json={'memory_mb':800, 'ttl_seconds':3600})
    assert ordinary.json()['status'] == 'queued'  # acquire-before-release cannot fit
    before = db.query_one("SELECT SUM(memory_mb) AS n FROM leases WHERE status='active'")['n']
    response = rotate(client, lease)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['lease_id'] != lease and body['replaces_lease_id'] == lease
    assert body['memory_mb'] == 800 and body['status'] == 'active'
    assert db.query_one('SELECT status FROM leases WHERE id=?', (lease,))['status'] == 'released'
    assert db.query_one("SELECT SUM(memory_mb) AS n FROM leases WHERE status='active'")['n'] == before == 1000
    assert db.query_one("SELECT COUNT(*) AS n FROM leases WHERE status='active'")['n'] == 2
    assert db.query_one("SELECT COUNT(*) AS n FROM audit_log WHERE action='lease.rolled_over'")['n'] == 1


def test_lost_response_replay_is_same_grant_not_new_memory(client, memory_resource):
    lease = allocate(client, memory_resource)
    first = rotate(client, lease)
    second = rotate(client, lease)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert db.query_one('SELECT COUNT(*) AS n FROM leases')['n'] == 2
    assert db.query_one('SELECT COUNT(*) AS n FROM lease_rollovers')['n'] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM audit_log WHERE action='lease.rolled_over'")['n'] == 1


@pytest.mark.parametrize('key,ttl', [('different-intent',3600), ('persisted-rotation-1',1800)])
def test_changed_intent_refuses_without_another_grant(client, memory_resource, key, ttl):
    lease = allocate(client, memory_resource)
    assert rotate(client, lease).status_code == 200
    response = rotate(client, lease, key, ttl)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'rollover_intent_conflict'
    assert db.query_one('SELECT COUNT(*) AS n FROM leases')['n'] == 2


@pytest.mark.parametrize('state', ['expired', 'released'])
def test_replay_reports_current_replacement_not_old_active_receipt(client, memory_resource, state):
    lease = allocate(client, memory_resource)
    new = rotate(client, lease).json()['lease_id']
    if state == 'expired':
        db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (new,))
    else:
        assert client.delete('/api/v1/leases/'+new).status_code == 200
    replay = rotate(client, lease)
    assert replay.status_code == 200, replay.text
    assert replay.json()['status'] == state
    assert db.query_one('SELECT COUNT(*) AS n FROM leases')['n'] == 2


def test_expired_predecessor_is_not_resurrected(client, memory_resource):
    lease = allocate(client, memory_resource)
    db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (lease,))
    assert rotate(client, lease).status_code == 404
    assert db.query_one('SELECT COUNT(*) AS n FROM leases')['n'] == 1
    assert db.query_one('SELECT COUNT(*) AS n FROM lease_rollovers')['n'] == 0


@pytest.mark.parametrize('change', ['exclusive_lease', 'exclusive_resource', 'zero_bytes', 'indefinite'])
def test_rollover_does_not_reset_exclusive_or_nonallocation_limits(client, memory_resource, change):
    lease = allocate(client, memory_resource)
    if change == 'exclusive_lease':
        db.execute('UPDATE leases SET exclusive=1 WHERE id=?', (lease,))
    elif change == 'exclusive_resource':
        db.execute("UPDATE resources SET allocation_mode='exclusive' WHERE id='rollover-host/gpu'")
    elif change == 'zero_bytes':
        db.execute('UPDATE leases SET memory_mb=0 WHERE id=?', (lease,))
    else:
        db.execute('UPDATE leases SET expires_at=NULL WHERE id=?', (lease,))
    assert rotate(client, lease).status_code in (404,422)
    assert db.query_one('SELECT COUNT(*) AS n FROM leases')['n'] == 1


@pytest.mark.parametrize('payload', [
    {'key':'x','ttl_seconds':3601}, {'key':'x','ttl_seconds':True},
    {'key':'x','ttl_seconds':0}, {'key':' ','ttl_seconds':3600},
    {'key':'x','ttl_seconds':3600,'memory_mb':1600},
    {'key':'x','ttl_seconds':3600,'resource_id':'different'},
    {'key':chr(0xD800),'ttl_seconds':3600}])
def test_request_cannot_change_allocation_or_increase_exposure(client, memory_resource, payload):
    lease = allocate(client, memory_resource)
    response = client.post('/api/v1/leases/'+lease+'/rollover',
        content=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
    assert response.status_code == 422, response.text
    assert db.query_one('SELECT COUNT(*) AS n FROM leases')['n'] == 1
    assert db.query_one('SELECT status FROM leases WHERE id=?', (lease,))['status'] == 'active'


def test_only_holder_or_admin_with_resource_management_can_roll(client, memory_resource):
    token, _ = _invite_and_activate(client, client._client, 'resident', ['device:rollover-host'])
    holder = AuthedClient(client._client, token)
    lease = allocate(holder, memory_resource)
    other_token, _ = _invite_and_activate(client, client._client, 'other', ['device:rollover-host'])
    other = AuthedClient(client._client, other_token)
    assert rotate(other, lease).status_code == 403
    hidden_token, _ = _invite_and_activate(client, client._client, 'hidden', ['device:elsewhere'])
    assert rotate(AuthedClient(client._client, hidden_token), lease).status_code == 404
    assert client._client.post('/api/v1/leases/'+lease+'/rollover',
                              json={'key':'x','ttl_seconds':3600}).status_code == 401
    assert rotate(holder, lease).status_code == 200


def test_racing_same_intent_grants_once(client, memory_resource):
    lease = allocate(client, memory_resource)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _:rotate(client,lease), range(2)))
    assert [r.status_code for r in responses] == [200,200]
    assert responses[0].json()['lease_id'] == responses[1].json()['lease_id']
    assert db.query_one("SELECT SUM(memory_mb) AS n FROM leases WHERE status='active'")['n'] == 800
    assert db.query_one('SELECT COUNT(*) AS n FROM leases')['n'] == 2


def test_racing_different_intents_cannot_both_replace_one_allocation(client, memory_resource):
    lease = allocate(client, memory_resource)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda key:rotate(client,lease,key), ['intent-a','intent-b']))
    assert sorted(r.status_code for r in responses) == [200,409]
    assert db.query_one('SELECT COUNT(*) AS n FROM lease_rollovers')['n'] == 1
    assert db.query_one("SELECT SUM(memory_mb) AS n FROM leases WHERE status='active'")['n'] == 800


def test_readers_never_see_predecessor_retired_without_replacement(client, memory_resource, monkeypatch):
    lease = allocate(client, memory_resource)
    original = db.transaction
    inside_swap, allow_commit = threading.Event(), threading.Event()
    class PausedInsert:
        def __init__(self, connection): self.connection = connection
        def execute(self, sql, parameters=()):
            if sql.startswith('INSERT INTO leases'):
                inside_swap.set()
                assert allow_commit.wait(5), 'test must release its synthetic writer'
            return self.connection.execute(sql, parameters)
    @contextmanager
    def transaction():
        with original() as connection:
            yield PausedInsert(connection)
    monkeypatch.setattr(db, 'transaction', transaction)
    with ThreadPoolExecutor(max_workers=1) as pool:
        swapping = pool.submit(rotate, client, lease)
        try:
            assert inside_swap.wait(5)
            assert db.query_one('SELECT status FROM leases WHERE id=?', (lease,))['status'] == 'active'
            assert db.query_one("SELECT SUM(memory_mb) AS n FROM leases WHERE status='active'")['n'] == 800
        finally:
            allow_commit.set()
        assert swapping.result(timeout=5).status_code == 200
    assert db.query_one('SELECT status FROM leases WHERE id=?', (lease,))['status'] == 'released'
    assert db.query_one("SELECT SUM(memory_mb) AS n FROM leases WHERE status='active'")['n'] == 800


def test_additive_schema_reopen_preserves_existing_lease_and_rollover_links(client, memory_resource):
    lease = allocate(client, memory_resource)
    db.execute('DROP TABLE lease_rollovers')  # simulate pre-feature schema, synthetic DB only
    before = db.query_one('SELECT * FROM leases WHERE id=?', (lease,))
    db.init_db()
    assert db.query_one('SELECT * FROM leases WHERE id=?', (lease,)) == before
    first = rotate(client,lease)
    assert first.status_code == 200
    db.init_db()
    assert rotate(client,lease).json() == first.json()


def test_resource_delete_remains_possible_with_rollover_history(client, memory_resource):
    lease = allocate(client,memory_resource)
    assert rotate(client,lease).status_code == 200
    assert client.delete(memory_resource).status_code == 200
    assert db.query_one('SELECT COUNT(*) AS n FROM lease_rollovers')['n'] == 0


def test_api_index_names_distinct_rollover_contract(client):
    operation = client.get('/api/v1/').json()['endpoints']['leases']['rollover']
    assert operation['method'] == 'POST'
    assert operation['path'] == '/api/v1/leases/{id}/rollover'


def test_failed_insert_rolls_back_predecessor_retirement(client, memory_resource, monkeypatch):
    lease = allocate(client, memory_resource)
    original = db.transaction
    class FailingInsert:
        def __init__(self, connection): self.connection = connection
        def execute(self, sql, parameters=()):
            if sql.startswith('INSERT INTO leases'):
                raise RuntimeError('synthetic write failure')
            return self.connection.execute(sql, parameters)
    @contextmanager
    def transaction():
        with original() as connection:
            yield FailingInsert(connection)
    monkeypatch.setattr(db, 'transaction', transaction)
    with pytest.raises(RuntimeError, match='synthetic write failure'):
        rotate(client, lease)
    assert db.query_one('SELECT status FROM leases WHERE id=?', (lease,))['status'] == 'active'
    assert db.query_one('SELECT COUNT(*) AS n FROM leases')['n'] == 1
    assert db.query_one('SELECT COUNT(*) AS n FROM lease_rollovers')['n'] == 0
