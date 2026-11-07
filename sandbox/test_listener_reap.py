#!/usr/bin/env python3
"""The listener reaps orphans and reports its own kill (#895 asks 4 and 2-path-2).

Drives the REAL registry/helpers/samcloud-listen.py against real forked children,
a real subprocess and a fake plane. No registry, no network, no sleeping.

Why this exists (2026-10-07): `samcloud-listen` is PID 1 in a profile-env
container and reaped nothing. The rail launches a holder detached
(`start_new_session=True`) and exits, so the holder reparents to PID 1 and its
children do too. Measured on a container seat after ONE failed turn: the dead
adapter and its code kernel sat `Zs` with PPID 1 for 40 and 32 minutes. Two leaked
PIDs per turn on a seat that runs every day.

And the listener's own cap kill told nobody: it logged `wake handler exceeded Ns
— killed` locally and stopped. No `adapter.jsonl` row (the handler died before
writing one) and no session report (the handler is what posts those, and it is
dead). It is the only kill with no record anywhere on the plane.

THE DANGEROUS PART, and why [3] and [4] exist. `waitpid(-1)` cannot tell our own
subprocess children from reparented orphans. Reaping from a SIGCHLD handler would
fire DURING `subprocess.run(WAKE_CMD)` and steal the exit status `run()` is waiting
for — the classic PID-1 reaping bug, which would turn every wake into a spurious
failure. The safety argument is positional: reap only from main-loop code, never
from a signal handler. [3] asserts that structurally and [4] behaviourally.

Point it at an older build to watch that build fail:
    LISTEN_UNDER_TEST=/tmp/old-listen.py python3 sandbox/test_listener_reap.py
(2.7.13 has neither function, so it fails at import of the names.)

Run from repo root: python3 sandbox/test_listener_reap.py
"""
import ast
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LISTEN = Path(os.environ.get("LISTEN_UNDER_TEST")
              or REPO / "registry/helpers/samcloud-listen.py")

PASS, FAIL = 0, 0


def check(desc, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"      \033[0;32m✓\033[0m {desc}")
    else:
        FAIL += 1
        print(f"      \033[0;31m✗\033[0m {desc}  {detail}")


def load(home: Path, **conf):
    (home / ".config/samcloud").mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    (home / ".config/samcloud/wake.env").write_text(
        "\n".join(f"{k}={v}" for k, v in conf.items()) + "\n")
    os.environ["HOME"] = str(home)
    os.environ["SAMCLOUD_WAKE_CONF"] = str(home / ".config/samcloud/wake.env")
    spec = importlib.util.spec_from_file_location(f"listen_{home.name}", LISTEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def capture(mod):
    """Collect log lines and every plane write, and make PID 1 reachable."""
    lines, posts = [], []
    mod.log = lambda m: lines.append(str(m))

    class FakeResp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        posts.append({"url": req.full_url, "method": req.get_method(),
                      "body": json.loads(req.data.decode()) if req.data else None})
        return FakeResp()

    mod.urllib.request.urlopen = fake_urlopen
    return lines, posts


def spawn_exited_child() -> int:
    """A real child that has already exited: reapable, status uncollected."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    return pid


def as_pid1(mod):
    """Make the PID-1 guard pass without being init. The guard is correct in
    production and untestable in a test runner, so it is the one thing faked."""
    mod.os.getpid = lambda: 1


def main():
    print("\n  samcloud-listen: reap orphans, report our own kill (#895)")
    print(f"  under test: {LISTEN}")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ── [1] the PID-1 guard ──────────────────────────────────────────────
        print("\n  [1] reap_orphans does nothing unless it is PID 1")
        h1 = tmp / "h1"
        h1.mkdir()
        mod = load(h1, API="http://127.0.0.1:1/api/v1", RAIL_V3="1",
                   WAKE_CMD="/bin/true", MAX_SESSION_MIN="30")
        lines, posts = capture(mod)
        check("reap_orphans exists", hasattr(mod, "reap_orphans"))
        check("report_truncated exists", hasattr(mod, "report_truncated"))
        if not (hasattr(mod, "reap_orphans") and hasattr(mod, "report_truncated")):
            # Bail cleanly rather than AttributeError-ing three checks in. A crash
            # here reports two failures and hides the other sixteen, which tells a
            # reader far less than "this build has neither function".
            print(f"\n      {LISTEN.name} has neither function — nothing further "
                  f"to exercise.")
            print(f"\n  FAILURES DETECTED: listener reap + truncation "
                  f"{PASS} passed, {FAIL} failed (build predates the change)")
            return 1
        pid = spawn_exited_child()
        check("not PID 1 -> returns 0 and reaps nothing", mod.reap_orphans() == 0)
        os.waitpid(pid, 0)                      # clean up ourselves

        # ── [2] reaping, and its termination conditions ──────────────────────
        print("\n  [2] as PID 1 it reaps, counts, and terminates")
        as_pid1(mod)
        for _ in range(3):
            spawn_exited_child()
        # Children die asynchronously, so ACCUMULATE across calls with a deadline
        # rather than asserting one call sees all three. An earlier version of this
        # test broke out on the first non-zero and then asserted 3 — it failed
        # against correct code, which is the kind of flake that gets a real fix
        # reverted.
        total, deadline = 0, time.monotonic() + 10
        while total < 3 and time.monotonic() < deadline:
            total += mod.reap_orphans()
        check("reaps all three exited orphans", total == 3, total)
        check("and says so in the log",
              sum(int("reaped" in l) for l in lines) >= 1, lines[-3:])
        check("a second call reaps nothing (ECHILD, no exception)",
              mod.reap_orphans() == 0)
        # children that exist but have NOT exited must not block or be waited on
        alive = os.fork()
        if alive == 0:
            try:
                os.pause()
            finally:
                os._exit(0)
        check("a live child is left alone (WNOHANG, pid==0 breaks the loop)",
              mod.reap_orphans() == 0, "reaped a child that had not exited")
        os.kill(alive, 9)
        os.waitpid(alive, 0)

        # ── [3] the safety invariant, asserted structurally ─────────────────
        print("\n  [3] reaping is never reachable from a signal handler")
        tree = ast.parse(LISTEN.read_text())
        sig_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and "signal" in ast.unparse(n.func)]
        check("the module installs NO signal handlers at all",
              len(sig_calls) == 0, [ast.unparse(c)[:60] for c in sig_calls])
        callers = [f.name for f in ast.walk(tree)
                   if isinstance(f, ast.FunctionDef)
                   and any(isinstance(n, ast.Call) and getattr(n.func, "id", "")
                           == "reap_orphans" for n in ast.walk(f))]
        check("called only from wake() and stream_once()",
              sorted(callers) == ["stream_once", "wake"], callers)

        # ── [4] the hazard itself: a wake's exit status must survive reaping ─
        print("\n  [4] a reapable orphan present DURING a wake does not corrupt it")
        h4 = tmp / "h4"
        h4.mkdir()
        mod4 = load(h4, API="http://127.0.0.1:1/api/v1", RAIL_V3="1",
                    WAKE_CMD="/bin/sh -c 'exit 7'", MAX_SESSION_MIN="30")
        lines4, posts4 = capture(mod4)
        as_pid1(mod4)
        mod4.inbox_state = lambda token: (True, True)
        orphan = spawn_exited_child()           # reapable BEFORE the wake starts
        mod4.wake("test", {"reason": "test"}, token="tok")
        check("the handler's real exit code is still observed",
              any("wake handler exit=7" in l for l in lines4),
              [l for l in lines4 if "exit" in l])
        check("and the orphan was reaped after it, not during",
              any("reaped" in l for l in lines4), lines4)
        check("no truncation reported for a handler that exited on its own",
              not any("/wake/session" in p["url"] for p in posts4), posts4)
        del orphan

        # ── [5] our own kill is reported ────────────────────────────────────
        print("\n  [5] a kill at the cap reports cause=watchdog")
        h5 = tmp / "h5"
        h5.mkdir()
        mod5 = load(h5, API="http://127.0.0.1:1/api/v1", RAIL_V3="1",
                    WAKE_CMD="/bin/sleep 30", MAX_SESSION_MIN="0")
        lines5, posts5 = capture(mod5)
        mod5.inbox_state = lambda token: (True, True)
        mod5.WAKE_TIMEOUT = 1                   # 1s cap, real sleep, real kill
        mod5.wake("test", {"reason": "test"}, token="tok")
        sess = [p for p in posts5 if p["url"].endswith("/wake/session")]
        check("exactly one session report", len(sess) == 1, posts5)
        b = sess[0]["body"] if sess else {}
        check("cause is watchdog", b.get("cause") == "watchdog", b)
        check("outcome failed, rc 143 (SIGTERM)",
              b.get("outcome") == "failed" and b.get("rc") == 143, b)
        check("the error names the cap and the knob",
              "MAX_SESSION_MIN" in (b.get("error") or ""), b)
        check("logged the kill AND the report",
              any("exceeded" in l for l in lines5)
              and any("reported truncation" in l for l in lines5), lines5)

        # ── [6] no token, no report (a seat that cannot authenticate) ───────
        print("\n  [6] without a token it reports nothing rather than erroring")
        posts5.clear()
        mod5.report_truncated(None, 1920, 143)
        check("no post attempted", len(posts5) == 0, posts5)

    verdict = "ALL PASSED" if FAIL == 0 else "FAILURES DETECTED"
    print(f"\n  {verdict}: listener reap + truncation {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
