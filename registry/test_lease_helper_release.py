"""sc-lease release and hold against a fake curl: what lands, what is retried, what stops."""
import os
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parent / 'helpers/samcloud-lease'

# The fake curl answers from a script, one line per call: "<code> <body>", or "000" for no
# answer (curl exit 7). The last line repeats. Every call is logged.
FAKE = r'''#!/usr/bin/env python3
import os, sys
log = os.environ["CALL_LOG"]
n = sum(1 for _ in open(log)) if os.path.exists(log) else 0
with open(log, "a") as f: f.write(" ".join(sys.argv[1:]) + "\n")
lines = open(os.environ["ANSWERS"]).read().splitlines()
code, _, body = lines[min(n, len(lines) - 1)].partition(" ")
if code == "000":
    sys.stdout.write("000"); sys.stderr.write("curl: (7) Failed to connect\n"); sys.exit(7)
print(body); print(code)
'''


def run(tmp_path, answers, *args, retry='4'):
    bindir = tmp_path / 'bin'
    bindir.mkdir(exist_ok=True)
    (bindir / 'curl').write_text(FAKE)
    (bindir / 'curl').chmod(0o700)
    hdr = tmp_path / 'hdr'
    hdr.write_text('X-Test: synthetic\n')
    (tmp_path / 'answers').write_text('\n'.join(answers) + '\n')
    log = tmp_path / 'calls'
    env = dict(os.environ, HOME=str(tmp_path), PATH=str(bindir) + os.pathsep + os.environ['PATH'],
               SAMCLOUD_API='http://registry.invalid/api/v1', SAMCLOUD_AUTH_HDR=str(hdr),
               CALL_LOG=str(log), ANSWERS=str(tmp_path / 'answers'))
    extra = ['--retry', retry] if retry is not None and args[0] == 'release' else []
    r = subprocess.run(['bash', str(HELPER), *args, *extra], env=env, capture_output=True, text=True, timeout=30)
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls


@pytest.mark.parametrize('first', ['000', '502 <html>Bad Gateway</html>', '503 {}', '429 {"detail":"slow down"}'])
def test_release_retries_a_restart_until_it_lands(tmp_path, first):
    r, calls = run(tmp_path, [first, '200 {"status":"released"}'], 'release', 'L1', retry='10')
    assert r.returncode == 0, r.stderr
    assert len(calls) == 2 and all('-X DELETE http://registry.invalid/api/v1/leases/L1 ' in c for c in calls)
    assert 'retrying in 2s' in r.stderr and 'released L1' in r.stderr


def test_release_gives_up_after_the_budget_and_says_keep_the_id(tmp_path):
    r, calls = run(tmp_path, ['502 bad'], 'release', 'L1', retry='4')
    assert r.returncode == 5
    assert len(calls) == 2          # at 0 s, then after 2 s; a 4 s wait would pass the budget
    assert 'keep L1' in r.stderr and 'run release again' in r.stderr


def test_release_with_no_budget_tries_once(tmp_path):
    r, calls = run(tmp_path, ['000'], 'release', 'L1', retry='0')
    assert r.returncode == 5 and len(calls) == 1


def test_only_the_planes_own_404_counts_as_gone(tmp_path):
    r, calls = run(tmp_path, ['404 {"detail":"Lease L1 not found"}'], 'release', 'L1')
    assert r.returncode == 0 and 'already gone' in r.stderr and len(calls) == 1


@pytest.mark.parametrize('body', ['Not Found', '{"detail":"Lease L12 not found"}', '{"detail":"Not Found"}'])
def test_another_404_is_not_released(tmp_path, body):
    r, calls = run(tmp_path, [f'404 {body}'], 'release', 'L1')
    assert r.returncode == 5 and 'not released' in r.stderr and len(calls) == 1


@pytest.mark.parametrize('code', ['401', '403'])
def test_a_wrong_identity_stops_at_once(tmp_path, code):
    r, calls = run(tmp_path, [f'{code} {{"detail":"no"}}', '200 {}'], 'release', 'L1')
    assert r.returncode == 5 and len(calls) == 1
    assert 'wrong or rotated' in r.stderr


def test_a_400_is_final(tmp_path):
    r, calls = run(tmp_path, ['400 {"detail":"bad"}', '200 {}'], 'release', 'L1')
    assert r.returncode == 5 and len(calls) == 1


def test_retried_calls_carry_a_time_limit_and_no_bearer(tmp_path):
    r, calls = run(tmp_path, ['200 {}'], 'release', 'L1')
    assert r.returncode == 0
    assert '-m 60' in calls[0] and 'Bearer' not in calls[0]


def test_hold_survives_a_renew_with_no_answer(tmp_path):
    r, calls = run(tmp_path, ['000', '502 bad', '200 {}', '404 {"detail":"Lease L1 not found","status":"released"}'],
                   'hold', 'L1', '--ttl', '30', '--every', '1')
    assert r.returncode == 0, r.stderr
    assert len(calls) == 4 and all('/leases/L1/renew' in c for c in calls)
    assert 'hold ends' in r.stderr


def test_acquire_still_has_no_time_limit(tmp_path):
    r, calls = run(tmp_path, ['200 {"id":"L1","status":"granted"}'], 'acquire', 'res/gpu-0', '--memory', '10')
    assert calls and '-m 60' not in calls[0]
