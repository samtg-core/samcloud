"""PR100 review faults: temporary registry DB and local fake helper transport.

These assert physical/queue safety and fail on dd524b9. No live registry,
provider, editor, lease or credential is used.
"""
import os
from pathlib import Path
import subprocess

import db
from test_registry import client  # noqa: F401
from test_lease_admission import resource  # noqa: F401
from test_lease_queue import lease, row, backdate


def test_expired_waiter_is_not_resurrected_by_holder_release(client, resource):
    holder = lease(client, resource, memory_mb=1000)
    waiter = lease(client, resource, memory_mb=500, wait_s=60, ttl_seconds=300)
    backdate(waiter['lease_id'])
    reply = client.delete('/api/v1/leases/' + holder['lease_id'])
    assert reply.status_code == 200
    assert waiter['lease_id'] not in reply.json()['promoted']
    assert row(waiter['lease_id'])['status'] == 'expired'
    assert row(waiter['lease_id'])['expiry_reason'] == 'wait_expired'


def test_bad_queued_claim_cannot_mint_capacity_on_promotion(client, resource):
    holder = lease(client, resource, memory_mb=1000)
    bad = lease(client, resource, memory_mb=500, wait_s=600)
    good = [lease(client, resource, memory_mb=500, wait_s=600) for _ in range(3)]
    # Legacy/corrupt queued data must not be more trusted than active data.
    db.execute('UPDATE leases SET memory_mb=-1000 WHERE id=?', (bad['lease_id'],))
    client.delete('/api/v1/leases/' + holder['lease_id'])
    newcomer = client.post(resource + '/leases', json={'memory_mb':1000})
    positives = db.query_one("SELECT COALESCE(SUM(memory_mb),0) n FROM leases "
                             "WHERE status='active' AND memory_mb>0")['n']
    assert positives <= 1000, (good, newcomer.json(), positives)
    assert row(bad['lease_id'])['status'] != 'active'


def test_expired_allocation_does_not_prove_live_executor_settled(client, resource):
    # The fail-closed rule is a per-resource policy (task 868): `hold` keeps an expired
    # holder charged until its holder or an admin settles it. gpu-0 runs `hold`; the
    # default stays `free` so an agent's ad-hoc exclusive lease (a headset, a seat)
    # still ends when the agent dies. The default-policy behaviour has its own test below.
    assert client.patch(resource, json={'expiry_policy': 'hold'}).status_code == 200, 'policy'
    holder = lease(client, resource, memory_mb=1000, ttl_seconds=300)
    waiter = lease(client, resource, memory_mb=500, wait_s=600)
    # The external executor is deliberately still alive. No executor exit,
    # unload or administrative reconciliation occurs between expiry and read.
    executor = subprocess.Popen(['sleep', '30'])
    try:
        backdate(holder['lease_id'])
        client.get(resource)
        assert executor.poll() is None
        assert row(waiter['lease_id'])['status'] == 'queued', 'TTL promoted work against a live executor'
    finally:
        executor.terminate()
        executor.wait(timeout=5)


def test_served_release_helper_does_not_release_live_pid_at_wait_bound(tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    curl = bindir / 'curl'
    curl.write_text('#!/usr/bin/env python3\nimport os,sys\n'
                    'with open(os.environ["CALL_LOG"], "a") as f: f.write(" ".join(sys.argv[1:])+"\\n")\n'
                    'print(\'{"status":"released"}\\n200\')\n')
    curl.chmod(0o700)
    hdr = tmp_path / 'hdr'
    hdr.write_text('X-Test: synthetic\n')
    log = tmp_path / 'calls'
    helper = Path(__file__).resolve().parent / 'helpers/samcloud-lease'
    env = dict(os.environ, HOME=str(tmp_path), PATH=str(bindir)+os.pathsep+os.environ['PATH'],
               SAMCLOUD_API='http://registry.invalid/api/v1', SAMCLOUD_AUTH_HDR=str(hdr), CALL_LOG=str(log))
    # This test runner's own pid remains alive; the helper must never signal it.
    result = subprocess.run(['bash', str(helper), 'release', 'L1', '--after-pids', str(os.getpid()),
                             '--timeout', '1'], env=env, capture_output=True, text=True, timeout=10)
    assert not log.exists(), 'helper DELETEs while its supplied pid is still alive'
    assert result.returncode != 0


# --- the policy and the settlement, from the admin side of the same review -------------

def test_default_policy_frees_an_expired_holder_for_the_queue(client, resource):
    """`free`, the default: a holder past its TTL stops charging at once and the waiter is
    granted on the next read — today's behaviour for every resource not opted into `hold`."""
    holder = lease(client, resource, memory_mb=1000, ttl_seconds=300)
    waiter = lease(client, resource, memory_mb=500, wait_s=600)
    backdate(holder['lease_id'])
    view = client.get(resource).json()
    assert view['expiry_policy'] == 'free'
    assert row(holder['lease_id'])['status'] == 'expired'
    assert row(waiter['lease_id'])['status'] == 'active'
    assert view['unsettled'] == []


def test_hold_policy_settlement_by_the_holder_frees_the_bytes_and_promotes(client, resource):
    assert client.patch(resource, json={'expiry_policy': 'hold'}).status_code == 200
    holder = lease(client, resource, memory_mb=1000, ttl_seconds=300)
    waiter = lease(client, resource, memory_mb=500, wait_s=600)
    backdate(holder['lease_id'])
    view = client.get(resource).json()
    assert row(holder['lease_id'])['status'] == 'expired'
    assert row(waiter['lease_id'])['status'] == 'queued', 'an unsettled holder still charges'
    assert view['available_memory_mb'] == 0
    assert [u['lease_id'] for u in view['unsettled']] == [holder['lease_id']]
    # The settlement is the explicit act: DELETE by the holder (here the admin).
    reply = client.delete('/api/v1/leases/' + holder['lease_id'])
    assert reply.status_code == 200, reply.text
    assert reply.json()['status'] == 'settled'
    assert reply.json()['promoted'] == [waiter['lease_id']]
    assert row(holder['lease_id'])['settled_at']
    assert row(waiter['lease_id'])['status'] == 'active'
    assert client.get(resource).json()['unsettled'] == []
    # Idempotent: the second DELETE moves nothing and still answers.
    again = client.delete('/api/v1/leases/' + holder['lease_id'])
    assert again.status_code == 200 and again.json()['promoted'] == []


def test_expiry_policy_is_validated(client, resource):
    assert client.patch(resource, json={'expiry_policy': 'maybe'}).status_code == 422
    assert client.patch(resource, json={'expiry_policy': 'hold'}).json()['expiry_policy'] == 'hold'


def test_invalid_queued_ttl_leaves_the_queue_without_minting(client, resource):
    holder = lease(client, resource, memory_mb=1000)
    bad = lease(client, resource, memory_mb=100, wait_s=600)
    good = lease(client, resource, memory_mb=100, wait_s=600)
    db.execute('UPDATE leases SET ttl_s=-5 WHERE id=?', (bad['lease_id'],))
    reply = client.delete('/api/v1/leases/' + holder['lease_id'])
    assert reply.json()['promoted'] == [good['lease_id']]
    assert row(bad['lease_id'])['status'] == 'expired'
    assert row(bad['lease_id'])['expiry_reason'] == 'invalid_claim'
    assert row(good['lease_id'])['queue_position'] is None and row(good['lease_id'])['status'] == 'active'

