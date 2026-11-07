#!/usr/bin/env python3
"""Every attribute read off an Event must be a field Event actually has.

Why this exists. `_event_for_conversation` read `event.payload` from 0.12.20
until 0.12.28. Event has no `payload` — the field is `data`, and `to_sse` names
its LOCAL variable `payload` while reading `self.data`, which is where the slip
came from. The surrounding `except (TypeError, ValueError)` does not catch
AttributeError, so the clause raised out of an SSE generator and 500'd the
stream rather than answering it. Every failure wrote a full ASGI stack trace to
the container log, at roughly 200 MB/hour, into the same unbounded log that had
just filled the disk and taken the plane down (#814).

Three days, no test, and the symptom that surfaced it was log volume rather than
the silence it caused. A typo on a dataclass attribute is worth a mechanical
check: it costs nothing and it does not rely on anyone exercising the path.

Run: python3 sandbox/test_event_fields.py
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent / "registry"
PASS, FAIL = 0, 0


def check(desc, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"      \033[0;32m✓\033[0m {desc}")
    else:
        FAIL += 1
        print(f"      \033[0;31m✗\033[0m {desc}")
        if detail:
            print(f"        {detail}")


def event_members():
    """Field names + methods on the Event dataclass, read from the source."""
    tree = ast.parse((ROOT / "events.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Event":
            fields = {n.target.id for n in node.body if isinstance(n, ast.AnnAssign)}
            methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
            return fields | methods
    return set()


def event_attr_reads(path):
    """(attr, line) for every `event.<attr>` / `ev.<attr>` read in a file."""
    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in ("event", "ev")):
            out.append((node.attr, node.lineno))
    return out


def main():
    print("\n  Event attribute access\n")
    members = event_members()
    check("Event's fields are readable from source", bool(members), f"got {members}")
    check("`data` is the payload field", "data" in members)
    check("`payload` is NOT a field — the bug's exact shape", "payload" not in members)

    for path in sorted(ROOT.glob("*.py")):
        if path.name == "events.py":
            continue
        for attr, line in event_attr_reads(path):
            check(f"{path.name}:{line} reads event.{attr}", attr in members,
                  f"Event has no `{attr}`. Fields/methods: {sorted(members)}")

    print(f"\n  {'ALL PASSED' if not FAIL else 'FAILED'}: "
          f"event fields {PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
