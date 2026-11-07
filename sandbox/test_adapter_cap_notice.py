#!/usr/bin/env python3
"""A turn that ends on a CAP must say so on the thread (#895).

Drives the REAL registry/helpers/samcloud-adapter.py `main()` with a real
subprocess runtime and a real flock, against a fake plane. No registry, no
network.

Why this exists (2026-10-07): a native-client seat's first real job was assigned, the
seat woke in two seconds, drove its model for 15 inference calls and ran code —
and then produced permanent silence. The adapter killed it at TURN_TIMEOUT and
journalled `outcome: uncertain, comment: null`. Every other signal read positive:
the task stayed `todo`, `/wake/registrations` said `last_session_rc: ok`,
`consecutive_session_failures: 0`. The only evidence was a file inside the
container, which the requester could not read — the device is in a different group
from the requester, by design.

Three paths can end a turn with no answer and all three were silent:
  1. adapter  TURN_TIMEOUT   — killed a running turn
  2. adapter  LOCK_WAIT      — starved; the delivery was NEVER ATTEMPTED, so
                               nothing anywhere records that it existed
  3. listener WAKE_TIMEOUT   — not this file's to fix; it kills the whole wake
                               and writes no adapter journal at all

This covers 1 and 2, and the healthy case, because a notice that also fires on a
good turn would be worse than no notice.

Point it at an older build to watch that build fail:
    ADAPTER_UNDER_TEST=/tmp/old-adapter.py python3 sandbox/test_adapter_cap_notice.py
(1.2.2 posts nothing on either cap, so every notice check fails.)

Run from repo root: python3 sandbox/test_adapter_cap_notice.py
"""
import ast
import fcntl
import importlib.util
import io
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ADAPTER = Path(os.environ.get("ADAPTER_UNDER_TEST")
               or REPO / "registry/helpers/samcloud-adapter.py")

PASS, FAIL = 0, 0


def check(desc, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"      \033[0;32m✓\033[0m {desc}")
    else:
        FAIL += 1
        print(f"      \033[0;31m✗\033[0m {desc}  {detail}")


def load(home: Path, **conf):
    """Import the adapter with a wake.env of our making and $HOME redirected."""
    (home / ".config/samcloud").mkdir(parents=True, exist_ok=True)
    (home / ".samcloud").mkdir(parents=True, exist_ok=True)
    (home / ".samcloud/token").write_text("sc_agent_test")
    body = "\n".join(f"{k}={v}" for k, v in conf.items())
    (home / ".config/samcloud/wake.env").write_text(body + "\n")
    os.environ["HOME"] = str(home)
    os.environ["SAMCLOUD_WAKE_CONF"] = str(home / ".config/samcloud/wake.env")
    for k in list(os.environ):
        if k.startswith("RAIL_"):
            del os.environ[k]
    spec = importlib.util.spec_from_file_location(f"adapter_{home.name}", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def instrument(mod):
    """Replace the plane with a list. Every write the adapter makes lands here."""
    calls = []

    def fake_api(method, path, body=None, timeout=30, session_id=""):
        calls.append({"method": method, "path": path, "body": body})
        if path.endswith("/comments"):
            return {"comment_id": 9000 + len([c for c in calls
                                              if c["path"].endswith("/comments")])}
        return {}

    mod._api = fake_api
    return calls


def instrument_409(mod, fail_first=0, code=409):
    """A plane that refuses comment posts — 409 is the replay guard's code.

    `fail_first=N` refuses the first N comment posts and accepts the rest; 0
    refuses every one. `code` is the status, so the same harness proves the 409
    branch fires AND that other statuses do not reach it. The error is a real
    `urllib.error.HTTPError` rather than a stand-in, because the adapter branches
    on `e.code == 409` and a stub that merely looked like one would prove nothing.
    """
    calls = []
    seen = {"n": 0}

    def fake_api(method, path, body=None, timeout=30, session_id=""):
        calls.append({"method": method, "path": path, "body": body})
        if path.endswith("/comments"):
            seen["n"] += 1
            if fail_first == 0 or seen["n"] <= fail_first:
                raise urllib.error.HTTPError(
                    f"http://plane{path}", code, "Refused", {},
                    io.BytesIO(b'{"detail":"replay-guard: this exact body was '
                               b'already posted on this thread"}'))
            return {"comment_id": 9000 + seen["n"]}
        return {}

    mod._api = fake_api
    return calls


def notice_of(mod, **kw):
    """`cap_notice` with ids, on a build that may not accept them — or have it.

    1.3.0 raises TypeError (no `ids` parameter); 1.2.2 raises AttributeError (no
    `cap_notice` at all, which is the defect THAT release had). Both are the
    thing under test, so both are reported as the check failing rather than as
    the harness crashing — this file is documented as pointable at older builds,
    and a traceback would hide every later section's verdict.

    It returns the same string both times, so "two deliveries differ" fails on an
    old build for the right reason and not because two sentinels happened to
    differ.
    """
    try:
        return mod.cap_notice(**kw)
    except (TypeError, AttributeError):
        return "<this build's cap_notice cannot name a delivery>"


def run(mod, key, tmp, reply_to="task 763", ids="[1201]"):
    sys.argv = ["samcloud-adapter", "--key", key, "--sid", "conv-test",
                "--prompt-file", str(tmp / "turn.txt"), "--reply-to", reply_to,
                "--ids", ids]
    (tmp / "turn.txt").write_text("do the thing")
    return mod.main()


def starved(tmp, name, instrument_fn, ids="[1201]"):
    """One delivery that starves behind a REAL held turn lock.

    The lock is taken with `flock` rather than simulated: starvation is the only
    cap path where no runtime runs, and a mocked lock would test the mock.
    """
    h = tmp / name
    h.mkdir()
    state = h / "state"
    mod = load(h, API="http://127.0.0.1:1/api/v1",
               RAIL_STATE_DIR=str(state),
               TOKEN_PATH=str(h / ".samcloud/token"),
               RAIL_RUNTIME="hermes",
               RAIL_RUNTIME_CMD="echo never runs",
               RAIL_RUNTIME_TIMEOUT="30",
               RAIL_RUNTIME_WAIT="1")
    calls = instrument_fn(mod)
    key = f"thread-{name}"
    lock_path = state / "runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        run(mod, key, tmp, ids=ids)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    return mod, calls, state, key


def comments(calls):
    """Comment posts, each flattened to {"path", "text"}.

    `_api`'s third argument is the REQUEST BODY, so the comment text is at
    body["body"] — reading c["body"] gives the dict and a slice of it raises
    KeyError, which is how this helper earned a docstring.
    """
    return [{"path": c["path"], "text": (c["body"] or {}).get("body", "")}
            for c in calls if c["path"].endswith("/comments")]


def acks(calls):
    return [c for c in calls if c["path"].endswith("/notifications/ack")]


def journal_rows(state: Path, key):
    f = state / "threads" / key / "adapter.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def conv_probe(tmp: Path) -> str:
    """A CONV_CMD whose answer CHANGES after the first call.

    The adapter asks before the turn and again after the kill, and records the
    handle only when it differs (#838's guard: an unchanged handle predates this
    turn and belongs to someone else). A probe that answered the same thing twice
    would exercise the guard and not the recording.
    """
    script = tmp / "conv.sh"
    counter = tmp / "conv.count"
    script.write_text(
        "#!/bin/sh\n"
        f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
        f"echo $((n+1)) > {counter}\n"
        "if [ \"$n\" = \"0\" ]; then echo 20260101_000000_aaaaaa; "
        "else echo 20261007_112901_1e1380; fi\n")
    script.chmod(0o755)
    return f"sh {script}"


def main():
    print("\n  samcloud-adapter: a cap must not be silent (#895)")
    print(f"  under test: {ADAPTER}")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ── [1] TURN_TIMEOUT: the headline case ──────────────────────────────
        print("\n  [1] TURN_TIMEOUT kills a running turn")
        h1 = tmp / "h1"
        h1.mkdir()
        state1 = h1 / "state"
        mod = load(h1, API="http://127.0.0.1:1/api/v1",
                   RAIL_STATE_DIR=str(state1),
                   TOKEN_PATH=str(h1 / ".samcloud/token"),
                   RAIL_RUNTIME="hermes",
                   RAIL_RUNTIME_CMD="sleep 30",
                   RAIL_RUNTIME_TIMEOUT="1",
                   RAIL_RUNTIME_WAIT="2",
                   RAIL_RUNTIME_CONV_CMD=conv_probe(tmp),
                   RAIL_RUNTIME_CONV_RE=r"([0-9]{8}_[0-9]{6}_[0-9a-f]{6})")
        calls = instrument(mod)
        rc = run(mod, "thread-1778", tmp)
        cs = comments(calls)
        check("exits non-zero", rc != 0, rc)
        check("posts EXACTLY ONE comment", len(cs) == 1, len(cs))
        body = cs[0]["text"] if cs else ""
        check("addressed to the requester's object",
              cs and cs[0]["path"] == "/tasks/763/comments",
              cs[0]["path"] if cs else "none")
        check("names the cap that fired", "RAIL_RUNTIME_TIMEOUT" in body, body[:160])
        check("names the cap's value", "1s" in body, body[:160])
        check("names the runtime", "hermes" in body, body[:160])
        check("carries the conversation handle",
              "20261007_112901_1e1380" in body, body[:400])
        check("says the working is NOT lost",
              "not lost" in body.lower(), body[:400])
        # Both caps need the delivery named, not just the starvation one: two
        # timeouts on one thread with no conversation handle and the same rounded
        # elapsed would collide in the replay guard exactly the same way.
        check("names the delivery it is about", "1201" in body, body[:400])
        # `bool(body) and ...`: without the conjunct this reads PASS on a build that
        # posts nothing at all, which is the build with the bug (measured on 1.2.2).
        check("does NOT claim the seat produced nothing/hung",
              bool(body) and "hung" not in body.lower(), repr(body[:400]))
        check("does NOT ack the unanswered delivery", len(acks(calls)) == 0,
              acks(calls))
        rows = journal_rows(state1, "thread-1778")
        check("journals exactly one row", len(rows) == 1, len(rows))
        r = rows[0] if rows else {}
        check("journal names the cap", r.get("cap") == "RAIL_RUNTIME_TIMEOUT", r)
        check("journal records the notice comment id", bool(r.get("notice")), r)
        check("journal says the turn was attempted", r.get("attempted") is True, r)
        check("journal carries the conversation handle",
              r.get("conv") == "20261007_112901_1e1380", r)
        conv_file = state1 / "threads/thread-1778/conv"
        check("conversation handle RECORDED so the next turn resumes it",
              conv_file.exists()
              and conv_file.read_text().strip() == "20261007_112901_1e1380",
              conv_file.read_text() if conv_file.exists() else "no conv file")

        # ── [2] the healthy case, read once on purpose ───────────────────────
        print("\n  [2] a healthy turn answers and posts NO notice")
        h2 = tmp / "h2"
        h2.mkdir()
        state2 = h2 / "state"
        mod2 = load(h2, API="http://127.0.0.1:1/api/v1",
                    RAIL_STATE_DIR=str(state2),
                    TOKEN_PATH=str(h2 / ".samcloud/token"),
                    RAIL_RUNTIME="hermes",
                    RAIL_RUNTIME_CMD="echo the answer",
                    RAIL_RUNTIME_TIMEOUT="30",
                    RAIL_RUNTIME_WAIT="2")
        calls2 = instrument(mod2)
        rc2 = run(mod2, "thread-ok", tmp)
        cs2 = comments(calls2)
        check("exits zero", rc2 == 0, rc2)
        check("posts EXACTLY ONE comment", len(cs2) == 1, len(cs2))
        check("and it is the answer, not a notice",
              cs2 and cs2[0]["text"].strip() == "the answer"
              and "cap" not in cs2[0]["text"].lower(),
              cs2[0]["text"][:160] if cs2 else "none")
        check("acks the answered delivery", len(acks(calls2)) == 1, acks(calls2))
        rows2 = journal_rows(state2, "thread-ok")
        check("journal carries no cap on a healthy turn",
              rows2 and "cap" not in rows2[0], rows2)

        # ── [3] LOCK_WAIT starvation: never attempted ────────────────────────
        print("\n  [3] LOCK_WAIT starves a delivery that never ran")
        h3 = tmp / "h3"
        h3.mkdir()
        state3 = h3 / "state"
        mod3 = load(h3, API="http://127.0.0.1:1/api/v1",
                    RAIL_STATE_DIR=str(state3),
                    TOKEN_PATH=str(h3 / ".samcloud/token"),
                    RAIL_RUNTIME="hermes",
                    RAIL_RUNTIME_CMD="echo never runs",
                    RAIL_RUNTIME_TIMEOUT="30",
                    RAIL_RUNTIME_WAIT="1")
        calls3 = instrument(mod3)
        # Hold the seat's turn lock, for real, so the adapter starves on it.
        lock_path = state3 / "runtime.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = open(lock_path, "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            rc3 = run(mod3, "thread-starved", tmp)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        cs3 = comments(calls3)
        check("exits non-zero", rc3 != 0, rc3)
        check("posts EXACTLY ONE comment", len(cs3) == 1, len(cs3))
        body3 = cs3[0]["text"] if cs3 else ""
        check("names the wait cap", "RAIL_RUNTIME_WAIT" in body3, body3[:200])
        check("says it was NEVER ATTEMPTED",
              "never attempted" in body3.lower(), body3[:300])
        check("does not offer a conversation to resume",
              bool(body3) and "20261007" not in body3
              and "recoverable" not in body3.lower(), repr(body3[:300]))
        check("reports the real wait, not 0s",
              bool(body3) and "0s" not in body3, repr(body3[:200]))
        check("does NOT ack", len(acks(calls3)) == 0, acks(calls3))
        rows3 = journal_rows(state3, "thread-starved")
        r3 = rows3[0] if rows3 else {}
        check("journal says it was NOT attempted", r3.get("attempted") is False, r3)
        check("journal names the wait cap", r3.get("cap") == "RAIL_RUNTIME_WAIT", r3)

        # ── [4] the invariant a comment in the source claims ─────────────────
        print("\n  [4] `uncertain` comes only from the cap (the source says so)")
        tree = ast.parse(ADAPTER.read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "run_turn")
        uncertain = [n for n in ast.walk(fn) if isinstance(n, ast.Return)
                     and isinstance(n.value, ast.Tuple)
                     and isinstance(n.value.elts[0], ast.Constant)
                     and n.value.elts[0].value == "uncertain"]
        check("run_turn returns 'uncertain' from exactly one place",
              len(uncertain) == 1, len(uncertain))
        handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try)
                    for h in n.handlers
                    if any(u in ast.walk(h) for u in uncertain)]
        check("and that place is the TimeoutExpired handler",
              len(handlers) == 1
              and "TimeoutExpired" in ast.unparse(handlers[0].type or ast.Name("?")),
              [ast.unparse(h.type) for h in handlers])

        # ── [5] two starved deliveries must be two DISTINGUISHABLE reports ───
        # The 1.3.0 defect, measured on a live seat: nine deliveries starved at
        # 5700s, and FIVE notices were refused HTTP 409 because the bodies were
        # byte-identical — `elapsed` rounding decided which four the threads heard
        # about. The plane's guard is right; the notice was not distinguishable.
        print("\n  [5] two starved deliveries render two different notices")
        kw = dict(cap_name="RAIL_RUNTIME_WAIT", cap_value=5700, took=5700.4,
                  conv="", attempted=False)
        b1 = notice_of(mod, ids=[1201], **kw)
        b2 = notice_of(mod, ids=[1202], **kw)
        check("same cap, same elapsed, DIFFERENT ids -> different bodies",
              b1 != b2, repr(b1[:80]))
        check("each names its own notification", "1201" in b1 and "1202" in b2,
              (b1[:200], b2[:200]))
        # `"RAIL_RUNTIME_WAIT" in b1 and ...` is not decoration: without it this
        # reads PASS on a build whose cap_notice took no ids at all, because the
        # sentinel contains neither number. A check that passes on absence proves
        # nothing, and this suite has shipped two of those before.
        check("and not the other's",
              "RAIL_RUNTIME_WAIT" in b1 and "RAIL_RUNTIME_WAIT" in b2
              and "1202" not in b1 and "1201" not in b2,
              (b1[:200], b2[:200]))
        _, c5a, s5a, k5a = starved(tmp, "h5a", instrument, ids="[1201]")
        _, c5b, s5b, k5b = starved(tmp, "h5b", instrument, ids="[1202]")
        t5a = comments(c5a)[0]["text"] if comments(c5a) else ""
        t5b = comments(c5b)[0]["text"] if comments(c5b) else ""
        check("end to end: two real starved deliveries differ",
              bool(t5a) and t5a != t5b, repr(t5a[:120]))
        check("end to end: both still name the wait cap",
              "RAIL_RUNTIME_WAIT" in t5a and "RAIL_RUNTIME_WAIT" in t5b,
              (t5a[:80], t5b[:80]))
        # >=200 chars is the replay guard's own floor: below it identical bodies
        # are allowed through, so a shorter notice would never have had this bug
        # and the fix would be unnecessary. Measured on a REAL posted notice, not
        # on cap_notice's return, so it is a fact about the defect either way.
        check("the notice is long enough to be guarded at all (>=200 chars)",
              len(t5a) >= 200, len(t5a))

        # ── [6] a 409 WITH ids means the thread already has this notice ───────
        print("\n  [6] a 409 on an identified delivery is 'already told'")
        _, c6, s6, k6 = starved(tmp, "h6", instrument_409, ids="[1301]")
        check("does NOT post a second copy", len(comments(c6)) == 1,
              len(comments(c6)))
        r6 = journal_rows(s6, k6)[0] if journal_rows(s6, k6) else {}
        check("journal records it as a duplicate",
              r6.get("notice_duplicate") is True, r6)
        check("journal does NOT claim a comment id", r6.get("notice") is None, r6)
        check("journal still names the cap", r6.get("cap") == "RAIL_RUNTIME_WAIT",
              r6)
        check("and does not pretend it was tie-broken",
              "notice_tiebroken" not in r6, r6)

        # ── [7] a 409 with NO ids must still land ────────────────────────────
        # The residual case the ids cannot separate. Without the retry the thread
        # is simply never told, which is #895's own bug reintroduced by its fix.
        print("\n  [7] a 409 on an UNidentified delivery is tie-broken and lands")
        _, c7, s7, k7 = starved(tmp, "h7", lambda m: instrument_409(m, fail_first=1),
                                ids="[]")
        cs7 = comments(c7)
        check("retries exactly once", len(cs7) == 2, len(cs7))
        check("the retry carries a discriminator the first body lacked",
              len(cs7) == 2 and cs7[1]["text"].startswith(cs7[0]["text"])
              and len(cs7[1]["text"]) > len(cs7[0]["text"]),
              [c["text"][-80:] for c in cs7])
        check("and the discriminator is the pid, not a nonce",
              len(cs7) == 2 and "pid" in cs7[1]["text"].lower(),
              cs7[1]["text"][-200:] if len(cs7) == 2 else "none")
        r7 = journal_rows(s7, k7)[0] if journal_rows(s7, k7) else {}
        check("journal says it was tie-broken",
              r7.get("notice_tiebroken") is True, r7)
        check("journal records the comment id the retry got",
              bool(r7.get("notice")), r7)
        check("and does not also call it a duplicate",
              "notice_duplicate" not in r7, r7)

        # ── [8] both refused: claim nothing ─────────────────────────────────
        print("\n  [8] when both attempts are refused, nothing is claimed")
        _, c8, s8, k8 = starved(tmp, "h8", instrument_409, ids="[]")
        check("tried twice and stopped", len(comments(c8)) == 2,
              len(comments(c8)))
        r8 = journal_rows(s8, k8)[0] if journal_rows(s8, k8) else {}
        check("journal claims no comment id", r8.get("notice") is None, r8)
        check("journal does not claim a tie-break that did not land",
              "notice_tiebroken" not in r8, r8)
        check("journal does not claim it was already told",
              "notice_duplicate" not in r8, r8)

        # ── [9] only 409 is retried ─────────────────────────────────────────
        # A 500 is the plane being broken, not the guard refusing a replay. Re-
        # posting into it would double the traffic and tell nobody anything; this
        # check is what stops the fallback from becoming a general retry loop.
        print("\n  [9] a non-409 refusal is NOT retried")
        _, c9, s9, k9 = starved(tmp, "h9", lambda m: instrument_409(m, code=500),
                                ids="[]")
        check("posts once and gives up", len(comments(c9)) == 1,
              len(comments(c9)))
        r9 = journal_rows(s9, k9)[0] if journal_rows(s9, k9) else {}
        check("journal claims nothing about the notice",
              r9.get("notice") is None and "notice_tiebroken" not in r9
              and "notice_duplicate" not in r9, r9)

    verdict = "ALL PASSED" if FAIL == 0 else "FAILURES DETECTED"
    print(f"\n  {verdict}: adapter cap notice {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
