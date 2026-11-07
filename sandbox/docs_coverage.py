#!/usr/bin/env python3
"""docs_coverage — does the written record still describe the running system?

Run from the repo root:  python3 sandbox/docs_coverage.py

Why this exists (2026-09-02, #588 fallout): the rail-v3 cutover shipped over a
month of work — thread-held sessions, sc-say, header-file auth, a new broker —
and NONE of it reached docs/ or the skills. Meanwhile docs/ still asserted
things retired in May. An agent reading the canonical docs would have been
misinformed on both sides at once, which is exactly how a session ends up
"discovering" that a shipped feature never deployed.

Design rule, learned the hard way: DERIVE from the artifacts wherever possible.
Every check below either parses the real thing (helper version stamps, FastAPI
routes) or names a concept that must be curated by a human. The curated lists
are deliberately small and each entry carries a reason, because a second
un-derived source of truth is how drift starts.

Exit 0 = the record matches the system. Exit 1 = drift, itemised.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SKILLS = ROOT / ".claude" / "skills"
CLAUDE_MD = ROOT / "CLAUDE.md"
HELPERS = ROOT / "registry" / "helpers"
MAIN_PY = ROOT / "registry" / "main.py"

# Concepts that are load-bearing for anyone operating this system today.
# (term-regex, human name, reason it must be documented)
REQUIRED_CONCEPTS = [
    (r"rail v3|RAIL_V3|thread-held|held session",
     "rail v3 / thread-held sessions",
     "the prod session model since #588 — replaces cold one-shot wakes"),
    (r"\bholder\b",
     "holder",
     "the unit that owns a thread; 'session' alone is now ambiguous"),
    (r"sc-say|helpers/say",
     "sc-say",
     "the fleet's write primitive; agents are told to use it"),
    (r"auth\.hdr|SAMCLOUD_AUTH_HDR",
     "header-file auth",
     "how every v3 seat authenticates; bearer never in argv (#582)"),
    (r"claim",
     "thread claims",
     "routes replies to a live surface instead of a cold wake (#199)"),
    (r"subscribe|subscriber",
     "tactical subscriber",
     "how an interactive session hears replies (#227)"),
    (r"aws-broker|gh-broker|google-broker|adb-broker",
     "credential brokers",
     "short-lived scoped credentials; nothing static on disk"),
]

# Things that were REMOVED. Asserting them actively misleads.
# (term-regex, human name, when/why it went)
RETIRED_TERMS = [
    (r"issues\.yaml", "issues.yaml",
     "deleted 2026-06-12 — registry tickets are the only tracker"),
    (r"console-deploy", "console-deploy.sh",
     "legacy bare-process flow; a merge into main is the deploy"),
    (r"(bump|rais\w*)\s+(the\s+)?`?(?<![A-Z_])VERSION`?\b"
     r"|`?(?<![A-Z_])\bVERSION`?\s+(bump|train|changed|unchanged|did not change|change\b)"
     r"|on\s+`?VERSION`?\s+change",
     "raising VERSION to release",
     "0.12.96 — a merge into main is the release; post-merge assigns the version "
     "from the deploy host's tags, and there is no VERSION file"),
    (r"com\.samcloud\.(sslh|caddy|registry|console|srv)",
     "pre-container LaunchDaemons",
     "removed 2026-05-06 — the control plane is containerised"),
    (r"\bconnected\b(?=[^.\n]*\bsurface\b)|\bsurface\b(?=[^.\n]*\bconnected\b)",
     "`connected` as a surface flag",
     "deleted 0.12.13 — a surface is never asked to prove it is listening; it "
     "holds until it says {\"ended\": true}"),
]

# Files allowed to contain retired terms because they are dated records,
# not instructions. History is fine; standing guidance is not.
# A line that names a retired thing IN ORDER to retire it is doing its job.
RETIRED_MARKER = re.compile(
    r"\b(deleted|removed|retired|legacy|deprecat\w*|no longer|don't|do not|"
    r"never|was|were|used to|old|former\w*|superseded|history|instead of)\b",
    re.I)

HISTORICAL = re.compile(
    r"(Assessment|Interview Synthesis|Progress Report|Observations"
    r"|Production Bugs)", re.I)


# The ONE skill the registry serves to the fleet (GET /api/v1/skills/...).
# Every enrolled agent installs it, so staleness here is fleet-wide by
# construction — it gets stricter checks than anything else in the tree.
FLEET_SKILL = SKILLS / "samcloud-agent" / "SKILL.md"

# Safety-critical and operational facts the fleet skill must carry. UNSAFE
# omissions first: an agent that never learns the auth rule will invent one,
# and the one it invents puts the bearer in argv (#582).
FLEET_REQUIRED = [
    (r"-H @|SAMCLOUD_AUTH_HDR|token\.hdr", "header-file auth",
     "UNSAFE if missing — agents invent bearer-in-argv, world-readable in ps"),
    (r"argv|command line|process table|\bps\b", "the token-never-on-a-command-line rule",
     "UNSAFE if missing — the rule that closed the #582 exposure (stated as the action, per tone.md)"),
    (r"\bthread\b.*\bhold|you hold|holder", "the held-thread model",
     "agents reason wrongly about their own lifecycle without it"),
    (r"sc-say|helpers/say", "sc-say", "the write tool agents are told to use"),
    (r"kill -TERM \$PPID|end your process", "how a holder ends",
     "a stream session does not end by finishing a turn; a holder that never ends holds its thread forever"),
    (r"POST /api/v1/tickets|file a ticket", "the way out when something is wrong",
     "every page ends with the same call (tone.md rule 10)"),
]
# 3.0.0: `ack` is no longer required — delivery is receipt (doc 174) and the
# skill teaches no ack discipline on purpose.

# Progressive disclosure: a SKILL.md is an entry point, not a manual. Bodies
# above this are a smell — chunk them into files loaded on demand instead.
SKILL_ENTRY_MAX_LINES = 120


def skill_files():
    return sorted(SKILLS.rglob("SKILL.md")) if SKILLS.is_dir() else []


def check_skills(failures, warnings):
    """The skills tree: fleet-skill correctness, chunk integrity, entry size."""
    # 1. The fleet-distributed skill must carry the critical facts.
    if FLEET_SKILL.exists():
        body = read(FLEET_SKILL)
        for pat, name, why in FLEET_REQUIRED:
            if not re.search(pat, body, re.I):
                failures.append(
                    f"FLEET SKILL MISSING: {FLEET_SKILL.name} (served to every "
                    f"enrolled agent) does not teach {name} — {why}")
    else:
        failures.append("FLEET SKILL ABSENT: .claude/skills/samcloud-agent/SKILL.md "
                        "is what /api/v1/skills serves — it must exist")

    # 2. Progressive disclosure only works if the chunks resolve. A SKILL.md
    # pointing at a file that does not exist silently teaches nothing.
    for sk in skill_files():
        for m in re.finditer(r"\[[^\]]+\]\(([^)]+\.md)\)|`([A-Za-z0-9_./-]+\.md)`",
                             read(sk)):
            ref = (m.group(1) or m.group(2) or "").strip()
            if not ref or ref.startswith(("http", "/api/", "#")):
                continue
            target = (sk.parent / ref)
            if not target.exists() and not (ROOT / ref).exists():
                failures.append(
                    f"DANGLING CHUNK: {sk.relative_to(ROOT)} references "
                    f"'{ref}' which does not exist")

    # 3. Entry points should stay lean enough to load cheaply.
    for sk in skill_files():
        n = len(read(sk).splitlines())
        if n > SKILL_ENTRY_MAX_LINES:
            warnings.append(
                f"SKILL ENTRY LARGE: {sk.relative_to(ROOT)} is {n} lines "
                f"(>{SKILL_ENTRY_MAX_LINES}) — consider chunking into files "
                f"loaded on demand")


def corpus():
    """Every file that an agent or human would read as current guidance."""
    files = []
    if DOCS.is_dir():
        files += [p for p in DOCS.glob("*.md") if not HISTORICAL.search(p.name)]
    if SKILLS.is_dir():
        files += list(SKILLS.rglob("*.md"))
    if CLAUDE_MD.exists():
        files.append(CLAUDE_MD)
    return files


def read(p):
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def served_helpers():
    """Helper artifacts that carry a version stamp = things the fleet runs.

    One pattern, not an allowlist of exact spellings. The allowlist missed four
    of eleven helpers — `samcloud-say` (SAY_VERSION), `samcloud-subscribe.py`
    (SUBSCRIBE_VERSION), `samcloud-scope-check.py` (SCOPE_CHECK_VERSION) and
    `wake-install.sh` (GT_VERSION) — because it spelled the subscriber's
    constant `SUB_VERSION`. The write pen, the interactive adapter and the
    installer were never checked by the gate that says it covers what the fleet
    runs, and a rename would have dropped another silently. Match any
    `*_VERSION` stamp in either the shell/Python comment form or the assignment
    form, and let `helper_stamps_are_discoverable` below fail if a served helper
    carries none.
    """
    pat = r'^#?\s*([A-Z][A-Z0-9_]*_VERSION)\s*[:=]\s*"?([0-9][^"\s]*)"?'
    out = {}
    if not HELPERS.is_dir():
        return out
    for p in sorted(HELPERS.iterdir()):
        if not p.is_file():
            continue
        m = re.search(pat, read(p), re.M)
        if m:
            out[p.name] = m.group(2)
    return out


# The doc that owns the lifecycle. A state list is derivable from source in
# exactly the way helper names are, and until 0.12.22 nothing checked it: a doc
# could say a binding is `open | closed`, omit `closing`, omit the `adapter`
# holder entirely, or keep describing a flag deleted two releases ago, and this
# gate would pass it. Two docs that promise "changes in the same commit as the
# code" were two releases behind when this was written.
LIFECYCLE_DOC = DOCS / "Thread Lifecycle.md"

# name -> (file, regex capturing the tuple body)
STATE_SOURCES = {
    "binding state": (MAIN_PY, r"^_THREAD_STATES\s*=\s*\(([^)]*)\)"),
    "holder kind": (MAIN_PY, r"^_RAIL_HOLDERS\s*=\s*\(([^)]*)\)"),
    "surface kind": (MAIN_PY, r"^_SURFACE_KINDS\s*=\s*\(([^)]*)\)"),
    "dispatch verb": (ROOT / "registry/helpers/samcloud-rail.py",
                      r"^DISPATCH_VERBS\s*=\s*\(([^)]*)\)"),
}


def state_lists():
    """The vocabularies the lifecycle has, read out of the code that enforces
    them. Same technique as `served_helpers`: derive, never restate."""
    out = {}
    for name, (path, pat) in STATE_SOURCES.items():
        m = re.search(pat, read(path), re.M)
        if m:
            out[name] = [v.strip().strip("\"'") for v in m.group(1).split(",")
                         if v.strip().strip("\"'")]
    return out


def route_groups():
    """Top-level API groups actually served, from the FastAPI decorators."""
    body = read(MAIN_PY)
    groups = set()
    for m in re.finditer(r'@app\.(?:get|post|patch|delete|put)\("/api/v1/([^/"{]+)',
                         body):
        groups.add(m.group(1))
    return groups


def main():
    files = corpus()
    blob = {p: read(p) for p in files}
    joined = "\n".join(blob.values())
    failures, warnings = [], []

    # 1. Required concepts must appear somewhere in current guidance.
    for pat, name, why in REQUIRED_CONCEPTS:
        if not re.search(pat, joined, re.I):
            failures.append(f"CONCEPT UNDOCUMENTED: {name} — {why}")

    # 2. Retired terms must not be asserted as CURRENT guidance. Naming a
    # retired thing to say it is gone ("issues.yaml was deleted", "don't run
    # console-deploy.sh") is correct and useful — flagging those would make
    # this test cry wolf, which is how a checker gets ignored. So a mention
    # only counts as drift when its own line carries no retirement marker.
    for pat, name, why in RETIRED_TERMS:
        hits = []
        for p, t in blob.items():
            for line in t.splitlines():
                if re.search(pat, line, re.I) and not RETIRED_MARKER.search(line):
                    hits.append(p.relative_to(ROOT).as_posix())
                    break
        if hits:
            failures.append(
                f"RETIRED CLAIM: '{name}' asserted as current in "
                f"{', '.join(hits)} — {why}")

    # 3. Every versioned helper the fleet runs should be named somewhere.
    for fname, ver in served_helpers().items():
        stem = re.escape(fname.replace(".py", "").replace(".sh", ""))
        if not re.search(stem, joined, re.I):
            failures.append(
                f"HELPER UNDOCUMENTED: {fname} (v{ver}) is served to the fleet "
                f"but named in no doc or skill")

    # 3b. Every value in every lifecycle vocabulary must be named in the doc
    # that owns the lifecycle — derived from the code, so a state that is added
    # or renamed fails here until the doc says so.
    lifecycle = read(LIFECYCLE_DOC)
    if not lifecycle:
        failures.append(
            f"LIFECYCLE DOC MISSING: {LIFECYCLE_DOC.relative_to(ROOT).as_posix()} — "
            f"the state machine has no single owner, so four vocabularies "
            f"(binding, holder, surface, verb) are each described in several "
            f"places and none of them can be checked")
    else:
        # In a code span, not merely somewhere in the prose. A bare-word match
        # passes on any incidental use — "one adapter turn" satisfied a check
        # for the `adapter` holder kind — so a rename would slip through the
        # gate that exists to catch exactly that.
        for name, values in state_lists().items():
            missing = [v for v in values
                       if f"`{v}`" not in lifecycle]
            if missing:
                failures.append(
                    f"LIFECYCLE INCOMPLETE: {name} {missing} in the code but not in "
                    f"{LIFECYCLE_DOC.name} — the doc that owns the lifecycle has to "
                    f"name every value the code accepts")

    # 3c. The message/status split is stated twice — in the plane and in the
    # served rail — with a comment in each saying it mirrors the other. They
    # agree today and nothing made them. The rail is fetched over HTTP and
    # cannot import the plane, so "derive" here means "fail when they drift".
    _split = {}
    for label, path, pats in (
        ("plane", MAIN_PY, (r"^_OPEN_EVENT_TYPES\s*=\s*\(([^)]*)\)",
                            r"^_RESPONSE_EVENT_TYPES\s*=\s*\(([^)]*)\)")),
        ("rail", ROOT / "registry/helpers/samcloud-rail.py",
                 (r"^OPEN_EVENTS\s*=\s*\{([^}]*)\}",
                  r"^RESPONSE_EVENTS\s*=\s*\{([^}]*)\}")),
    ):
        body = read(path)
        got = []
        for pat in pats:
            m = re.search(pat, body, re.M)
            got.append(frozenset(v.strip().strip("\"'") for v in m.group(1).split(",")
                                 if v.strip().strip("\"'")) if m else None)
        _split[label] = got
    for i, which in enumerate(("OPEN", "RESPONSE")):
        a, b = _split["plane"][i], _split["rail"][i]
        if a is None or b is None:
            failures.append(f"EVENT SPLIT UNREADABLE: {which} not found in "
                            f"{'the plane' if a is None else 'the rail'} — the "
                            f"drift check cannot run")
        elif a != b:
            failures.append(
                f"EVENT SPLIT DRIFT: {which} differs — plane has {sorted(a - b)} "
                f"that the rail does not, rail has {sorted(b - a)} that the plane "
                f"does not. One row classified two ways is a row delivered twice "
                f"or not at all")

    # 3d. The model is stated ONCE — Thread Lifecycle.md, between <!-- model -->
    # markers — and must appear verbatim where a reader first meets the word
    # "thread": the rail doc, the served skill, CLAUDE.md. On 2026-10-06 the
    # conversation-scoped thread shipped (0.12.65) and three of the four still
    # opened with "a ticket and whatever belongs with it"; a session re-learned
    # the old shape from them after every compaction (#886). Derived, not
    # restated: the lifecycle doc owns the paragraph, the others carry it.
    _model = re.search(r"<!-- model -->(.*?)<!-- /model -->", lifecycle or "", re.S)
    if not _model:
        failures.append(
            f"MODEL PARAGRAPH MISSING: {LIFECYCLE_DOC.name} has no <!-- model --> "
            f"block — the thread's definition has no owner")
    else:
        canon = " ".join(_model.group(1).split())
        for p in (DOCS / "Agent Rail.md",
                  ROOT / "registry/skills/samcloud-agent/SKILL.md",
                  CLAUDE_MD):
            if canon not in " ".join(read(p).split()):
                failures.append(
                    f"MODEL PARAGRAPH DRIFT: {p.relative_to(ROOT).as_posix()} does "
                    f"not carry the model paragraph from {LIFECYCLE_DOC.name} "
                    f"verbatim — a reader starting there learns a different thread")

    # 4. API route groups should be discoverable from the samcloud skill.
    skill = read(SKILLS / "samcloud" / "SKILL.md")
    if skill:
        missing = sorted(g for g in route_groups()
                         if g not in skill and g.rstrip("s") not in skill)
        if missing:
            warnings.append(
                "ROUTE GROUPS not in samcloud skill: " + ", ".join(missing))

    # 5. Top-level subsystems (a dir with its own code) should be mentioned.
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir() or d.name.startswith((".", "_")):
            continue
        if d.name in {"docs", "node_modules", "sandbox", "scripts", "plists",
                      "deploy", "config", "devices", "maintenance", "console"}:
            continue
        if not any(d.glob("*.py")) and not any(d.glob("*.ts")):
            continue
        if not re.search(re.escape(d.name), joined, re.I):
            failures.append(
                f"SUBSYSTEM UNDOCUMENTED: {d.name}/ has code but is named in "
                f"no doc or skill")

    check_skills(failures, warnings)

    print(f"docs_coverage: {len(files)} current-guidance files scanned")
    print(f"  docs/: {len([p for p in files if 'docs' in p.parts])} · "
          f"skills: {len([p for p in files if 'skills' in p.parts])} · CLAUDE.md")
    print()
    for w in warnings:
        print(f"  WARN  {w}")
    for f in failures:
        print(f"  FAIL  {f}")
    print()
    if failures:
        print(f"FAILED: {len(failures)} drift(s) between the record and the "
              f"running system ({len(warnings)} warning(s))")
        return 1
    print(f"PASSED: the written record matches the code (a local check of docs against source; it reads nothing running) "
          f"({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
