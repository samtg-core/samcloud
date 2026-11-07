#!/usr/bin/env python3
"""Explicit live-plane acceptance pass; never install or unpark a listener.

Run inside the Hermes seat. Uses real notification rows, delivery rules, FIFO,
binding and receipts, but selects one named task rather than draining backlog.
Inference and agent-owned sc-say writes are real. Inspect before another pass;
this is not an automatic retry loop or evidence of listener operation.
"""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("dispatch", "inspect", "stop"))
    parser.add_argument("--task", type=int, required=True)
    parser.add_argument("--thread", type=int, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--api", required=True, help="the plane's API base, e.g. https://cloud.example.org/api/v1")
    parser.add_argument("--expected-plane-id", help="required to stop the inspected client")
    args = parser.parse_args()
    os.umask(0o077)
    seat = Path.home()
    root = args.scratch.resolve(strict=True)
    if not root.is_relative_to(seat / ".local/samcloud-acceptance"):
        raise ValueError("scratch must be an existing private acceptance directory")
    if root.stat().st_mode & 0o077:
        raise ValueError("scratch must be private")
    home = args.home.resolve(strict=True)
    # A parked production listener and this one-task pass are separate. Refuse
    # ambiguous park declarations rather than trusting the last/default value.
    park = [s.strip() for s in (seat / ".config/samcloud/wake.env").read_text().splitlines()
            if s.strip().startswith("RAIL_DRY=")]
    if park != ["RAIL_DRY=1"]:
        raise RuntimeError("production listener is not unambiguously parked")
    identity = {"task": args.task, "thread": args.thread, "home": str(home)}
    marker = root / "probe.json"
    if marker.exists():
        if json.loads(marker.read_text()) != identity:
            raise ValueError("scratch belongs to another probe")
    else:
        with marker.open("x") as f:
            json.dump(identity, f)
    state = root / "rail"
    state.mkdir(exist_ok=True, mode=0o700)
    lock = (root / "probe.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    repo = Path(__file__).resolve().parents[2]
    gateway = [sys.executable, "-u", "-c",
               "import sys; sys.path.insert(0,sys.argv[1]); from tui_gateway.entry import main; main()",
               "/opt/hermes-agent"]
    command = [sys.executable, str(Path(__file__).with_name("stream.py")),
               "--home", str(home), "--gateway-command", shlex.join(gateway)]
    os.environ.update(SAMCLOUD_WAKE_CONF="/dev/null", API=args.api.rstrip("/"),
                      TOKEN_PATH=str(seat / ".samcloud/token"), RAIL_RUNTIME="hermes",
                      RAIL_HOLDER="stream", RAIL_STREAM_CMD=shlex.join(command),
                      RAIL_STATE_DIR=str(state), RAIL_CWD=str(root),
                      LOG=str(root / "rail.log"), RAIL_DRY="0",
                      SAMCLOUD_HELPER_DIR=str(seat / ".local/bin"))
    spec = importlib.util.spec_from_file_location("cloud_probe_rail", repo / "registry/helpers/samcloud-rail.py")
    rail = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rail)
    if rail._whoami() != "hermes-codex":
        raise RuntimeError("this acceptance probe is scoped to hermes-codex")
    task = rail._api("GET", f"/tasks/{args.task}")
    if task["thread_id"] != args.thread or task["assignee"] != "hermes-codex":
        raise RuntimeError("task ownership/thread changed")
    key = f"thread-{args.thread}"
    reg = rail.load_registry()
    if set(reg) - {key}:
        raise RuntimeError("probe state contains another thread")
    entry = reg.get(key)
    if args.action == "dispatch":
        if task["status"] != "todo":
            raise RuntimeError("only an open acceptance task may dispatch")
        rows = rail._api("GET", f"/notifications?thread=task:{args.task}&unread=true&undelivered=true&limit=100")["notifications"]
        if not rows or len(rows) == 100:
            raise RuntimeError("no pending input or truncated selection; inspect, do not replay")
        if any(r["task_id"] != args.task or r["thread_id"] != args.thread for r in rows):
            raise RuntimeError("notification scope mismatch")
        if not rail.deliverable(key, rows, reg, "hermes-codex"):
            raise RuntimeError("normal rail delivery rule refused this input")
        selected = rail._receiver_rows(rows, entry, "hermes-codex")
        rail._write_0600(state / "auth.hdr", f"Authorization: Bearer {rail._token()}\n")
        try:
            verb = rail.dispatch(key, selected, reg)
        finally:
            rail.save_registry(reg)  # preserve any failed startup for inspection
        print(json.dumps({"verb": verb, "selected": [r["id"] for r in selected]}))
        entry = reg[key]
    elif args.action == "stop":
        if not entry or entry["sid"] != args.expected_plane_id:
            raise RuntimeError("stop requires the exact inspected plane id")
        if not rail.stream_alive(entry):
            raise RuntimeError("recorded process is not this live stream client")
        os.kill(entry["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 20
        while rail.stream_alive(entry) and time.monotonic() < deadline:
            time.sleep(.1)
        if rail.stream_alive(entry):
            raise RuntimeError("client has not ended; no broader kill attempted")
        rail.sweep(reg)
        rail.save_registry(reg)
    if entry:
        print(json.dumps({"entry": {k: entry[k] for k in ("sid", "pid", "state", "thread_id", "out") if k in entry},
                          "alive": rail.stream_alive(entry)}))
        path = state / "threads" / key / entry["out"]
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") in ("ready", "accepted"):
                    print(json.dumps(event))
                params = event.get("params", {})
                if params.get("type") == "message.complete":
                    payload = params.get("payload", {})
                    print(json.dumps({"terminal": {k: payload[k] for k in ("status", "persisted_turn") if k in payload}}))
    bindings = rail._api("GET", "/wake/threads")["threads"]
    print(json.dumps({"binding": [{k: b[k] for k in ("id", "thread_id", "holder", "state", "pid", "conversation_id", "home_thread_id") if k in b}
                                   for b in bindings if b["thread_id"] == args.thread]}))


if __name__ == "__main__":
    main()
