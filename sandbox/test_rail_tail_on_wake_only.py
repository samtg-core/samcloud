#!/usr/bin/env python3
"""A tail rides a delivery that starts a process; a running holder gets what
was added (rail 3.13.4).

The plane attaches the previous three comments to every mention row
(`payload.thread_tail`, 0.8.6). The rail's digest used to quote them on every
delivery — wake, continue and one-off alike. On 2026-10-08 four holders on
#904 mentioned each other on nearly every comment, so nearly every continue
carried the previous three quoted whole: of the 1.94M characters the services
holder was fed on that thread, 1.32M were re-reading (doc 355).

The rule since 2026-10-09: the tail belongs on a wake; otherwise a delivery
carries only what was added, each row delivered once.

Drives the REAL registry/helpers/samcloud-rail.py with $HOME redirected and the
thread cache seeded, so nothing reaches a plane. No registry, no network.

Run from repo root: python3 sandbox/test_rail_tail_on_wake_only.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAIL = Path(os.environ.get("RAIL_UNDER_TEST") or REPO / "registry/helpers/samcloud-rail.py")

PASS, FAIL = 0, 0


def check(desc, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"      \033[0;32m✓\033[0m {desc}")
    else:
        FAIL += 1
        print(f"      \033[0;31m✗\033[0m {desc}  {detail}")


def load(home: Path):
    """Import the rail with a wake.env of our making and $HOME redirected."""
    (home / ".config/samcloud").mkdir(parents=True, exist_ok=True)
    (home / ".samcloud").mkdir(parents=True, exist_ok=True)
    (home / ".samcloud/token").write_text("sc_agent_test")
    (home / ".config/samcloud/wake.env").write_text(
        "API=http://127.0.0.1:9\nRAIL_STATE_DIR=" + str(home / ".samcloud/rail") + "\n")
    os.environ["HOME"] = str(home)
    os.environ["SAMCLOUD_WAKE_CONF"] = str(home / ".config/samcloud/wake.env")
    for k in list(os.environ):
        if k.startswith("RAIL_"):
            del os.environ[k]
    spec = importlib.util.spec_from_file_location("rail_tail_test", RAIL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TAIL = [
    {"author": "claude-studio-services", "body": "TAIL-ONE the first earlier comment"},
    {"author": "claude-containers", "body": "TAIL-TWO the second earlier comment\nwith a second line"},
    {"author": "samclaude-admin", "body": "TAIL-THREE the third earlier comment"},
]


def mention_row(rid: int, body: str) -> dict:
    return {"id": rid, "thread_id": 1782, "ticket_id": 904, "event_type": "ticket.mentioned",
            "actor": "claude-services", "created_at": "2026-10-08 02:11:51",
            "payload": {"summary": "models-cs cuts off streams", "by": "claude-services",
                        "body": body, "comment_id": rid, "thread_tail": TAIL}}


def main():
    with tempfile.TemporaryDirectory() as td:
        rail = load(Path(td))
        key = "thread-1782"
        rail._THREAD_INFO[key] = {"id": 1782, "root": "ticket:904", "state": "open",
                                  "title": "models-cs cuts off streams", "open_members": True,
                                  "owners": ["claude-services"]}
        rows = [mention_row(9118, "NEW-ROW @claude-studio-services your figure is off by 2x")]

        print("  the digest")
        text, _ = rail._digest(rows)
        check("by default the digest quotes the tail (a wake's shape)",
              "TAIL-ONE" in text and "> claude-containers: TAIL-TWO" in text and "TAIL-THREE" in text)
        text, _ = rail._digest(rows, tail=False)
        check("with tail=False the digest carries the row alone", "NEW-ROW" in text and "TAIL-" not in text)
        check("the row's own head survives", "claude-services (02:11 UTC)" in text)

        print("  the three prompts")
        cont = rail.continue_prompt(key, rows)
        check("a continue into a running holder carries no tail", "NEW-ROW" in cont and "TAIL-" not in cont,
              cont[:200])
        check("the continue still ends with the footer", "sc-say ticket 904 to reply" in cont)
        wake = rail.wake_prompt(key, rows, resumed=True)
        check("a wake (resume) carries the tail", "NEW-ROW" in wake and "TAIL-ONE" in wake and "TAIL-THREE" in wake)
        one = rail.oneshot_prompt(key, rows)
        check("a one-off carries the tail", "NEW-ROW" in one and "TAIL-ONE" in one)

        two = [mention_row(9118, "NEW-ROW first"), mention_row(9123, "NEW-ROW second")]
        cont2 = rail.continue_prompt(key, two)
        check("a batched continue carries both rows and no tail",
              cont2.count("NEW-ROW") == 2 and "TAIL-" not in cont2 and "2 messages on" in cont2
              and cont2.startswith("You are "))

        print("  the subscriber")
        src = (REPO / "registry/helpers/samcloud-subscribe.py").read_text()
        check("the subscriber no longer renders thread_tail",
              'p.get("thread_tail")' not in src)
        check("the subscriber's stamp moved past 2.9.0", "# SUBSCRIBE_VERSION: 2.9.0" not in src)

    print()
    if FAIL:
        print(f"  {FAIL} FAILED, {PASS} passed")
        sys.exit(1)
    print(f"  ALL PASSED ({PASS})")


if __name__ == "__main__":
    main()
