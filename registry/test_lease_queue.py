"""The lease queue is the compute queue (0.12.98): a request the resource cannot
grant now waits in order and is promoted inside the transaction that frees the
bytes. HTTP against a temporary DB, via the registry fixtures."""
from datetime import datetime, timedelta, timezone

import pytest

import db
from test_registry import client  # noqa: F401 — fixture
from test_lease_admission import resource  # noqa: F401 — 1000 MB shared 'admission-host/memory'

RID = 'admission-host/memory'


def lease(client, resource, **body):
    r = client.post(resource + '/leases', json=body)
    assert r.status_code == 200, r.text
    return r.json()


def row(lease_id):
    return db.query_one('SELECT * FROM leases WHERE id=?', (lease_id,))


def backdate(lease_id, seconds=60):
    past = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    db.execute('UPDATE leases SET expires_at=? WHERE id=?', (past, lease_id))


def test_exclusive_waiter_is_queued_and_promoted_on_release(client, resource):
    holder = lease(client, resource, exclusive=True, ttl_seconds=600)
    waiter = lease(client, resource, exclusive=True, ttl_seconds=900, wait_s=600)
    assert waiter['status'] == 'queued' and waiter['queue_position'] == 1
    assert waiter['wait_until'] > datetime.now(timezone.utc).isoformat()
    got = client.get('/api/v1/leases/' + waiter['lease_id']).json()
    assert got['status'] == 'queued' and got['queue_position'] == 1
    released = client.delete('/api/v1/leases/' + holder['lease_id']).json()
    assert released['promoted'] == [waiter['lease_id']]
    got = client.get('/api/v1/leases/' + waiter['lease_id']).json()
    assert got['status'] == 'active' and got['exclusive'] is True and got['queue_position'] is None
    # Promoted with the TTL it asked for, counted from the grant, not a default.
    expires = datetime.fromisoformat(got['expires_at'])
    assert timedelta(seconds=800) < expires - datetime.now(timezone.utc) <= timedelta(seconds=900)


def test_without_wait_an_exclusive_conflict_is_still_a_409(client, resource):
    lease(client, resource, exclusive=True, service_id=None)
    r = client.post(resource + '/leases', json={'exclusive': True})
    assert r.status_code == 409
    assert 'held_by' in r.json()['detail']
    assert db.query_one("SELECT COUNT(*) n FROM leases WHERE status='queued'")['n'] == 0


def test_head_of_line_a_newcomer_that_fits_does_not_pass_a_waiter(client, resource):
    first = lease(client, resource, memory_mb=600)
    big = lease(client, resource, memory_mb=600, wait_s=600)
    assert big['status'] == 'queued'
    refused = client.post(resource + '/leases', json={'memory_mb': 100})
    assert refused.status_code == 409
    assert refused.json()['detail']['code'] == 'queue_ahead'
    assert refused.json()['detail']['queue_length'] == 1
    small = lease(client, resource, memory_mb=100, wait_s=600)
    assert small['status'] == 'queued' and small['queue_position'] == 2
    released = client.delete('/api/v1/leases/' + first['lease_id']).json()
    assert released['promoted'] == [big['lease_id'], small['lease_id']]
    assert db.query_one("SELECT SUM(memory_mb) n FROM leases WHERE status='active'")['n'] == 700


def test_a_larger_waiter_at_the_head_holds_the_smaller_ones_behind_it(client, resource):
    first = lease(client, resource, memory_mb=600)
    big = lease(client, resource, memory_mb=900, wait_s=600)
    small = lease(client, resource, memory_mb=100, wait_s=600)
    third = lease(client, resource, memory_mb=300, wait_s=600)   # would fit beside first; a waiter is ahead
    assert third['status'] == 'queued' and third['queue_position'] == 3
    # first leaves: 900 fits, then 100 fits in what is left, 300 does not — the walk stops there.
    released = client.delete('/api/v1/leases/' + first['lease_id']).json()
    assert released['promoted'] == [big['lease_id'], small['lease_id']]
    got = client.get(resource).json()
    assert [q['lease_id'] for q in got['queue']] == [third['lease_id']]
    assert got['queue'][0]['position'] == 1
    assert client.delete('/api/v1/leases/' + big['lease_id']).json()['promoted'] == [third['lease_id']]


def test_priority_orders_the_queue(client, resource):
    holder = lease(client, resource, memory_mb=1000)
    normal = lease(client, resource, memory_mb=500, wait_s=600)
    critical = lease(client, resource, memory_mb=500, wait_s=600, priority='critical')
    assert critical['queue_position'] == 1
    assert client.get('/api/v1/leases/' + normal['lease_id']).json()['queue_position'] == 2
    released = client.delete('/api/v1/leases/' + holder['lease_id']).json()
    assert released['promoted'] == [critical['lease_id'], normal['lease_id']]


def test_a_waiter_that_leaves_frees_its_place(client, resource):
    lease(client, resource, memory_mb=1000)
    gone = lease(client, resource, memory_mb=500, wait_s=600)
    stays = lease(client, resource, memory_mb=500, wait_s=600)
    assert stays['queue_position'] == 2
    backdate(gone['lease_id'])
    got = client.get(resource).json()
    assert [q['lease_id'] for q in got['queue']] == [stays['lease_id']]
    assert got['queue'][0]['position'] == 1
    expired = row(gone['lease_id'])
    assert expired['status'] == 'expired' and expired['expiry_reason'] == 'wait_expired'
    assert client.get('/api/v1/leases/' + gone['lease_id']).json()['status'] == 'expired'


def test_renew_while_queued_extends_the_wait_and_grants_nothing(client, resource):
    lease(client, resource, memory_mb=1000)
    waiter = lease(client, resource, memory_mb=500, wait_s=60)
    r = client.post('/api/v1/leases/' + waiter['lease_id'] + '/renew', json={'ttl_seconds': 1200})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['status'] == 'queued' and body['reason'] == 'waiting'
    assert body['wait_until'] > waiter['wait_until']
    assert body['extended_by_s'] >= 1100
    assert row(waiter['lease_id'])['status'] == 'queued'


def test_wait_long_poll_answers_when_the_lease_leaves_the_queue(client, resource):
    holder = lease(client, resource, exclusive=True, ttl_seconds=600)
    waiter = lease(client, resource, exclusive=True, wait_s=600)
    still = client.get('/api/v1/leases/' + waiter['lease_id'] + '/wait?timeout_s=1').json()
    assert still['status'] == 'queued' and still['waiting'] is True and still['queue_position'] == 1
    client.delete('/api/v1/leases/' + holder['lease_id'])
    done = client.get('/api/v1/leases/' + waiter['lease_id'] + '/wait?timeout_s=5').json()
    assert done['status'] == 'active' and done['waiting'] is False


def test_impossible_request_is_refused_not_queued(client, resource):
    for body in ({'memory_mb': 1500}, {'memory_mb': 1500, 'wait_s': 600}):
        r = client.post(resource + '/leases', json=body)
        assert r.status_code == 409, r.text
        assert r.json()['detail']['code'] == 'exceeds_capacity'
        assert r.json()['detail']['total_mb'] == 1000
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == 0


def test_legacy_over_bytes_request_still_queues_bounded_by_the_default_wait(client, resource):
    lease(client, resource, memory_mb=1000)
    waiter = lease(client, resource, memory_mb=500)
    assert waiter['status'] == 'queued' and waiter['queue_position'] == 1
    until = datetime.fromisoformat(waiter['wait_until'])
    assert timedelta(seconds=3500) < until - datetime.now(timezone.utc) <= timedelta(seconds=3600)


def test_time_passing_promotes_the_head(client, resource):
    holder = lease(client, resource, exclusive=True, ttl_seconds=600)
    waiter = lease(client, resource, memory_mb=200, wait_s=600)
    backdate(holder['lease_id'])
    got = client.get(resource).json()
    assert got['queue'] == []
    assert {l['id']: l['status'] for l in got['leases']} == {waiter['lease_id']: 'active'}
    assert row(holder['lease_id'])['status'] == 'expired'


def test_promotion_is_published_after_commit_and_named(client, resource, monkeypatch):
    import main
    seen = []
    def emit(kind, subject, payload, **kw):
        if kind == 'lease.granted' and payload.get('promoted'):
            seen.append((payload, row(subject)['status']))
    monkeypatch.setattr(main.event_bus, 'emit', emit)
    holder = lease(client, resource, memory_mb=1000)
    waiter = lease(client, resource, memory_mb=400, wait_s=600, service_id=None)
    client.delete('/api/v1/leases/' + holder['lease_id'])
    assert len(seen) == 1
    payload, status = seen[0]
    assert status == 'active'
    assert payload['lease_id'] == waiter['lease_id'] and payload['resource_id'] == RID
    assert payload['memory_mb'] == 400 and payload['waited_s'] >= 0


def test_a_released_queued_request_leaves_the_queue(client, resource):
    lease(client, resource, memory_mb=1000)
    first = lease(client, resource, memory_mb=500, wait_s=600)
    second = lease(client, resource, memory_mb=500, wait_s=600)
    r = client.delete('/api/v1/leases/' + first['lease_id'])
    assert r.status_code == 200 and r.json()['promoted'] == []
    assert r.json()['status'] == 'cancelled'
    assert row(first['lease_id'])['status'] == 'cancelled'   # it never held bytes
    assert client.get('/api/v1/leases/' + second['lease_id']).json()['queue_position'] == 1


def test_resource_read_shows_the_queue_and_the_dashboard_counts_it(client, resource):
    lease(client, resource, memory_mb=1000, service_id=None)
    waiter = lease(client, resource, memory_mb=300, wait_s=600, priority='high')
    got = client.get(resource).json()
    assert got['queue'] == [{
        'lease_id': waiter['lease_id'], 'position': 1, 'service_id': None,
        'agent_id': got['queue'][0]['agent_id'], 'memory_mb': 300, 'exclusive': False,
        'priority': 'high', 'queued_at': got['queue'][0]['queued_at'],
        'wait_until': waiter['wait_until']}]
    dash = [r for r in client.get('/api/v1/resources/dashboard').json() if r['id'] == RID][0]
    assert dash['leases'] == 1 and dash['queued'] == 1


def test_exclusive_resource_queues_a_second_holder_and_promotes_it_exclusive(client):
    from test_registry import _register_device
    _register_device(client, 'mutex-host', 'mutex-host')
    r = client.post('/api/v1/resources', json={
        'device_id': 'mutex-host', 'name': 'seat', 'type': 'accelerator', 'provider': 'test',
        'specs': {}, 'allocation_mode': 'exclusive'})
    assert r.status_code == 200, r.text
    res = '/api/v1/resources/mutex-host/seat'
    holder = lease(client, res, ttl_seconds=300)
    assert holder['status'] == 'active'
    waiter = lease(client, res, wait_s=600)
    assert waiter['status'] == 'queued'
    assert client.delete('/api/v1/leases/' + holder['lease_id']).json()['promoted'] == [waiter['lease_id']]
    got = row(waiter['lease_id'])
    assert got['status'] == 'active' and got['exclusive'] == 1
    assert got['expires_at'] is not None     # a promoted mutex still gets the default TTL


def test_an_expired_holder_reads_expired_not_released_and_is_published(client, resource, monkeypatch):
    """Expiry is the registry giving up on a holder that stopped renewing. Its
    bytes are free in the ledger; whether its processes are gone is not known."""
    import main
    seen = []
    monkeypatch.setattr(main.event_bus, 'emit',
                        lambda kind, subject, payload, **kw: seen.append((kind, payload)))
    holder = lease(client, resource, memory_mb=800, ttl_seconds=300)
    waiter = lease(client, resource, memory_mb=500, wait_s=600)
    backdate(holder['lease_id'])
    got = client.get('/api/v1/leases/' + holder['lease_id']).json()
    assert got['status'] == 'expired' and got['expiry_reason'] == 'ttl_expired'
    assert got['released_at'] is None
    expired = [p for k, p in seen if k == 'lease.expired']
    assert len(expired) == 1 and expired[0]['lease_id'] == holder['lease_id']
    assert expired[0]['reason'] == 'ttl_expired' and 'not proof' in expired[0]['note']
    granted = [p for k, p in seen if k == 'lease.granted' and p.get('promoted')]
    assert [p['lease_id'] for p in granted] == [waiter['lease_id']]


def test_lost_replies_are_recovered_by_re_reading_and_release_is_idempotent(client, resource):
    from test_registry import _register_device  # noqa: F401
    assert client.post('/api/v1/services', json={
        'device_id': 'admission-host', 'name': 'lane', 'port': 1,
        'description': 'test'}).status_code in (200, 201)
    granted = lease(client, resource, memory_mb=400, service_id='admission-host/lane')
    # The grant reply was lost: the row is found again by resource and service.
    mine = client.get('/api/v1/leases?resource_id=admission-host/memory&service_id=admission-host/lane&status=active').json()
    rows = mine if isinstance(mine, list) else mine.get('leases', mine)
    assert [r['id'] for r in rows] == [granted['lease_id']]
    first = client.delete('/api/v1/leases/' + granted['lease_id'])
    again = client.delete('/api/v1/leases/' + granted['lease_id'])
    assert first.status_code == 200 and first.json()['status'] == 'released'
    assert again.status_code == 200 and again.json()['status'] == 'released'
    assert again.json()['promoted'] == []
    assert client.get('/api/v1/leases/' + granted['lease_id']).json()['status'] == 'released'


@pytest.mark.parametrize('iteration', range(5))
def test_cancellation_racing_promotion_leaves_no_orphaned_bytes(client, resource, iteration):
    """The holder releases while the waiter cancels. Serialized either way: the
    waiter ends cancelled (never held) or released (promoted, then given back),
    and no bytes stay reserved for a process that never ran."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    holder = lease(client, resource, memory_mb=1000)
    waiter = lease(client, resource, memory_mb=600, wait_s=600)
    barrier = threading.Barrier(2)
    def go(lease_id):
        barrier.wait(timeout=10)
        return client.delete('/api/v1/leases/' + lease_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(go, [holder['lease_id'], waiter['lease_id']]))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    final = row(waiter['lease_id'])['status']
    assert final in ('cancelled', 'released'), final
    assert db.query_one("SELECT COUNT(*) n FROM leases WHERE status IN ('active','queued')")['n'] == 0
    assert client.get(resource).json()['available_memory_mb'] == 1000


# --- a wait with no deadline (no timeouts as a default at any layer) ---

def test_an_indefinite_wait_has_no_deadline_and_the_reaper_leaves_it(client, resource):
    holder = lease(client, resource, memory_mb=1000)
    waiter = lease(client, resource, memory_mb=500, wait=True, ttl_seconds=300)
    assert waiter['status'] == 'queued' and waiter['queue_position'] == 1
    assert waiter['wait_until'] is None
    assert row(waiter['lease_id'])['expires_at'] is None
    # However long it has waited, a read reaps nothing of it.
    long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    db.execute('UPDATE leases SET created_at=? WHERE id=?', (long_ago, waiter['lease_id']))
    view = client.get(resource).json()
    assert [q['lease_id'] for q in view['queue']] == [waiter['lease_id']]
    assert view['queue'][0]['wait_until'] is None
    assert row(waiter['lease_id'])['status'] == 'queued'
    # It leaves the queue the way every waiter does: promoted in the release.
    released = client.delete('/api/v1/leases/' + holder['lease_id']).json()
    assert released['promoted'] == [waiter['lease_id']]
    got = client.get('/api/v1/leases/' + waiter['lease_id']).json()
    assert got['status'] == 'active' and got['expires_at'] is not None   # the TTL starts at the grant


def test_renewing_an_indefinite_wait_changes_nothing(client, resource):
    lease(client, resource, memory_mb=1000)
    waiter = lease(client, resource, memory_mb=500, wait=True)
    r = client.post('/api/v1/leases/' + waiter['lease_id'] + '/renew', json={'ttl_seconds': 600})
    assert r.status_code == 200, r.text
    assert r.json()['reason'] == 'waiting' and r.json()['wait_until'] is None
    assert r.json()['extended_by_s'] == 0
    assert row(waiter['lease_id'])['expires_at'] is None and row(waiter['lease_id'])['status'] == 'queued'


def test_an_indefinite_waiter_leaves_by_its_own_delete(client, resource):
    lease(client, resource, memory_mb=1000)
    waiter = lease(client, resource, memory_mb=500, wait=True)
    r = client.delete('/api/v1/leases/' + waiter['lease_id'])
    assert r.status_code == 200 and r.json()['status'] == 'cancelled'
    assert client.get(resource).json()['queue'] == []


def test_wait_true_with_wait_s_keeps_the_deadline(client, resource):
    lease(client, resource, memory_mb=1000)
    waiter = lease(client, resource, memory_mb=500, wait=True, wait_s=60)
    assert waiter['wait_until'] is not None
    backdate(waiter['lease_id'])
    client.get(resource)
    assert row(waiter['lease_id'])['status'] == 'expired'
    assert row(waiter['lease_id'])['expiry_reason'] == 'wait_expired'


def test_wait_is_a_strict_boolean_and_false_is_absent(client, resource):
    lease(client, resource, memory_mb=1000)
    assert client.post(resource + '/leases', json={'memory_mb': 500, 'wait': 'yes'}).status_code == 422
    assert client.post(resource + '/leases', json={'memory_mb': 500, 'wait': 1}).status_code == 422
    # false is the same as not asking: a shared request over the bytes takes the legacy bounded wait.
    legacy = lease(client, resource, memory_mb=500, wait=False)
    assert legacy['status'] == 'queued' and legacy['wait_until'] is not None
