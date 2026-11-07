#!/usr/bin/env python3
"""Experimental local streaming client. No Samcloud API, auto-post or retry.

    stream.py --home PROFILE --session-id UUID -- GATEWAY ARG ...
    stream.py --home PROFILE --resume UUID -- GATEWAY ARG ...

stdin: the rail's user/message/content JSONL. stdout: native events plus local
ready/accepted records. A ready record proves the mapping, accepted proves RPC
acceptance, and neither proves a reply. --oneshot accepts plain text through EOF
and waits for the native terminal event. Streaming EOF closes this client and
its child; it does not mean work finished. No production rail selects this yet.
"""
import argparse
import json
import os
import queue
import select
import shlex
import signal
import sys
import threading

from binding import Binding
from native import Conversation, Gateway, ProtocolError


def message_text(frame):
    if (not isinstance(frame, dict) or set(frame) != {"type", "message"}
            or frame["type"] != "user" or not isinstance(frame["message"], dict)
            or set(frame["message"]) != {"role", "content"}
            or frame["message"]["role"] != "user"
            or not isinstance(frame["message"]["content"], str)
            or not frame["message"]["content"].strip()):
        raise ProtocolError("Expected a user/message/content input envelope")
    return frame["message"]["content"]


def input_lines(fd, gateway):
    """Do not hide runtime death behind a blocking readline on a quiet FIFO."""
    pending = b""
    while True:
        gateway.check_health()
        readable, _, _ = select.select([fd], [], [], .2)
        if not readable:
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            if pending:
                raise ProtocolError("Incomplete input envelope at EOF")
            return
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            yield line.decode("utf-8")


def run(args):
    plane_id = args.resume or args.session_id
    inherited = os.environ.get("SAMCLOUD_SESSION_ID")
    if inherited and inherited != plane_id:
        raise ValueError("Launch conversation differs from SAMCLOUD_SESSION_ID")
    if args.gateway_command and args.command:
        raise ValueError("Choose --gateway-command or an argv after --, not both")
    command = shlex.split(args.gateway_command) if args.gateway_command else (
        args.command[1:] if args.command[:1] == ["--"] else args.command)
    if not command:
        raise ValueError("An explicit native gateway argv is required")
    os.environ["SAMCLOUD_SESSION_ID"] = plane_id
    # A Hermes tool has its own terminal server parent; $PPID is not necessarily
    # this client. The rail's wrap-up names this exact process for shutdown.
    os.environ["SAMCLOUD_CLIENT_PID"] = str(os.getpid())
    output_lock = threading.Lock()
    terminal = queue.Queue()

    def emit(frame):
        with output_lock:
            print(json.dumps(frame, ensure_ascii=False), flush=True)

    def on_event(frame):
        emit(frame)
        params = frame.get("params")
        if (args.oneshot and frame.get("method") == "event" and isinstance(params, dict)
                and params.get("type") == "message.complete"):
            terminal.put(params)

    with Binding(args.home, plane_id, resume=bool(args.resume)) as binding:
        gateway = Gateway(command, args.home, on_event)
        try:
            conversation = Conversation(gateway, binding.stored_id)
            binding.save(conversation.stored_id)
            emit({"type": "ready", "plane_id": plane_id,
                  "stored_id": conversation.stored_id, "live_id": conversation.live_id})

            def submit(text):
                accepted = conversation.submit(text)
                emit({"type": "accepted", "plane_id": plane_id,
                      "stored_id": conversation.stored_id, "receipt": accepted})
                return accepted

            if args.oneshot:
                accepted = submit(sys.stdin.read())
                user_row_id = accepted.get("user_row_id")
                if (accepted["status"] != "streaming" or type(user_row_id) is not int
                        or user_row_id <= 0):
                    raise ProtocolError("One-shot input has no directly admitted native row")
                while True:
                    try:
                        event = terminal.get(timeout=.2)
                    except queue.Empty:
                        gateway.check_health()
                        continue
                    if event.get("session_id") != conversation.live_id:
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict) or payload.get("status") != "complete":
                        return 1
                    # A session event alone is not this input's completion.
                    # Hermes supplies committed row addresses; no text/timestamp
                    # matching and no successful exit on a partial receipt.
                    persisted = payload.get("persisted_turn")
                    if (not isinstance(persisted, dict) or persisted.get("complete") is not True
                            or type(persisted.get("user_row_id")) is not int
                            or persisted["user_row_id"] != user_row_id
                            or type(persisted.get("final_assistant_row_id")) is not int
                            or persisted["final_assistant_row_id"] <= 0):
                        raise ProtocolError("Native terminal event does not establish this input's completion")
                    # Even this committed completion is not proof of a public reply.
                    return 0
            for line in input_lines(sys.stdin.fileno(), gateway):
                submit(message_text(json.loads(line)))
            return 0
        finally:
            gateway.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--session-id")
    mode.add_argument("--resume")
    parser.add_argument("--oneshot", action="store_true")
    parser.add_argument("--gateway-command", help="explicit native gateway argv, shell-quoted but not shell-executed")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    # A deliberate holder end closes only its child. No process-name killing.
    def end(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, end)
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Don't render runtime error bodies or inputs into operator diagnostics.
        print(f"Hermes client stopped ({type(error).__name__}); no replay or automatic reply", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
