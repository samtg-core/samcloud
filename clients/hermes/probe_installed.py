#!/usr/bin/env python3
"""Real stdio/cold-process probe against installed Hermes, with inference disabled.

Run using Hermes' own Python. State is retained in a new private scratch home.
This is a characterization fixture, not the production launch command.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

from native import Conversation, Gateway


BOOT = r'''
import os, socket, sys, threading
os.environ["HERMES_TEST_ISOLATION"] = "1"
sys.path.insert(0, sys.argv[1])
if len(sys.argv) > 2:
    sys.path.insert(0, sys.argv[2])
    # Hermes startup inserts its own checkout into sys.path. Preload the one
    # explicitly selected overlay module so the probe actually tests it.
    import hermes_state_sessions
    assert os.path.realpath(hermes_state_sessions.__file__) == os.path.realpath(
        os.path.join(sys.argv[2], "hermes_state_sessions.py"))
def forbidden(*a, **kw):
    raise AssertionError("No network allowed in the native delivery probe")
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
from tui_gateway import server, entry
from hermes_cli import model_switch_providers
# Keep all RPC/session/storage code real. Disable model construction, execution
# and optional startup discovery: this checks delivery, not inference or health.
server._schedule_agent_build = lambda *a, **kw: None
server._start_agent_build = lambda *a, **kw: None
server._restart_completed_failed_agent_build = lambda *a: False
server._run_after_agent_ready = lambda *a, **kw: threading.Event().wait()
server._maybe_schedule_auto_continue = lambda *a, **kw: None
entry.ensure_mcp_discovery_started = lambda: None
model_switch_providers.prewarm_picker_cache_async = lambda: None
entry.main()
'''


def rows(home, key):
    db = sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True)
    try:
        return db.execute("SELECT role,content,active FROM messages WHERE session_id=? ORDER BY id", (key,)).fetchall()
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="/opt/hermes-agent")
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--overlay", help="isolated candidate runtime modules; never patches the install")
    parser.add_argument("--expect-retained", action="store_true",
                        help="require queued input in active context after restart")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="native-stdio-", dir=args.scratch))
    rail, operator = root / "rail", root / "operator"
    rail.mkdir(mode=0o700)
    operator.mkdir(mode=0o700)
    command = [sys.executable, "-u", "-c", BOOT, args.repo]
    if args.overlay:
        command.append(args.overlay)
    events = []
    log = open(root / "gateway.log", "w")
    gateways = []

    def gateway(home):
        g = Gateway(command, home, events.append, stderr=log)
        gateways.append(g)
        return g

    try:
        first = gateway(rail)
        a = Conversation(first)
        receipt = a.submit("STDIO-A: receive before any inference")
        assert receipt["status"] == "streaming" and type(receipt.get("user_row_id")) is int, receipt
        assert rows(rail, a.stored_id) == [("user", "STDIO-A: receive before any inference", 1)]
        # Independent profile and process: an operator chat cannot become A.
        other = Conversation(gateway(operator))
        other.submit("OPERATOR-ONLY")
        assert not rows(rail, other.stored_id)
        assert not rows(operator, a.stored_id)
        busy = a.submit("STDIO-B: queued while A has not inferred")
        assert busy["status"] == "queued"
        before = rows(rail, a.stored_id)
        assert any(content == "STDIO-B: queued while A has not inferred" and active
                   for _, content, active in before)
        old_pid = first.process.pid
        # A real abrupt process death, not clearing an in-memory session table.
        first.process.kill()
        first.process.wait(10)
        first.close()
        gateways.remove(first)
        resumed = Conversation(gateway(rail), a.stored_id)
        assert resumed.stored_id == a.stored_id
        after = rows(rail, a.stored_id)
        assert any(content == "STDIO-A: receive before any inference" and active for _, content, active in after)
        b_rows = [(content, active) for _, content, active in after
                  if content == "STDIO-B: queued while A has not inferred"]
        assert sum(bool(active) for _, active in b_rows) == int(args.expect_retained), b_rows
        assert not any(role == "assistant" for role, _, _ in after)
        print(json.dumps({"scratch":str(root), "old_pid":old_pid,
                          "stored_session_id":a.stored_id,
                          "acceptance_before_inference":True,
                          "separate_profile_stores":True,
                          "native_resume_after_process_death":True,
                          "busy_message_active_after_restart":args.expect_retained,
                          "inference_performed":False}, indent=2))
    finally:
        for g in gateways:
            g.close()
        log.close()


if __name__ == "__main__":
    main()
