#!/usr/bin/env python3
"""Listener periodic clocks under a BUSY stream (#627).

Drives the REAL registry/helpers/samcloud-listen.py `stream_once()` against a
synthetic SSE response and a fake clock — no registry, no network, no sleeping.

Why this exists (2026-09-06): the registry resets its 15s keepalive timer on
every byte written to a client, real events included (#184). A stream busier
than one visible event per 15s therefore emits no `:` keepalive line at all —
and admin identities see every fleet event, so that is precisely the busiest
stream in the fleet. Every periodic clock in the listener hung off the
keepalive branch: the v3 recovery heartbeat AND the MAX_CONN_AGE age-out that
carries register()/self-update/skills-sync. Measured on samclaude-admin:
age-out 1h22m late (2026-09-06) and 1h48m late (2026-09-03), inbox unpolled
across both windows — with a `wake.dark` page sitting in it, the one
notification the dark-watch writes with no stream event at all, so an inbox
poll is its ONLY delivery path. Nothing in the gates exercised a stream that
does not idle.

Point it at an older build to watch that build fail:
    LISTEN_UNDER_TEST=/tmp/old-listen.py python3 sandbox/test_listen_clocks.py
(pre-2.7.8 never returns; the stream's line budget turns that into a failure
instead of a hang.)

Run from repo root: python3 sandbox/test_listen_clocks.py
"""
import importlib.util
import os
import sys
import tempfile
import time as _real_time
import types
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


class Clock:
    """Fake wall clock. Proxies everything else (log() wants strftime)."""

    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def __getattr__(self, name):
        return getattr(_real_time, name)


class Stream:
    """One SSE response. `lines` is a callable producing the next line.

    Counts what it emitted so a test can tell a completed event block from a
    half-read one, and refuses to run forever: a loop whose clocks never fire
    would otherwise hang the gate instead of failing it.
    """

    def __init__(self, clock, per_line, budget=20000):
        self.clock, self.per_line, self.budget = clock, per_line, budget
        self.n = 0
        self.closed = False
        self.emitted_ids, self.completed_ids = [], []

    def readline(self):
        self.n += 1
        if self.n > self.budget:
            raise RuntimeError(
                f"stream exhausted after {self.budget} lines — the read loop "
                "never fired a clock (this is the #627 bug, not a flake)")
        self.clock.t += self.per_line
        return self._next()

    def close(self):
        self.closed = True


class BusyStream(Stream):
    """Endless visible events, never a keepalive — an admin seat's stream."""

    def _next(self):
        i, phase = divmod(self.n - 1, 4)
        eid = i + 1
        if phase == 0:
            self.emitted_ids.append(eid)
            return f"id: {eid}\n".encode()
        if phase == 1:
            return b"event: ticket.commented\n"
        if phase == 2:
            return b'data: {"comment_id": %d}\n' % eid
        self.completed_ids.append(eid)
        return b"\n"


class QuietStream(Stream):
    """Keepalives only — the case that already worked; must keep working."""

    def _next(self):
        return b": keepalive\n"


def load(tmp, **conf):
    """Import the listener fresh with a synthetic wake.env."""
    body = {"API": "http://sandbox/api/v1", "RAIL_V3": "1",
            "TOKEN_PATH": str(Path(tmp) / "token"),
            "WAKE_CMD": "/bin/true",
            "LISTEN_LOG": str(Path(tmp) / "listen.log")}
    body.update(conf)
    (Path(tmp) / "wake.env").write_text(
        "".join(f"{k}={v}\n" for k, v in body.items()))
    os.environ["SAMCLOUD_WAKE_CONF"] = str(Path(tmp) / "wake.env")
    os.environ.pop("RAIL_V3", None)      # conf, not inherited env
    spec = importlib.util.spec_from_file_location("listen_under_test", LISTEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def instrument(mod, stream, clock, unread=True):
    """Stub every leg that would touch a plane; keep the read loop real."""
    calls = {"wake": [], "inbox": 0}
    mod.time = clock
    mod.register = lambda token: {}
    mod.sync_skills = lambda token: None
    mod.sync_hooks = lambda token: None

    def _inbox(token):
        calls["inbox"] += 1
        return (unread, False)

    def _wake(reason, payload, token=None):
        calls["wake"].append(reason)

    mod.inbox_state = _inbox
    mod.wake = _wake
    mod.urllib = types.SimpleNamespace(request=types.SimpleNamespace(
        Request=lambda url, headers=None: object(),
        urlopen=lambda req, timeout=None: stream))
    return calls


def main():
    print("\n  Listener clocks under a busy stream (#627)")
    print(f"  under test: {LISTEN}")

    with tempfile.TemporaryDirectory() as tmp:
        print("\n  [1] busy stream (no keepalive ever): the clocks still run")
        clock = Clock()
        # 1s per line, 4 lines per event = one event every 4s, far denser than
        # the server's 15s keepalive threshold — so no `:` line is ever sent.
        stream = BusyStream(clock, per_line=1.0)
        mod = load(tmp, MAX_CONN_AGE_SEC="600", RAIL_V3_HEARTBEAT_MIN="5")
        calls = instrument(mod, stream, clock)
        t0 = clock.t
        try:
            ret = mod.stream_once("tok", None)
        except RuntimeError as e:
            ret, err = None, str(e)
            check("age-out fires without a single keepalive", False, err)
        else:
            check("age-out fires without a single keepalive", True)
            elapsed = clock.t - t0
            check("age-out lands at MAX_CONN_AGE, not late",
                  600 <= elapsed < 610, f"{elapsed}s")
            check("socket closed before returning", stream.closed)
            hb = [r for r in calls["wake"] if r == "v3-heartbeat"]
            # 600s at a 300s interval, clock restarted by reconcile-on-connect.
            check("v3 recovery heartbeat fires on the busy stream",
                  len(hb) >= 1, calls["wake"][:6])
            check("heartbeat honours its interval (no per-line storm)",
                  len(hb) <= 3, len(hb))

            print("\n  [2] a reconnect never cuts a half-read event")
            check("returned id is a COMPLETED block",
                  ret in stream.completed_ids, (ret, stream.completed_ids[-3:]))
            check("returned id is the LAST completed block",
                  ret == stream.completed_ids[-1],
                  (ret, stream.completed_ids[-1]))
            dispatched = [r for r in calls["wake"] if r.startswith("event ")]
            check("nothing was dispatched past the resume point",
                  bool(dispatched) and f"(id {ret})" in dispatched[-1],
                  dispatched[-1:])
            check("a partially-read block is left for the resume to replay",
                  len(stream.emitted_ids) > len(stream.completed_ids)
                  or stream.emitted_ids[-1] == ret,
                  (stream.emitted_ids[-1], ret))

        print("\n  [3] quiet stream (keepalives only) — unchanged")
        clock2 = Clock()
        stream2 = QuietStream(clock2, per_line=15.0)
        mod2 = load(tmp, MAX_CONN_AGE_SEC="600", RAIL_V3_HEARTBEAT_MIN="5")
        calls2 = instrument(mod2, stream2, clock2)
        t0 = clock2.t
        try:
            ret2 = mod2.stream_once("tok", 41)
        except RuntimeError as e:
            check("age-out still fires on an idle stream", False, str(e))
        else:
            check("age-out still fires on an idle stream", True)
            check("age-out lands at MAX_CONN_AGE", 600 <= clock2.t - t0 < 620,
                  clock2.t - t0)
            check("no event seen — resume id carried through unchanged",
                  ret2 == 41, ret2)
            check("heartbeat runs on keepalives too",
                  "v3-heartbeat" in calls2["wake"], calls2["wake"][:4])

        print("\n  [4] empty inbox never spawns a handler")
        clock3 = Clock()
        stream3 = BusyStream(clock3, per_line=1.0)
        mod3 = load(tmp, MAX_CONN_AGE_SEC="600", RAIL_V3_HEARTBEAT_MIN="5")
        calls3 = instrument(mod3, stream3, clock3, unread=False)
        try:
            mod3.stream_once("tok", None)
        except RuntimeError as e:
            check("age-out fires with an empty inbox", False, str(e))
        else:
            check("age-out fires with an empty inbox", True)
            check("heartbeat polled but woke nobody",
                  calls3["inbox"] >= 1
                  and "v3-heartbeat" not in calls3["wake"],
                  (calls3["inbox"], calls3["wake"][:4]))

        print("\n  [5] default conf (2.7.8): no heartbeat at all — the age-out is the only clock")
        clock4 = Clock()
        stream4 = BusyStream(clock4, per_line=1.0)
        mod4 = load(tmp, MAX_CONN_AGE_SEC="600")          # no RAIL_V3_HEARTBEAT_MIN
        calls4 = instrument(mod4, stream4, clock4)
        try:
            mod4.stream_once("tok", None)
        except RuntimeError as e:
            check("age-out fires with the heartbeat off", False, str(e))
        else:
            check("age-out fires with the heartbeat off", True)
            check("no heartbeat wake, no heartbeat inbox poll (no timers — doc 174)",
                  "v3-heartbeat" not in calls4["wake"] and calls4["inbox"] == 0,
                  (calls4["inbox"], [r for r in calls4["wake"] if not r.startswith("event ")]))
            check("events still dispatch as they arrive",
                  any(r.startswith("event ") for r in calls4["wake"]), calls4["wake"][:3])

    verdict = "ALL PASSED" if FAIL == 0 else "FAILURES DETECTED"
    print(f"\n  {verdict}: listener clocks {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
