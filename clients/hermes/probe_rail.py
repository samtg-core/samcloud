#!/usr/bin/env python3
"""Real rail FIFO + installed Hermes RPC/storage; fake plane, no inference.

Reuses the loopback fixture, with a private profile and network-disabled native
gateway. Production config, profiles, bindings and credentials are untouched.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import tempfile
import time

from probe_installed import BOOT, rows

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "sandbox"))
from test_native_stream_holder import NativeHolder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="/opt/hermes-agent")
    parser.add_argument("--scratch", required=True)
    args = parser.parse_args()
    evidence = Path(tempfile.mkdtemp(prefix="native-rail-", dir=args.scratch))
    fixture = NativeHolder()
    try:
        fixture.setUp()
        fixture.rail.STREAM_CMD = shlex.join([
            sys.executable, str(Path(__file__).with_name("stream.py")),
            "--home", str(fixture.home), "--gateway-command",
            shlex.join([sys.executable, "-u", "-c", BOOT, args.repo])])

        def accepted(entry, count):
            output = fixture.rail.STATE_DIR / "threads" / fixture.key / entry["out"]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                records = []
                for line in output.read_text().splitlines():
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("type") == "accepted":
                        records.append(record)
                if len(records) >= count:
                    return records
                time.sleep(.05)
            raise AssertionError("native acceptance not observed")

        assert fixture.dispatch(1) == "wake"
        first = dict(fixture.reg[fixture.key])
        one = accepted(first, 1)[0]
        assert one["receipt"]["status"] == "streaming"
        stored = one["stored_id"]
        assert rows(fixture.home, stored) == [("user", "WAKE 1", 1)]
        assert fixture.dispatch(2) == "continue"
        two = accepted(first, 2)[1]
        assert two["receipt"]["status"] == "queued"
        assert fixture.reg[fixture.key]["pid"] == first["pid"]
        assert "CONTINUE 2" in [text for _,text,active in rows(fixture.home, stored) if active]
        # Only this probe's launcher process group, never a production process.
        os.killpg(fixture.processes[0].pid, signal.SIGKILL)
        fixture.processes[0].wait(10)
        assert fixture.dispatch(3) == "resume"
        resumed = dict(fixture.reg[fixture.key])
        three = accepted(resumed, 1)[0]
        assert resumed["sid"] == first["sid"]
        assert resumed["pid"] != first["pid"]
        assert three["stored_id"] == stored
        history = rows(fixture.home, stored)
        assert "WAKE 1" in [text for _,text,active in history if active]
        # Characterize the installed runtime honestly, not a patched overlay.
        assert ("user", "CONTINUE 2", 0) in history
        assert "RESUME 3" in [text for _,text,active in history if active]
        assert not any(role == "assistant" for role,_,_ in history)
        assert not any(path.endswith("/comments") for _,path,_ in fixture.calls)
        assert sum(path.endswith("/notifications/delivered") for _,path,_ in fixture.calls) == 3
        result = {"scratch":str(evidence), "plane_id":first["sid"], "stored_id":stored,
                  "real_rail_fifo":True, "same_client_continue":True,
                  "exact_id_cold_resume":True, "busy_input_retired_by_installed_runtime":True,
                  "inference_performed":False, "live_plane_contacted":False}
        (evidence / "result.json").write_text(json.dumps(result, indent=2)+"\n")
    finally:
        for process in getattr(fixture, "processes", []):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(10)
        try:
            if hasattr(fixture, "home"):
                shutil.copytree(fixture.home, evidence / "profile")
                if fixture.rail.STATE_DIR.exists():
                    shutil.copytree(fixture.rail.STATE_DIR, evidence / "rail",
                                    ignore=shutil.ignore_patterns("in.fifo"))
        finally:
            fixture.doCleanups()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
