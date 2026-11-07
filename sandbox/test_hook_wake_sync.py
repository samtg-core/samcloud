#!/usr/bin/env python3
"""The repo's git hooks touch this repository and nothing else.

A holder works from the rail: its directory is RAIL_CWD, its skills are the
served ones, and the code it changes is its own clone. The hooks used to
reset a rail-owned clone (~/.local/samcloud/wake-repo) to a fetched branch on
every commit and merge; that clone followed `staging` for five weeks after
nothing went through it, and holders do not start in it. The sync is gone, so a
commit or merge here never writes outside the committing checkout.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / ".githooks"

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        failed += 1
        print(f"  \033[31m✗\033[0m {name}  {detail}")


def main():
    print(f"hooks stay inside the repository ({HOOKS})")
    for name in ("post-commit", "post-merge", "pre-commit"):
        src = (HOOKS / name).read_text()
        check(f"{name} does not sync a rail checkout",
              "wake-sync" not in src and "wake-repo" not in src)
    check("wake-sync.sh is gone", not (HOOKS / "wake-sync.sh").exists())
    if failed:
        print(f"\n  FAILED: {failed} failed, {passed} passed")
        raise SystemExit(1)
    print(f"\n  ALL PASSED: hooks stay inside the repository {passed} passed, 0 failed")


if __name__ == "__main__":
    main()
