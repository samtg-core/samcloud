#!/usr/bin/env python3
"""Exercise the actual stream CLI against native Hermes with inference disabled.

Each child gets a new process group and private profile; SIGKILL is restricted to
those groups. No production profile, API, credential, config or runtime is used.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from probe_installed import BOOT, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="/opt/hermes-agent")
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--overlay")
    parser.add_argument("--expect-retained", action="store_true")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="native-stream-", dir=args.scratch))
    processes, logs, readers = [], [], []
    env = dict(os.environ)
    env.pop("SAMCLOUD_SESSION_ID", None)
    env.pop("SAMCLOUD_HOLDER", None)

    def launch(home, sid, resume=False):
        home.mkdir(mode=0o700, exist_ok=True)
        log = open(root / f"child-{len(processes)}.log", "w")
        logs.append(log)
        command = [sys.executable, str(Path(__file__).with_name("stream.py")), "--home", str(home),
                   "--resume" if resume else "--session-id", sid,
                   "--", sys.executable, "-u", "-c", BOOT, args.repo]
        if args.overlay:
            command.append(args.overlay)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=log, text=True, env=env, start_new_session=True)
        processes.append(process)
        frames = queue.Queue()
        def read():
            try:
                for line in process.stdout:
                    frames.put(json.loads(line))
            finally:
                frames.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        readers.append(reader)
        return process, frames

    def frame_of(frames, kind):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            frame = frames.get(timeout=max(.01, deadline-time.monotonic()))
            assert frame is not None, "client stopped before the requested record"
            if frame.get("type") == kind:
                return frame
        raise TimeoutError(kind)

    def submit(process, frames, text):
        process.stdin.write(json.dumps({"type":"user","message":{"role":"user","content":text}})+"\n")
        process.stdin.flush()
        return frame_of(frames, "accepted")

    try:
        rail, operator = root / "rail", root / "operator"
        sid = str(uuid.uuid4())
        first, frames = launch(rail, sid)
        ready = frame_of(frames, "ready")
        stored = ready["stored_id"]
        mapping = json.loads((rail / ".samcloud-client" / "conversations" / f"{sid}.json").read_text())
        assert mapping["stored_id"] == stored
        assert mapping["plane_id"] == sid
        receipt = submit(first, frames, "STREAM-A before inference")
        assert receipt["receipt"]["status"] == "streaming"
        assert rows(rail, stored) == [("user", "STREAM-A before inference", 1)]
        # A second client cannot own the same mapping even before inference.
        duplicate, duplicate_frames = launch(rail, sid, resume=True)
        assert duplicate.wait(10) != 0
        assert duplicate_frames.get(timeout=5) is None
        other, other_frames = launch(operator, sid)
        other_ready = frame_of(other_frames, "ready")
        submit(other, other_frames, "OPERATOR-ONLY")
        assert not rows(rail, other_ready["stored_id"])
        assert not rows(operator, stored)
        assert submit(first, frames, "STREAM-B accepted busy")["receipt"]["status"] == "queued"
        # Kill only the process group created by this probe, including its child.
        os.killpg(first.pid, signal.SIGKILL)
        first.wait(10)
        resumed, resumed_frames = launch(rail, sid, resume=True)
        assert frame_of(resumed_frames, "ready")["stored_id"] == stored
        active = [content for _, content, enabled in rows(rail, stored) if enabled]
        assert "STREAM-A before inference" in active
        assert ("STREAM-B accepted busy" in active) == args.expect_retained
        assert not any(role == "assistant" for role, _, _ in rows(rail, stored))
        print(json.dumps({"scratch":str(root), "plane_id":sid, "stored_id":stored,
                          "mapping_before_input":True, "second_writer_refused":True,
                          "profiles_separate":True, "exact_resume_after_sigkill":True,
                          "busy_input_active_after_restart":args.expect_retained,
                          "inference_performed":False}, indent=2))
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(10)
            process.stdin.close()
        for reader in readers:
            reader.join(5)
        for process in processes:
            process.stdout.close()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
