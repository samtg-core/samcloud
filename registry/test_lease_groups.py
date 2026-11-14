"""HTTP and real SQLite group recovery controls; no provider or production calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import pytest

import db
import main
from test_registry import client, _register_device  # noqa: F401

ROOT = '/api/v1/lease-groups'
PROOF = {'evidence_ref': 'test:backend-absence', 'evidence_sha256': 'a' * 64}


@pytest.fixture
def hosts(client):
    ids = []
    for n in range(3):
        device = f'group-host-{n}'
        assert _register_device(client, device, device).status_code == 200
        result = client.post('/api/v1/resources', json={
            'device_id': device, 'name': 'memory', 'type': 'memory', 'provider': 'test',
            'specs': {'ram_mb': 1000}, 'allocation_mode': 'shared'})
        assert result.status_code == 200, result.text
        resource = f'{device}/memory'
        db.execute("UPDATE resources SET expiry_policy='hold' WHERE id=?", (resource,))
        ids.append(resource)
    return ids


def intent(hosts, key='job-1'):
    return {'request_key': key, 'members': [
        {'resource_id': resource, 'memory_mb': 1000, 'ttl_seconds': 600}
        for resource in hosts]}


def create(client, hosts, key='job-1'):
    response = client.post(ROOT, json=intent(hosts, key))
    assert response.status_code == 200, response.text
    return response.json()


def command(client, group, action, **extra):
    return client.post(f"{ROOT}/{group['id']}/{action}",
                       json={'generation': group['generation'], **extra})


def ordinary(client, resource, **extra):
    response = client.post(f'/api/v1/resources/{resource}/leases',
                           json={'exclusive': True, 'wait': True, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def drain(client, group):
    for member in group['members']:
        if member['lease_id']:
            response = command(client, group, f"members/{member['lease_id']}/release", **PROOF)
            assert response.status_code == 200, response.text
    return client.get(f"{ROOT}/{group['id']}").json()


def test_full_group_uses_ordinary_leases_and_needs_all_before_serving(client, hosts):
    holder = ordinary(client, hosts[2])
    group = create(client, hosts)
    assert group['phase'] == 'acquiring'
    assert [m['status'] for m in group['members']] == ['active', 'active', 'queued']
    assert command(client, group, 'serving', **PROOF).status_code == 409
    assert client.delete('/api/v1/leases/' + holder['lease_id']).status_code == 200
    group = client.get(f"{ROOT}/{group['id']}").json()
    assert group['phase'] == 'admitted'
    assert command(client, group, 'serving', **PROOF).json()['phase'] == 'serving'
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == 4


def test_wait_for_first_host_leaves_other_hosts_working_and_keeps_queue_order(client, hosts):
    holder = ordinary(client, hosts[0])
    older = ordinary(client, hosts[0])
    group = create(client, hosts)
    assert [m['status'] for m in group['members']] == ['queued', 'not_requested', 'not_requested']
    assert group['members'][0]['queue_position'] == 2
    assert db.query_one('SELECT expires_at FROM leases WHERE id=?',
                        (group['members'][0]['lease_id'],))['expires_at'] is None
    # An unrelated user keeps working on a later host; no group priority boost.
    other = ordinary(client, hosts[1])
    assert other['status'] == 'active'
    assert client.delete('/api/v1/leases/' + holder['lease_id']).json()['promoted'] == [older['lease_id']]
    assert command(client, group, 'advance').json()['members'][1]['status'] == 'not_requested'
    client.delete('/api/v1/leases/' + older['lease_id'])
    # GET observes promotion; it never takes another host by itself.
    assert client.get(f"{ROOT}/{group['id']}").json()['members'][1]['status'] == 'not_requested'
    advanced = command(client, group, 'advance').json()
    assert [m['status'] for m in advanced['members']] == ['active', 'queued', 'active']


def test_lost_create_response_replays_same_bindings_and_events(client, hosts):
    group = create(client, hosts)
    assert create(client, hosts) == group
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == 3
    assert db.query_one('SELECT COUNT(*) n FROM lease_groups')['n'] == 1
    changed = intent(hosts)
    changed['members'][0]['ttl_seconds'] = 601
    response = client.post(ROOT, json=changed)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'group_key_conflict'


def test_concurrent_coordinators_share_one_group_and_one_member_set(client, hosts):
    barrier = threading.Barrier(8)
    def send(_):
        barrier.wait(timeout=10)
        return client.post(ROOT, json=intent(hosts))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(send, range(8)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert len({r.json()['id'] for r in results}) == 1
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == 3


def test_group_fifo_blocks_cross_group_deadlock_without_jumping_host_queues(client, hosts):
    first = create(client, hosts)
    second = create(client, list(reversed(hosts)), 'job-2')
    third = create(client, hosts, 'job-3')
    assert second['phase'] == third['phase'] == 'pending'
    assert third['queue_position'] == 2
    assert all(m['lease_id'] is None for m in second['members'])
    assert command(client, third, 'advance').json()['phase'] == 'pending'
    assert drain(client, first)['phase'] == 'released'
    assert command(client, third, 'advance').json()['phase'] == 'pending'
    assert command(client, second, 'advance').json()['phase'] == 'admitted'


def test_release_cancels_waiters_but_active_members_need_cleanup(client, hosts):
    ordinary(client, hosts[1])
    group = create(client, hosts)
    waiting = group['members'][1]['lease_id']
    response = command(client, group, 'release')
    assert response.json()['phase'] == 'releasing'
    assert db.query_one('SELECT status FROM leases WHERE id=?', (waiting,))['status'] == 'cancelled'
    assert all(m['status'] == 'active' for m in response.json()['members'] if m['ordinal'] != 1)
    assert drain(client, group)['phase'] == 'released'
    assert command(client, group, 'advance').status_code == 409
    # Replaying the original creation cannot acquire a terminal group anew.
    assert create(client, hosts)['phase'] == 'released'


def test_pending_group_can_cancel_without_displacing_active_group(client, hosts):
    first = create(client, hosts)
    pending = create(client, hosts, 'pending')
    assert command(client, pending, 'release').json()['phase'] == 'cancelled'
    assert client.get(f"{ROOT}/{first['id']}").json()['phase'] == 'admitted'


def test_hold_expiry_keeps_unknown_member_until_exact_cleanup_and_settlement(client, hosts):
    group = create(client, hosts)
    lease_id = group['members'][0]['lease_id']
    db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (lease_id,))
    unknown = client.get(f"{ROOT}/{group['id']}").json()
    assert unknown['phase'] == 'uncertain'
    assert unknown['members'][0]['status'] == 'expired'
    assert unknown['members'][0]['settled_at'] is None
    assert command(client, group, 'serving', **PROOF).status_code == 409
    assert command(client, group, 'advance').status_code == 409
    waiter = ordinary(client, hosts[0])
    assert waiter['status'] == 'queued'
    releasing = command(client, group, 'release').json()
    assert releasing['phase'] == 'uncertain'
    response = command(client, group, f'members/{lease_id}/release', **PROOF)
    assert response.status_code == 200, response.text
    receipt = response.json()['members'][0]['release_receipt']
    assert receipt['lease_id'] == lease_id and receipt['settled_at']
    assert db.query_one('SELECT status FROM leases WHERE id=?', (waiter['lease_id'],))['status'] == 'active'
    assert drain(client, group)['phase'] == 'released'


def test_member_removed_by_admin_is_uncertain_and_never_reallocated(client, hosts):
    group = create(client, hosts)
    lease_id = group['members'][0]['lease_id']
    db.execute('DELETE FROM leases WHERE id=?', (lease_id,))
    response = client.get(f"{ROOT}/{group['id']}").json()
    assert response['phase'] == 'uncertain'
    assert response['members'][0]['lease_id'] == lease_id
    assert response['members'][0]['status'] == 'missing'
    assert command(client, group, 'advance').status_code == 409
    assert command(client, group, f'members/{lease_id}/release', **PROOF).status_code == 409
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == 2


def test_ordinary_release_without_group_proof_does_not_end_group(client, hosts):
    group = create(client, hosts)
    for member in group['members']:
        client.delete('/api/v1/leases/' + member['lease_id'])
    assert command(client, group, 'release').json()['phase'] == 'uncertain'
    assert drain(client, group)['phase'] == 'released'


def test_generation_fence_rejects_old_coordinator_without_freeing_active_hosts(client, hosts):
    group = create(client, hosts)
    fenced = command(client, group, 'fence', reason='coordinator replaced').json()
    assert fenced['generation'] == 2 and fenced['phase'] == 'releasing'
    assert all(m['status'] == 'active' for m in fenced['members'])
    for action, extra in [('advance', {}), ('serving', PROOF), ('release', {}),
                           (f"members/{group['members'][0]['lease_id']}/release", PROOF)]:
        response = command(client, group, action, **extra)
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'group_generation_changed'
    assert drain(client, fenced)['phase'] == 'released'


def test_cleanup_attestation_replay_is_idempotent_and_cannot_be_replaced(client, hosts):
    group = create(client, hosts)
    action = f"members/{group['members'][0]['lease_id']}/release"
    first = command(client, group, action, **PROOF)
    assert first.status_code == 200, first.text
    assert command(client, group, action, **PROOF).json() == first.json()
    assert command(client, group, action, **{**PROOF, 'evidence_sha256': 'b' * 64}).status_code == 409


@pytest.mark.parametrize('change', ['free', 'same_host', 'missing', 'too_large', 'bad_service'])
def test_invalid_later_member_has_no_partial_intent_or_lease(client, hosts, change):
    payload = intent(hosts)
    if change == 'free':
        db.execute("UPDATE resources SET expiry_policy='free' WHERE id=?", (hosts[-1],))
    elif change == 'same_host':
        payload['members'][-1]['resource_id'] = hosts[0]
    elif change == 'missing':
        payload['members'][-1]['resource_id'] = 'missing/memory'
    elif change == 'too_large':
        payload['members'][-1]['memory_mb'] = 1001
    else:
        payload['members'][-1]['service_id'] = 'missing-service'
    assert client.post(ROOT, json=payload).status_code in (404, 409, 422)
    for table in ('leases', 'lease_groups', 'lease_group_members', 'lease_group_events'):
        assert db.query_one(f'SELECT COUNT(*) n FROM {table}')['n'] == 0


def test_commit_failure_rolls_back_all_members_and_never_publishes(client, hosts, monkeypatch):
    original = db.transaction
    @contextmanager
    def fail_commit():
        with original() as conn:
            yield conn
            raise RuntimeError('simulated group commit failure')
    events = []
    monkeypatch.setattr(db, 'transaction', fail_commit)
    monkeypatch.setattr(main.event_bus, 'emit', lambda *args, **kw: events.append(args))
    with pytest.raises(RuntimeError, match='simulated group commit failure'):
        client.post(ROOT, json=intent(hosts))
    for table in ('leases', 'lease_groups', 'lease_group_members', 'lease_group_events'):
        assert db.query_one(f'SELECT COUNT(*) n FROM {table}')['n'] == 0
    assert events == []


def test_events_are_durable_paginated_and_published_only_after_commit(client, hosts, monkeypatch):
    seen = []
    def observe(kind, target, data, **kw):
        if kind.startswith('lease_group.'):
            seen.append(db.query_one('SELECT sequence FROM lease_group_events WHERE sequence=?',
                                     (data['sequence'],))['sequence'])
        elif kind == 'lease.granted':
            # Even the first member event sees the complete binding set.
            assert db.query_one('SELECT COUNT(*) n FROM lease_group_members WHERE lease_id IS NOT NULL')['n'] == 3
    monkeypatch.setattr(main.event_bus, 'emit', observe)
    group = create(client, hosts)
    events = client.get(f"{ROOT}/{group['id']}/events?limit=2").json()
    assert len(events) == 2
    tail = client.get(f"{ROOT}/{group['id']}/events?after={events[-1]['sequence']}").json()
    assert seen == [e['sequence'] for e in events + tail]
    assert client.get(f"{ROOT}/{group['id']}/events?after=-1").status_code == 400
    assert client.get(f"{ROOT}/{group['id']}/events?limit=-1").status_code == 400


def test_fence_preserves_acknowledged_member_cleanup(client, hosts):
    group = create(client, hosts)
    member = group['members'][0]
    released = command(client, group, f"members/{member['lease_id']}/release", **PROOF).json()
    saved = released['members'][0]['cleanup_evidence']
    fenced = command(client, group, 'fence', reason='coordinator recovery').json()
    result = drain(client, fenced)
    assert result['phase'] == 'released'
    assert result['members'][0]['cleanup_evidence'] == saved


def test_all_hosts_must_be_visible_and_only_owner_can_mutate(client, hosts):
    from test_auth_boundaries import user
    from events import Event
    owner, _ = user(client, 'group-owner', {'all-hosts': 'viewer'})
    reader, _ = user(client, 'group-reader', {'all-hosts': 'viewer'})
    partial, _ = user(client, 'group-partial', {'one-host': 'viewer'})
    for resource in hosts:
        device = resource.split('/')[0]
        assert client.post('/api/v1/groups/all-hosts/devices', json={'device_id': device}).status_code == 200
    assert client.post('/api/v1/groups/one-host/devices', json={
        'device_id': hosts[0].split('/')[0]}).status_code == 200
    assert partial.post(ROOT, json=intent(hosts)).status_code == 404
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == 0
    group = create(owner, hosts)
    path = f"{ROOT}/{group['id']}"
    assert reader.get(path).status_code == 200
    assert partial.get(path).status_code == 404
    assert partial.get(path + '/events').status_code == 404
    assert command(reader, group, 'advance').status_code == 403
    event = Event('lease_group.changed', group['id'], {'group_id': group['id']})
    assert main._can_see_event(main.get_auth(owner._headers['Authorization']), event)
    assert main._can_see_event(main.get_auth(reader._headers['Authorization']), event)
    assert not main._can_see_event(main.get_auth(partial._headers['Authorization']), event)
    # Losing visibility stops further serving/acquisition, but not cleanup.
    db.execute("DELETE FROM group_devices WHERE group_id='all-hosts'")
    assert owner.get(path).status_code == 200
    assert reader.get(path).status_code == 404
    assert command(owner, group, 'advance').status_code == 404
    assert command(owner, group, 'serving', **PROOF).status_code == 404
    assert drain(owner, group)['phase'] == 'released'


def test_schema_upgrade_is_repeatable_and_preserves_existing_leases(client, hosts):
    holder = ordinary(client, hosts[0])
    # Model a database from before the group tables existed.
    with db.transaction() as conn:
        for table in ('lease_group_events', 'lease_group_members', 'lease_groups'):
            conn.execute(f'DROP TABLE {table}')
    db.init_db()
    db.init_db()
    assert db.query_one('SELECT status FROM leases WHERE id=?', (holder['lease_id'],))['status'] == 'active'
    group = create(client, hosts)
    assert group['members'][0]['status'] == 'queued'


@pytest.mark.parametrize('bad', [None, 0, -1, True, '600', 21601])
def test_invalid_member_lifetime_never_creates_partial_work(client, hosts, bad):
    payload = intent(hosts)
    payload['members'][-1]['ttl_seconds'] = bad
    assert client.post(ROOT, json=payload).status_code == 422
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == 0


def test_concurrent_advance_after_promotion_never_duplicates_other_members(client, hosts):
    holder = ordinary(client, hosts[0])
    group = create(client, hosts)
    client.delete('/api/v1/leases/' + holder['lease_id'])
    barrier = threading.Barrier(8)
    def send(_):
        barrier.wait(timeout=10)
        return command(client, group, 'advance')
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(send, range(8)))
    assert all(r.status_code == 200 and r.json()['phase'] == 'admitted' for r in results)
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == 4
