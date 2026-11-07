"""HTTP + real SQLite contention tests; temporary DB from registry fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import sqlite3

import pytest

import db
from test_registry import client, _register_device


@pytest.fixture
def resource(client):
    assert _register_device(client, 'admission-host', 'admission-host').status_code == 200
    r = client.post('/api/v1/resources', json={
        'device_id': 'admission-host', 'name': 'memory', 'type': 'memory',
        'provider': 'test', 'specs': {'ram_mb': 1000}, 'allocation_mode': 'shared'})
    assert r.status_code == 200, r.text
    return '/api/v1/resources/admission-host/memory'


def race(client, resource, payloads):
    barrier = threading.Barrier(len(payloads))
    def send(payload):
        barrier.wait(timeout=10)
        return client.post(resource + '/leases', json=payload)
    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        return list(pool.map(send, payloads))


def test_shared_race_cannot_spend_budget_twice(client, resource):
    responses = race(client, resource, [{'memory_mb': 200}] * 10)
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    states = [r.json()['status'] for r in responses]
    assert states.count('active') == states.count('queued') == 5
    assert db.query_one("SELECT SUM(memory_mb) n FROM leases WHERE status='active'")['n'] == 1000
    positions = sorted(r.json()['queue_position'] for r in responses if r.json()['status'] == 'queued')
    assert positions == [1, 2, 3, 4, 5]


@pytest.mark.parametrize('iteration', range(5))
def test_shared_exclusive_race_is_mutually_exclusive(client, resource, iteration):
    responses = race(client, resource, [{'memory_mb': 100, 'exclusive': True}, {'memory_mb': 100}])
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    assert db.query_one("SELECT COUNT(*) n FROM leases WHERE status='active'")['n'] == 1


@pytest.mark.parametrize('amount', [-1, True, '100', 1.5])
def test_non_integer_or_negative_claim_rejected(client, resource, amount):
    r = client.post(resource + '/leases', json={'memory_mb': amount})
    assert r.status_code == 422, r.text
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == 0


def test_capacity_response_subtracts_once_and_matches_resource(client, resource):
    r = client.post(resource + '/leases', json={'memory_mb': 800})
    assert r.json()['resource_available_mb'] == 200
    assert client.get(resource).json()['available_memory_mb'] == 200


def test_grant_event_observes_committed_row(client, resource, monkeypatch):
    import main
    observed = []
    def emit(kind, subject, payload, **kwargs):
        if kind == 'lease.granted':
            observed.append(db.query_one('SELECT status FROM leases WHERE id=?', (subject,))['status'])
    monkeypatch.setattr(main.event_bus, 'emit', emit)
    assert client.post(resource + '/leases', json={'memory_mb': 800}).status_code == 200
    assert observed == ['active']


def test_rollback_does_not_publish_or_keep_grant(client, resource, monkeypatch):
    import main
    original = db.transaction
    @contextmanager
    def fail_commit():
        with original() as conn:
            yield conn
            raise RuntimeError('simulated commit failure')
    events = []
    monkeypatch.setattr(db, 'transaction', fail_commit)
    monkeypatch.setattr(main.event_bus, 'emit', lambda *args, **kw: events.append(args))
    with pytest.raises(RuntimeError, match='simulated commit failure'):
        client.post(resource + '/leases', json={'memory_mb': 800})
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == 0
    assert events == []


def test_bad_legacy_claim_does_not_create_capacity(client, resource):
    db.execute("INSERT INTO leases(id,resource_id,memory_mb,status) "
               "VALUES ('bad','admission-host/memory',-1000,'active')")
    r = client.post(resource + '/leases', json={'memory_mb': 1500})
    assert r.status_code == 409
    assert r.json()['detail']['code'] == 'invalid_existing_allocation'


def test_release_promotes_the_queued_intent_that_now_fits(client, resource):
    """0.12.98: the queue is promoted in the transaction that frees the bytes.
    (Before it, this test asserted the opposite: a release left the waiter queued,
    because nothing promoted — the gap the compute queue closes.)"""
    first = client.post(resource + '/leases', json={'memory_mb': 1000}).json()
    second = client.post(resource + '/leases', json={'memory_mb': 1000}).json()
    assert second['status'] == 'queued'
    r = client.delete('/api/v1/leases/' + first['lease_id'])
    assert r.status_code == 200
    assert r.json()['promoted'] == [second['lease_id']]
    assert db.query_one('SELECT status FROM leases WHERE id=?', (second['lease_id'],))['status'] == 'active'


@pytest.mark.parametrize('message,translated', [
    (None, True),  # exercise the real SQLite index after injecting unseen state
    ('UNIQUE constraint failed: leases.resource_id', True),
    ('UNIQUE constraint failed: leases.id', False),
    ('UNIQUE constraint failed: leases.resource_id_extra', False),
    ('UNIQUE constraint failed: leases.resource_id, leases.id', False),
    ('FOREIGN KEY constraint failed', False),
    ('NOT NULL constraint failed: leases.agent_id', False),
])
def test_only_resource_uniqueness_failure_translates_to_409(client, resource, monkeypatch,
                                                          message, translated):
    """Fault injection at INSERT, not a claim of a real cross-process race."""
    import main
    original = db.transaction
    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def execute(self, sql, params=()):
            if sql.startswith('INSERT INTO leases'):
                if message is not None:
                    raise sqlite3.IntegrityError(message)
                # Deliberately introduce a conflicting row after the precheck.
                # This is same-connection fault injection, not a process race.
                self.conn.execute("INSERT INTO leases(id,resource_id,exclusive,status,expires_at) "
                                  "VALUES ('schema-racer','admission-host/memory',1,'active',?)",
                                  ('2099-01-01T00:00:00+00:00',))
            return self.conn.execute(sql, params)
    @contextmanager
    def inject_failure():
        with original() as conn:
            yield Connection(conn)
    events = []
    monkeypatch.setattr(db, 'transaction', inject_failure)
    monkeypatch.setattr(main.event_bus, 'emit', lambda *args, **kw: events.append(args))
    if translated:
        response = client.post(resource + '/leases', json={'memory_mb': 100, 'exclusive': True})
        assert response.status_code == 409, response.text
        assert response.json()['detail'] == {
            'detail': 'Exclusive lease held by unknown' if message is not None else
                      'Exclusive lease held by None',
            'held_by': None,
            'expires_at': None if message is not None else '2099-01-01T00:00:00+00:00'}
    else:
        with pytest.raises(sqlite3.IntegrityError) as error:
            client.post(resource + '/leases', json={'memory_mb': 100, 'exclusive': True})
        assert str(error.value) == message
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == 0
    assert events == []


def test_api_index_describes_memory_validation_and_refusals(client):
    description = client.get('/api/v1/').json()['endpoints']['leases']['request']['description']
    for term in ('strict non-negative integer', 'true', '"100"', '1.5', '422',
                 'invalid_existing_allocation', 'invalid_resource_capacity'):
        assert term in description
