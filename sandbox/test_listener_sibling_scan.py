#!/usr/bin/env python3
"""The listener must never install the rail over itself.

`_sibling_handler_specs()` (listener 2.7.11, #796) walks every file in the
seat's bin directory and refreshes any that carries one of our version stamps,
so a handler the seat stopped pointing at cannot freeze. The listener spells
its OWN stamp `RAIL_VERSION` — the same name the rail uses — so the rail's
pattern matched the listener and the scan wrote the rail over it.

Measured 2026-09-22: twelve seats across the plane host, studio and mediahost were
carrying rail 3.13.0 at `~/.local/bin/samcloud-listen`. None of them showed it:
the listener already resident kept running from the bytes it was loaded from,
so a seat only goes deaf at its next restart. One seat's did, in front of us,
when its LaunchDaemon was installed — 52 spawns, exit 0 each time, silent.

Run: python3 sandbox/test_listener_sibling_scan.py
"""
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
LISTENER = REPO / "registry/helpers/samcloud-listen.py"
RAIL = REPO / "registry/helpers/samcloud-rail.py"


def main() -> int:
    print("\n  listener sibling scan")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sibscan-"))
    try:
        bindir = tmp / ".local/bin"
        bindir.mkdir(parents=True)
        shutil.copy(LISTENER, bindir / "samcloud-listen")
        shutil.copy(LISTENER, bindir / "samcloud-listen.stg")   # a copy under another name
        shutil.copy(RAIL, bindir / "samcloud-rail.prod")
        shutil.copy(REPO / "registry/helpers/samcloud-adapter.py",
                    bindir / "samcloud-adapter")
        (bindir / "someones-own-harness").write_text("#!/bin/sh\necho hi\n")

        os.environ["HOME"] = str(tmp)
        os.environ["WAKE_CMD"] = str(bindir / "samcloud-rail.prod")
        os.environ["SAMCLOUD_API"] = "http://127.0.0.1:1/api/v1"
        sys.argv = [str(bindir / "samcloud-listen")]
        spec = importlib.util.spec_from_file_location("_sl_under_test", LISTENER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        sibs = [os.path.basename(p) for p, _v, _pat, _s in mod._sibling_handler_specs()]
        failed = 0

        def check(label, ok, detail=""):
            nonlocal failed
            print(f"  {'✓' if ok else '✗'} {label}")
            if not ok:
                failed += 1
                print(f"      {detail}")

        check("the running listener is not in the update set",
              "samcloud-listen" not in sibs, f"siblings={sibs}")
        check("nor is a listener copied under another name",
              "samcloud-listen.stg" not in sibs, f"siblings={sibs}")
        check("the idle adapter still is — the scan has not been disarmed",
              "samcloud-adapter" in sibs, f"siblings={sibs}")
        check("a stampless harness of someone else's is never touched",
              "someones-own-harness" not in sibs, f"siblings={sibs}")
        check("and the active handler is skipped as before",
              "samcloud-rail.prod" not in sibs, f"siblings={sibs}")

        print()
        if failed:
            print(f"  FAILED: {failed} failed")
            return 1
        print("  ALL PASSED: listener sibling scan 5 passed, 0 failed")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
