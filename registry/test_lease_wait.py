"""GET /leases/{id}/wait can't stall the plane (claude-producer, task 1472).

The wait used to sleep on a request thread for its whole timeout, so about 40 of them,
from any token holder, took every thread anyio gives sync routes and the rest of the API
stopped answering. Now it sleeps on the event loop and borrows a thread only to look, and
a caller holds at most LEASE_WAIT_PER_CALLER waits (LEASE_WAIT_TOTAL in all); beyond is 429.
"""
import threading
import time

import main
from test_registry import client  # noqa: F401 — fixture
from test_lease_admission import resource  # noqa: F401 — 1000 MB shared 'admission-host/memory'


def queued(client, resource):
    """A holder of the whole resource and one waiter behind it."""
    client.post(resource + '/leases', json={'exclusive': True, 'ttl_seconds': 600})
    r = client.post(resource + '/leases', json={'exclusive': True, 'wait_s': 600})
    assert r.status_code == 200 and r.json()['status'] == 'queued', r.text
    return r.json()['lease_id']


def open_waits(client, lease_id, n, timeout_s):
    got = []
    def one():
        got.append(client.get(f'/api/v1/leases/{lease_id}/wait?timeout_s={timeout_s}'))
    threads = [threading.Thread(target=one) for _ in range(n)]
    for t in threads:
        t.start()
    return threads, got


def test_more_waits_than_request_threads_leave_the_api_answering(client, resource, monkeypatch):
    monkeypatch.setattr(main, 'LEASE_WAIT_PER_CALLER', 100, raising=False)   # raising: so it also runs on the old wait
    monkeypatch.setattr(main, 'LEASE_WAIT_TOTAL', 100, raising=False)
    waiter = queued(client, resource)
    threads, got = open_waits(client, waiter, 50, 5)     # anyio lends sync routes 40 threads
    time.sleep(1.5)
    t = time.monotonic()
    r = client.get('/api/v1/leases/' + waiter)            # a sync route
    took = time.monotonic() - t
    still_open = sum(th.is_alive() for th in threads)
    for th in threads:
        th.join(timeout=30)
    assert r.status_code == 200 and took < 2, (r.status_code, took)
    assert still_open == 50, still_open                   # it answered while all 50 were held
    assert [x.status_code for x in got] == [200] * 50
    assert all(x.json()['waiting'] is True for x in got)


def test_a_caller_past_its_cap_is_429_and_a_finished_wait_gives_its_place_back(client, resource, monkeypatch):
    monkeypatch.setattr(main, 'LEASE_WAIT_PER_CALLER', 2)
    waiter = queued(client, resource)
    threads, got = open_waits(client, waiter, 2, 3)
    time.sleep(1)
    r = client.get(f'/api/v1/leases/{waiter}/wait?timeout_s=1')
    for th in threads:
        th.join(timeout=30)
    assert r.status_code == 429 and r.headers.get('retry-after') == '5', (r.status_code, r.headers)
    assert '2 per caller' in r.json()['detail']
    assert [x.status_code for x in got] == [200, 200]
    after = client.get(f'/api/v1/leases/{waiter}/wait?timeout_s=0')
    assert after.status_code == 200 and after.json()['waiting'] is True
    assert main._lease_waits == {}


def test_the_plane_wide_cap_holds_across_callers(client, resource, monkeypatch):
    monkeypatch.setattr(main, 'LEASE_WAIT_TOTAL', 1)
    monkeypatch.setattr(main, '_lease_waits', {'someone-else': 1})
    waiter = queued(client, resource)
    r = client.get(f'/api/v1/leases/{waiter}/wait?timeout_s=0')
    assert r.status_code == 429 and '1 in all' in r.json()['detail'], r.text


def test_a_wait_that_fails_gives_its_place_back(client, resource, monkeypatch):
    monkeypatch.setattr(main, 'LEASE_WAIT_PER_CALLER', 1)
    for _ in range(3):
        r = client.get('/api/v1/leases/lease_nope/wait?timeout_s=0')
        assert r.status_code == 404 and 'Lease lease_nope not found' in r.json()['detail']
    assert main._lease_waits == {}


def test_a_wait_still_answers_when_the_lease_is_granted(client, resource):
    holder = client.post(resource + '/leases', json={'exclusive': True, 'ttl_seconds': 600}).json()
    waiter = client.post(resource + '/leases', json={'exclusive': True, 'wait_s': 600}).json()['lease_id']
    threads, got = open_waits(client, waiter, 1, 10)
    time.sleep(1.2)
    client.delete('/api/v1/leases/' + holder['lease_id'])
    t = time.monotonic()
    threads[0].join(timeout=15)
    assert got and got[0].status_code == 200 and got[0].json()['status'] == 'active', got
    assert got[0].json()['waiting'] is False and time.monotonic() - t < 3
