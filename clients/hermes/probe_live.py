#!/usr/bin/env python3
"""Bounded, explicitly invoked Ada canary. Hermes posts via its own tool.

No plane API or result-posting controller here. Creates one native conversation,
waits for its terminal event, restarts the client and resumes that exact ID.
The caller must verify capacity/work-wins and the parked rail before running.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import time
import uuid

from native import Conversation, Gateway


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--repo", default="/opt/hermes-agent")
    parser.add_argument("--continue-record", type=Path,
                        help="explicit manual continuation of an inspected existing canary")
    parser.add_argument("--prompt-file", type=Path)
    args = parser.parse_args()
    if bool(args.continue_record) != bool(args.prompt_file):
        parser.error("--continue-record and --prompt-file are required together")
    if args.continue_record:
        root = args.continue_record.parent
        record = json.loads(args.continue_record.read_text())
        assert record["home"] == args.home
        assert record["state"] not in ("starting", "continuing", "first_submit", "resumed_submit")
    else:
        root = Path(tempfile.mkdtemp(prefix="native-live-", dir=args.scratch))
        record = {"state": "starting", "plane_session": str(uuid.uuid4()),
                  "nonce": "NATIVE-" + uuid.uuid4().hex[:10], "home": args.home}
    os.environ["SAMCLOUD_SESSION_ID"] = record["plane_session"]
    # This explicit canary is not a rail-born holder. Do not claim that kind or
    # teach it to terminate a rail parent on wrap-up; the real rail sets its own
    # holder marker when the installed client is launched through that path.
    os.environ.pop("SAMCLOUD_HOLDER", None)
    os.environ["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + os.environ.get("PATH", "")
    # Hermes named profiles intentionally give tools a different HOME. Match
    # the rail's holder environment: explicit scoped header path, never a token
    # copied into the profile or a bearer passed in argv.
    header = Path.home() / ".samcloud/token.hdr"
    if not header.is_file():
        raise RuntimeError("seat auth header is absent; cannot run the reply canary")
    os.environ["SAMCLOUD_AUTH_HDR"] = str(header)
    events = queue.Queue()
    command = [sys.executable, "-u", "-c",
               "import sys; sys.path.insert(0,sys.argv[1]); from tui_gateway.entry import main; main()",
               args.repo]

    def save():
        (root / "result.json").write_text(json.dumps(record, indent=2) + "\n")

    def event(frame):
        params = frame.get("params", {})
        if params.get("type") in ("message.complete", "error"):
            events.put(params)

    def wait_turn(g, live_id):
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            try:
                e = events.get(timeout=1)
            except queue.Empty:
                if g.process.poll() is not None:
                    raise RuntimeError("native gateway exited before terminal event")
                continue
            if e.get("session_id") != live_id:
                continue
            if e["type"] == "error":
                record["runtime_error_seen"] = True
                save()
                continue
            payload = e.get("payload", {})
            if payload.get("status") != "complete":
                raise RuntimeError("Hermes turn did not complete successfully")
            return {k: payload[k] for k in ("status", "text", "persisted_turn") if k in payload}
        raise TimeoutError("bounded canary expired; inspect exact session, do not replay")

    save()
    print(json.dumps({"scratch": str(root), "state": record["state"]}), flush=True)
    g = None
    try:
        with (root / "gateway.log").open("a") as log:
            g = Gateway(command, args.home, event, stderr=log)
            if args.continue_record:
                conversation = Conversation(g, record["stored_session_id"])
                assert conversation.stored_id == record["stored_session_id"]
                record["state"] = "continuing"
                attempt = {"prompt_file": str(args.prompt_file)}
                record.setdefault("continuations", []).append(attempt)
                save()
                attempt["acceptance"] = conversation.submit(args.prompt_file.read_text())
                save()
                attempt["terminal"] = wait_turn(g, conversation.live_id)
                record["state"] = "runtime_turns_finished_requires_plane_verification"
                save()
                print(json.dumps(record, indent=2), flush=True)
                return
            conversation = Conversation(g)
            record.update(stored_session_id=conversation.stored_id, state="first_submit")
            save()
            receipt = conversation.submit(
                "Controlled Samcloud native-client acceptance canary, task 819. "
                "Do only this bounded task, no other backlog or configuration work. "
                f"Remember this conversation code: {record['nonce']}. "
                "Use the terminal tool to run uname -s and compute 37 * 43. "
                "Then use your installed sc-say to post ONE comment on task 819: "
                "'Native Hermes canary A: <observed OS>, <computed product>, <conversation code>'. "
                "Use a scoped file if sc-say needs -f. Do not read credentials or configs. "
                "Do not infer successful posting from generated text; check the command result. "
                "After posting, finish with a short confirmation. No subagents.")
            record["first_acceptance"] = receipt
            save()
            record["first_terminal"] = wait_turn(g, conversation.live_id)
            record["state"] = "first_complete"
            save()
            g.close()
            g = None
            g = Gateway(command, args.home, event, stderr=log)
            resumed = Conversation(g, record["stored_session_id"])
            assert resumed.stored_id == record["stored_session_id"]
            record["state"] = "resumed_submit"
            record["second_acceptance"] = resumed.submit(
                "Second half of the same bounded task 819 canary, after client restart. "
                "Read the conversation code from our existing history (do not inspect files for it). "
                "Use sc-say to post exactly one task 819 comment: "
                "'Native Hermes canary B: resumed <conversation code>'. "
                "No other work or credential/config reads; check posting succeeded, then finish.")
            save()
            record["second_terminal"] = wait_turn(g, resumed.live_id)
            record["state"] = "runtime_turns_finished_requires_plane_verification"
            save()
            print(json.dumps(record, indent=2), flush=True)
    except BaseException as exc:
        record["state"] = "stopped_requires_inspection"
        record["failure_type"] = type(exc).__name__
        save()
        raise
    finally:
        if g is not None:
            g.close()


if __name__ == "__main__":
    main()
