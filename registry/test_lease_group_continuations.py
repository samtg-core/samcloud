"""HTTP/SQLite continuation controls. No production, worker or inference call."""
# Pytest injects these imported fixtures by the same names as test parameters.
# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import threading

import pytest

import db
import main
from test_lease_groups import (ROOT, PROOF, client, hosts, create, intent,  # noqa: F401
                               ordinary, command, drain)


def expired_group(client, hosts):
    group = create(client, hosts)
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    for member in group['members']:
        db.execute('UPDATE leases SET granted_at=?,expires_at=? WHERE id=?',
                   (start.isoformat(), (start + timedelta(seconds=600)).isoformat(), member['lease_id']))
    group = client.get(f"{ROOT}/{group['id']}").json()
    assert group['phase'] == 'uncertain'
    assert all(m['status'] == 'expired' and m['settled_at'] is None for m in group['members'])
    return group


def request(group, key='bounded-successor'):
    return {'generation': group['generation'], 'request_key': key,
            'members': [{'lease_id': m['lease_id'], **PROOF} for m in group['members']]}


def send(client, group, body=None):
    return client.post(f"{ROOT}/{group['id']}/continue-after-cleanup", json=body or request(group))


def snapshot():
    return {table: db.query(f'SELECT * FROM {table} ORDER BY 1') for table in
            ('leases', 'lease_groups', 'lease_group_members', 'lease_group_events',
             'lease_group_continuations')}


def test_atomic_successor_keeps_exact_pool_and_waiter_order_without_resurrection(client, hosts):
    old = expired_group(client, hosts)
    waiters = [ordinary(client, resource) for resource in hosts]
    old_rows = {m['lease_id']: db.query_one('SELECT * FROM leases WHERE id=?', (m['lease_id'],))
                for m in old['members']}
    response = send(client, old)
    assert response.status_code == 200, response.text
    new = response.json()
    assert new['phase'] == 'admitted' and new['generation'] == 1
    assert new['owner_id'] == old['owner_id'] and new['continued_from'] == old['id']
    prior = client.get(f"{ROOT}/{old['id']}").json()
    assert prior['phase'] == 'released' and prior['generation'] == old['generation'] + 1
    assert prior['continued_to'] == new['id']
    for before, after in zip(prior['members'], new['members'], strict=True):
        assert before['lease_id'] != after['lease_id']
        ended = db.query_one('SELECT * FROM leases WHERE id=?', (before['lease_id'],))
        actual = db.query_one('SELECT * FROM leases WHERE id=?', (after['lease_id'],))
        assert ended['status'] == 'expired' and ended['settled_at'] and not ended['released_at']
        for key in ('granted_at', 'expires_at', 'agent_id', 'resource_id', 'memory_mb'):
            assert ended[key] == old_rows[ended['id']][key]
        for key in ('agent_id', 'resource_id', 'service_id', 'memory_mb', 'exclusive',
                    'priority', 'ttl_s', 'max_total_s'):
            assert actual[key] == ended[key]
        assert before['cleanup_evidence']['evidence_sha256'] == PROOF['evidence_sha256']
        assert before['release_receipt']['lease_id'] == ended['id']
        assert before['release_receipt']['settled_at'] == ended['settled_at']
    for waiter in waiters:
        row = db.query_one('SELECT * FROM leases WHERE id=?', (waiter['lease_id'],))
        assert row['status'] == 'queued' and row['queue_position'] == 1
    assert db.query_one("SELECT COUNT(*) n FROM leases WHERE status='active'")['n'] == 3
    assert command(client, old, 'advance').status_code == 409
    for member in new['members']:
        projection = client.get(f"{ROOT}/{new['id']}/members/{member['lease_id']}/authority").json()
        assert projection['authoritative'] and projection['generation'] == new['generation']
    for member in old['members']:
        projection = client.get(f"{ROOT}/{old['id']}/members/{member['lease_id']}/authority").json()
        assert not projection['authoritative'] and projection['generation'] == prior['generation']
    # Original keys still resolve to their original operation; no implicit replay.
    assert create(client, hosts)['id'] == old['id']
    replacement = intent(hosts, 'bounded-successor')
    assert client.post(ROOT, json=replacement).json()['id'] == new['id']


def test_continuation_replay_returns_current_successor_without_renewal(client, hosts):
    old = expired_group(client, hosts)
    first = send(client, old).json()
    rows = snapshot()
    assert send(client, old).json() == first
    assert snapshot() == rows
    # Reordering identical proofs does not allocate again.
    reordered = request(old)
    reordered['members'].reverse()
    assert send(client, old, reordered).json() == first
    assert drain(client, first)['phase'] == 'released'
    assert send(client, old).json()['phase'] == 'released'
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == 6


def test_replaying_an_expired_successor_never_restores_authority(client, hosts):
    old = expired_group(client, hosts)
    new = send(client, old).json()
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    for member in new['members']:
        db.execute('UPDATE leases SET granted_at=?,expires_at=? WHERE id=?',
                   (start.isoformat(), (start + timedelta(seconds=600)).isoformat(), member['lease_id']))
    assert client.get(f"{ROOT}/{new['id']}").json()['phase'] == 'uncertain'
    before = snapshot()
    replay = send(client, old).json()
    assert replay['phase'] == 'uncertain' and all(m['status'] == 'expired' for m in replay['members'])
    assert snapshot() == before


@pytest.mark.parametrize('change', ['key', 'generation', 'proof'])
def test_conflicting_continuation_never_replaces_or_releases_again(client, hosts, change):
    old = expired_group(client, hosts)
    assert send(client, old).status_code == 200
    before = snapshot()
    body = request(old)
    if change == 'key':
        body['request_key'] = 'different'
    elif change == 'generation':
        body['generation'] += 1
    else:
        body['members'][0]['evidence_sha256'] = 'b' * 64
    assert send(client, old, body).status_code == 409
    assert snapshot() == before


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'foreign', 'empty'])
def test_each_exact_cleanup_proof_is_required_before_any_settlement(client, hosts, change):
    old = expired_group(client, hosts)
    before = snapshot()
    body = request(old)
    if change == 'missing':
        body['members'].pop()
    elif change == 'duplicate':
        body['members'][1] = dict(body['members'][0])
    elif change == 'foreign':
        body['members'][1]['lease_id'] = 'lease-foreign'
    else:
        body['members'][0]['evidence_ref'] = ''
    assert send(client, old, body).status_code in (409, 422)
    assert snapshot() == before


@pytest.mark.parametrize('change', ['owner', 'size', 'ttl', 'ceiling', 'missing', 'settled', 'released', 'free'])
def test_changed_or_ended_predecessor_refuses_and_keeps_other_claims(client, hosts, change):
    old = expired_group(client, hosts)
    lease = old['members'][1]['lease_id']
    if change == 'owner':
        db.execute('UPDATE leases SET agent_id=? WHERE id=?', ('user:foreign', lease))
    elif change == 'size':
        db.execute('UPDATE leases SET memory_mb=999 WHERE id=?', (lease,))
    elif change == 'ttl':
        db.execute('UPDATE leases SET ttl_s=601 WHERE id=?', (lease,))
    elif change == 'ceiling':
        db.execute('UPDATE leases SET max_total_s=1 WHERE id=?', (lease,))
    elif change == 'missing':
        db.execute('DELETE FROM leases WHERE id=?', (lease,))
    elif change == 'settled':
        db.execute('UPDATE leases SET settled_at=? WHERE id=?', ('observed-settled', lease))
    elif change == 'released':
        db.execute("UPDATE leases SET status='released' WHERE id=?", (lease,))
    else:
        db.execute("UPDATE resources SET expiry_policy='free' WHERE id=?", (hosts[1],))
    before = snapshot()
    assert send(client, old).status_code in (404, 409)
    assert snapshot() == before


def test_partial_expiry_or_still_active_group_cannot_continue(client, hosts):
    old = create(client, hosts)
    before = snapshot()
    assert send(client, old).status_code == 409
    assert snapshot() == before
    db.execute("UPDATE leases SET status='expired' WHERE id=?", (old['members'][0]['lease_id'],))
    client.get(f"{ROOT}/{old['id']}")
    before = snapshot()
    assert send(client, old).status_code == 409
    assert snapshot() == before


def test_release_request_cannot_turn_into_another_acquisition(client, hosts):
    old = expired_group(client, hosts)
    assert command(client, old, 'release').status_code == 200
    before = snapshot()
    assert send(client, old).status_code == 409
    assert snapshot() == before


def test_a_previously_serving_turn_cannot_continue_ahead_of_waiters(client, hosts):
    old = create(client, hosts)
    assert command(client, old, 'serving', **PROOF).status_code == 200
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    for member in old['members']:
        db.execute('UPDATE leases SET granted_at=?,expires_at=? WHERE id=?',
                   (start.isoformat(), (start + timedelta(seconds=600)).isoformat(), member['lease_id']))
    old = client.get(f"{ROOT}/{old['id']}").json()
    assert old['phase'] == 'uncertain' and old['serving_evidence']
    before = snapshot()
    assert send(client, old).status_code == 409
    assert snapshot() == before


def test_existing_successor_key_is_not_adopted_or_displaced(client, hosts):
    old = expired_group(client, hosts)
    other = create(client, hosts, 'bounded-successor')
    assert other['phase'] == 'pending'
    before = snapshot()
    assert send(client, old).status_code == 409
    assert snapshot() == before


def test_another_charged_allocation_cannot_be_hidden_by_continuation(client, hosts):
    old = expired_group(client, hosts)
    db.execute("INSERT INTO leases(id,resource_id,agent_id,memory_mb,exclusive,status) "
               "VALUES ('conflict',?,'user:foreign',1,0,'active')", (hosts[1],))
    before = snapshot()
    assert send(client, old).status_code == 409
    assert snapshot() == before


def test_resource_redimension_requires_new_admission_not_a_larger_transfer(client, hosts):
    old = expired_group(client, hosts)
    db.execute('UPDATE resources SET specs=? WHERE id=?', (json.dumps({'ram_mb': 999}), hosts[1]))
    before = snapshot()
    assert send(client, old).status_code == 409
    assert snapshot() == before


def test_owner_and_current_all_host_visibility_are_required(client, hosts):
    from test_auth_boundaries import user
    owner, _ = user(client, 'continuation-owner', {'all-hosts': 'viewer'})
    reader, _ = user(client, 'continuation-reader', {'all-hosts': 'viewer'})
    partial, _ = user(client, 'continuation-partial', {'one-host': 'viewer'})
    for resource in hosts:
        assert client.post('/api/v1/groups/all-hosts/devices',
                           json={'device_id': resource.split('/')[0]}).status_code == 200
    assert client.post('/api/v1/groups/one-host/devices',
                       json={'device_id': hosts[0].split('/')[0]}).status_code == 200
    old = expired_group(owner, hosts)
    before = snapshot()
    assert send(reader, old).status_code == 403
    assert send(partial, old).status_code == 404
    assert snapshot() == before
    db.execute("DELETE FROM group_devices WHERE group_id='all-hosts'")
    assert send(owner, old).status_code == 404
    assert snapshot() == before
    # Admin still performs the same attributed operation for the original owner.
    result = send(client, old)
    assert result.status_code == 200, result.text
    assert result.json()['owner_id'] == old['owner_id']


def test_mid_transfer_database_failure_rolls_back_even_the_first_replacement(client, hosts):
    import sqlite3
    old = expired_group(client, hosts)
    before = snapshot()
    db.execute("CREATE TRIGGER fail_second_successor BEFORE INSERT ON leases "
               "WHEN (SELECT COUNT(*) FROM leases WHERE status='active')=1 "
               "BEGIN SELECT RAISE(ABORT,'second successor blocked'); END")
    with pytest.raises(sqlite3.IntegrityError, match='second successor blocked'):
        send(client, old)
    assert snapshot() == before


def test_commit_failure_rolls_back_all_settlements_and_grants_and_publishes_nothing(client, hosts, monkeypatch):
    old = expired_group(client, hosts)
    before = snapshot()
    original = db.transaction
    @contextmanager
    def fail():
        with original() as conn:
            yield conn
            raise RuntimeError('continuation commit failed')
    events = []
    monkeypatch.setattr(db, 'transaction', fail)
    monkeypatch.setattr(main.event_bus, 'emit', lambda *args, **kw: events.append(args))
    with pytest.raises(RuntimeError, match='continuation commit failed'):
        send(client, old)
    assert snapshot() == before
    assert not events


def test_every_event_sees_committed_settlements_and_the_full_new_pool(client, hosts, monkeypatch):
    old = expired_group(client, hosts)
    events = []
    def emitted(kind, ident, payload, **kwargs):
        assert db.query_one("SELECT COUNT(*) n FROM leases WHERE status='active'")['n'] == 3
        assert db.query_one('SELECT COUNT(*) n FROM leases WHERE settled_at IS NOT NULL')['n'] == 3
        events.append((kind, ident, payload))
    monkeypatch.setattr(main.event_bus, 'emit', emitted)
    new = send(client, old).json()
    assert len([e for e in events if e[0] == 'lease.granted']) == 3
    assert len([e for e in events if e[0] == 'lease.settled']) == 3
    assert [e for e in events if e[0] == 'lease_group.continued']
    assert new['phase'] == 'admitted'


def test_concurrent_replays_share_one_successor_and_one_new_member_set(client, hosts):
    old = expired_group(client, hosts)
    barrier = threading.Barrier(6)
    def run(_):
        barrier.wait(timeout=10)
        return send(client, old)
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(run, range(6)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert len({r.json()['id'] for r in responses}) == 1
    assert db.query_one('SELECT COUNT(*) n FROM leases')['n'] == 6
    assert db.query_one('SELECT COUNT(*) n FROM lease_groups')['n'] == 2
    assert db.query_one('SELECT COUNT(*) n FROM lease_group_continuations')['n'] == 1


@pytest.mark.parametrize('extra', [{'generation': True}, {'ttl_seconds': 21600},
                                  {'members': []}, {'request_key': ' '}, {'request_key': '\ud800'}])
def test_untyped_or_unrecorded_controls_refuse_without_changes(client, hosts, extra):
    old = expired_group(client, hosts)
    before = snapshot()
    body = request(old) | extra
    # Encode a surrogate explicitly; httpx's JSON serializer rejects it locally.
    result = client.post(f"{ROOT}/{old['id']}/continue-after-cleanup",
                         content=json.dumps(body, ensure_ascii=True),
                         headers={'Content-Type': 'application/json'})
    assert result.status_code == 422
    assert snapshot() == before


@pytest.mark.parametrize('reason', ['wait_expired', None])
def test_noncharging_expired_history_does_not_block_the_held_pool(client, hosts, reason):
    old = expired_group(client, hosts)
    db.execute("INSERT INTO leases(id,resource_id,agent_id,memory_mb,exclusive,status,"
               "expiry_reason,settled_at,granted_at,expires_at) "
               "VALUES ('historical',?,'user:history',100,0,'expired',?,NULL,?,?)",
               (hosts[0], reason, None if reason else '2026-10-01T00:00:00+00:00',
                '2026-10-01T01:00:00+00:00'))
    prior = db.query_one("SELECT * FROM leases WHERE id='historical'")
    assert client.get('/api/v1/resources/'+hosts[0]).json()['unsettled'] == [
        {'lease_id': old['members'][0]['lease_id'], 'service_id': None,
         'agent_id': old['owner_id'], 'memory_mb': 1000, 'exclusive': True,
         'expired_at': old['members'][0]['expires_at']}]
    response = send(client, old)
    assert response.status_code == 200, response.text
    assert response.json()['phase'] == 'admitted'
    assert db.query_one("SELECT * FROM leases WHERE id='historical'") == prior
    assert db.query_one("SELECT COUNT(*) n FROM leases WHERE status='active'")['n'] == 3


def test_another_real_expired_hold_still_blocks_continuation(client, hosts):
    old = expired_group(client, hosts)
    db.execute("INSERT INTO leases(id,resource_id,agent_id,memory_mb,exclusive,status,"
               "expiry_reason,settled_at,granted_at,expires_at) "
               "VALUES ('real-hold',?,'user:other',100,0,'expired','ttl_expired',NULL,?,?)",
               (hosts[0], '2026-10-01T00:00:00+00:00', '2026-10-01T01:00:00+00:00'))
    before = snapshot()
    assert send(client, old).status_code == 409
    assert snapshot() == before
