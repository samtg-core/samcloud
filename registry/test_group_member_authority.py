"""Host-local authority through the real HTTP/SQLite path, no provider calls."""
from datetime import datetime
import json

import pytest

import db
import main
from events import EventBus
from test_auth_boundaries import user
from test_lease_groups import hosts, create, command, ordinary, PROOF, ROOT  # noqa: F401
from test_registry import client, AuthedClient  # noqa: F401


@pytest.fixture
def local_reader(client, hosts):
    reader, _ = user(client, 'host-local-reader', {'one-member': 'viewer'})
    assert client.post('/api/v1/groups/one-member/devices', json={
        'device_id': hosts[0].split('/')[0]}).status_code == 200
    return reader


def path(group, ordinal=0):
    return f"{ROOT}/{group['id']}/members/{group['members'][ordinal]['lease_id']}/authority"


def observed(reader, group):
    response = reader.get(path(group))
    assert response.status_code == 200, response.text
    return response.json()


def test_host_reads_only_its_member_without_full_group_visibility(client, hosts, local_reader):
    group = create(client, hosts, 'private-request-key')
    command(client, group, 'serving', **PROOF)
    view = observed(local_reader, group)
    assert local_reader.get(path(group)).headers['cache-control'] == 'no-store'
    assert view['authoritative'] is True and view['phase'] == 'serving'
    assert view['group_id'] == group['id'] and view['generation'] == 1
    assert view['member']['resource_id'] == hosts[0]
    assert view['member']['lease']['id'] == group['members'][0]['lease_id']
    assert view['member']['lease']['exclusive'] is True
    assert datetime.fromisoformat(view['observed_at']).utcoffset() is not None
    encoded = json.dumps(view)
    for hidden in [hosts[1], hosts[2], group['members'][1]['lease_id'],
                   group['members'][2]['lease_id'], 'private-request-key', PROOF['evidence_ref']]:
        assert hidden not in encoded
    assert set(view) == {'group_id', 'owner_id', 'generation', 'phase',
                         'release_requested', 'observed_at', 'authoritative', 'member'}
    assert local_reader.get(f"{ROOT}/{group['id']}").status_code == 404
    assert local_reader.get(f"{ROOT}/{group['id']}/events").status_code == 404
    assert command(local_reader, group, 'release').status_code == 404
    assert command(local_reader, group, f"members/{group['members'][0]['lease_id']}/release",
                   **PROOF).status_code == 404


def test_foreign_nonmember_and_missing_ids_have_same_private_refusal(client, hosts, local_reader):
    group = create(client, hosts)
    another = ordinary(client, hosts[0])
    paths = [path(group, 1), path(group, 2),
             f"{ROOT}/{group['id']}/members/{another['lease_id']}/authority",
             f"{ROOT}/missing/members/{group['members'][0]['lease_id']}/authority"]
    for target in paths:
        response = local_reader.get(target)
        assert response.status_code == 404
        assert response.json() == {'detail': 'Lease-group member not found'}


def test_device_scoped_service_can_observe_without_other_device_scopes(client, hosts):
    assert client.post('/api/v1/users', json={
        'username': 'host-service-reader', 'password': 'test-pw', 'role': 'user',
        'scopes': ['device:' + hosts[0].split('/')[0]]}).status_code == 200
    login = client.post('/api/v1/auth/login', json={
        'username': 'host-service-reader', 'password': 'test-pw'})
    assert login.status_code == 200
    reader = AuthedClient(client._client, login.json()['token'])
    group = create(client, hosts)
    assert observed(reader, group)['authoritative'] is True
    assert reader.get(path(group, 1)).status_code == 404
    assert reader.get(f"{ROOT}/{group['id']}").status_code == 404


def test_projection_cannot_advance_a_partly_acquired_group(client, hosts, local_reader):
    holder = ordinary(client, hosts[0])
    group = create(client, hosts)
    count = db.query_one('SELECT COUNT(*) n FROM leases')['n']
    assert observed(local_reader, group)['authoritative'] is False
    client.delete('/api/v1/leases/' + holder['lease_id'])
    view = observed(local_reader, group)
    assert view['member']['lease']['status'] == 'active'
    assert view['phase'] == 'acquiring' and view['authoritative'] is False
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == count
    assert db.query_one('SELECT COUNT(*) n FROM lease_group_members WHERE lease_id IS NULL')['n'] == 2


def test_own_active_lease_is_not_authority_with_another_member_queued(client, hosts, local_reader):
    ordinary(client, hosts[2])
    group = create(client, hosts)
    view = observed(local_reader, group)
    assert view['member']['lease']['status'] == 'active'
    assert view['phase'] == 'acquiring' and view['authoritative'] is False


@pytest.mark.parametrize('action', ['release', 'fence'])
def test_fence_or_release_is_visible_while_local_lease_stays_charged(client, hosts, local_reader, action):
    group = create(client, hosts)
    assert observed(local_reader, group)['authoritative'] is True
    extra = {'reason': 'successor coordinator'} if action == 'fence' else {}
    assert command(client, group, action, **extra).status_code == 200
    view = observed(local_reader, group)
    assert view['authoritative'] is False and view['release_requested'] is True
    assert view['generation'] == (2 if action == 'fence' else 1)
    assert view['member']['lease']['status'] == 'active'
    assert db.query_one('SELECT COUNT(*) n FROM lease_group_events WHERE kind=?',
                        ('lease_group.member_released',))['n'] == 0


@pytest.mark.parametrize('expired_member', [0, 2])
def test_expired_hold_loss_stops_authority_without_freeing_any_claim(client, hosts, local_reader, expired_member):
    group = create(client, hosts)
    dead = group['members'][expired_member]['lease_id']
    db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (dead,))
    view = observed(local_reader, group)
    assert view['authoritative'] is False and view['phase'] == 'uncertain'
    lease = db.query_one('SELECT * FROM leases WHERE id=?', (dead,))
    assert lease['status'] == 'expired' and lease['settled_at'] is None
    waiter = ordinary(client, hosts[expired_member])
    assert waiter['status'] == 'queued'
    assert hosts[2] not in json.dumps(view)  # no foreign lease or reason payload


def test_member_cleanup_and_terminal_receipt_do_not_expose_evidence(client, hosts, local_reader):
    group = create(client, hosts)
    assert command(client, group, 'release').status_code == 200
    assert command(client, group, f"members/{group['members'][0]['lease_id']}/release", **PROOF).status_code == 200
    view = observed(local_reader, group)
    assert view['authoritative'] is False
    assert view['member']['cleanup_attested'] and view['member']['settlement_recorded']
    assert view['member']['lease']['status'] == 'released'
    assert PROOF['evidence_ref'] not in json.dumps(view)


@pytest.mark.parametrize('field,value', [('exclusive', 0), ('priority', 'high'),
                                       ('memory_mb', 999), ('ttl_s', 601),
                                       ('granted_at', '2099-01-01T00:00:00+00:00')])
def test_mismatched_local_lease_never_establishes_authority(client, hosts, local_reader, field, value):
    group = create(client, hosts)
    db.execute(f'UPDATE leases SET {field}=? WHERE id=?', (value, group['members'][0]['lease_id']))
    assert observed(local_reader, group)['authoritative'] is False


def test_mismatched_link_never_returns_another_resources_lease(client, hosts, local_reader):
    group = create(client, hosts)
    foreign = ordinary(client, hosts[1])['lease_id']
    db.execute('UPDATE lease_group_members SET lease_id=? WHERE group_id=? AND ordinal=0',
               (foreign, group['id']))
    changed = {'id': group['id'], 'members': [{'lease_id': foreign}]}
    view = observed(local_reader, changed)
    assert view['authoritative'] is False and view['member']['lease'] is None
    assert hosts[1] not in json.dumps(view)


def test_missing_lease_and_free_resource_never_establish_authority(client, hosts, local_reader):
    group = create(client, hosts)
    db.execute("UPDATE resources SET expiry_policy='free' WHERE id=?", (hosts[0],))
    assert observed(local_reader, group)['authoritative'] is False
    db.execute('DELETE FROM leases WHERE id=?', (group['members'][0]['lease_id'],))
    view = observed(local_reader, group)
    assert view['authoritative'] is False and view['member']['lease'] is None


def test_lost_resource_visibility_revokes_projection_without_owner_fallback(client, hosts, local_reader):
    group = create(client, hosts)
    assert observed(local_reader, group)['authoritative'] is True
    db.execute("DELETE FROM group_devices WHERE group_id='one-member'")
    response = local_reader.get(path(group))
    assert response.status_code == 404
    assert response.json() == {'detail': 'Lease-group member not found'}


def test_owner_cleanup_fallback_does_not_widen_projection_access(client, hosts):
    owner, _ = user(client, 'member-projection-owner', {'all-three': 'viewer'})
    for resource in hosts:
        assert client.post('/api/v1/groups/all-three/devices', json={
            'device_id': resource.split('/')[0]}).status_code == 200
    group = create(owner, hosts)
    assert observed(owner, group)['authoritative'] is True
    db.execute("DELETE FROM group_devices WHERE group_id='all-three'")
    assert owner.get(path(group)).status_code == 404
    assert owner.get(f"{ROOT}/{group['id']}").status_code == 200
    assert command(owner, group, 'release').status_code == 200


def test_fence_wakeup_is_committed_and_visible_only_to_its_host(client, hosts, local_reader, monkeypatch):
    bus = EventBus()
    monkeypatch.setattr(main, 'event_bus', bus)
    emit = bus.emit
    noticed = []

    def after_commit(kind, target, data, **extra):
        if kind == 'lease.group_changed':
            saved = db.query_one('SELECT * FROM lease_group_events WHERE sequence=?',
                                 (data['group_event_sequence'],))
            assert saved is not None and saved['generation'] == data['generation']
            noticed.append(data)
        emit(kind, target, data, **extra)

    monkeypatch.setattr(bus, 'emit', after_commit)
    group = create(client, hosts, 'hidden-notification-intent')
    noticed.clear()
    assert command(client, group, 'fence', reason='private replacement reason').status_code == 200
    assert len(noticed) == 3  # one per bound host, not per private event
    recent = local_reader.get('/api/v1/events/recent?limit=100').json()
    notices = [x for x in recent if x['type'] == 'lease.group_changed'
               and x['data']['group_id'] == group['id'] and x['data']['generation'] == 2]
    assert len(notices) == 1
    value = notices[0]['data']
    assert value['resource_id'] == hosts[0] and value['phase'] == 'releasing'
    assert set(value) == {'group_id', 'generation', 'phase', 'group_event_sequence',
                          'resource_id', 'lease_id'}
    encoded = json.dumps(notices)
    for secret in (hosts[1], hosts[2], group['members'][1]['lease_id'],
                   'hidden-notification-intent', 'private replacement reason'):
        assert secret not in encoded
    assert not any(x['type'].startswith('lease_group.') for x in recent)
    assert observed(local_reader, group)['authoritative'] is False


def test_unchanged_authority_reads_emit_no_wakeup_loop(client, hosts, local_reader, monkeypatch):
    bus = EventBus()
    monkeypatch.setattr(main, 'event_bus', bus)
    group = create(client, hosts)
    before = len(bus.recent(100))
    for _ in range(3):
        assert observed(local_reader, group)['authoritative'] is True
    assert len(bus.recent(100)) == before


def test_missing_member_still_wakes_its_host_without_settling(client, hosts, local_reader, monkeypatch):
    bus = EventBus()
    monkeypatch.setattr(main, 'event_bus', bus)
    group = create(client, hosts)
    lease_id = group['members'][0]['lease_id']
    db.execute('DELETE FROM leases WHERE id=?', (lease_id,))
    view = observed(local_reader, group)
    assert view['authoritative'] is False and view['phase'] == 'uncertain'
    notices = [x for x in local_reader.get('/api/v1/events/recent?limit=100').json()
               if x['type'] == 'lease.group_changed' and x['data']['phase'] == 'uncertain']
    assert len(notices) == 1 and notices[0]['data']['lease_id'] == lease_id
    assert db.query_one('SELECT COUNT(*) n FROM lease_group_members WHERE release_receipt IS NOT NULL')['n'] == 0
