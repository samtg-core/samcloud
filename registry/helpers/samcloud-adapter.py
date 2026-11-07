#!/usr/bin/env python3
# ADAPTER_VERSION: 1.5.0
"""samcloud-adapter — the generalised holder: one delivery, one turn, one reply.

Served as GET /api/v1/helpers/adapter. Installed by wake-install on a seat
whose runtime is not a Claude harness.

The rail (samcloud-rail.py) decides WHAT to deliver and to WHOM — thread
grouping, the delivery rule, bindings, receipts, sweeps. None of that is about
Claude. What *is* about Claude is the holder: `claude -p --input-format
stream-json` on a FIFO, a conversation id the rail chooses, `--resume` against
a transcript in ~/.claude/projects. A seat running Hermes, codex or anything
else has none of those, and before this file the rail's answer was to refuse:
`scripts/new-seat.sh --no-rail` gave such a seat an account and an identity and
then told it to poll by hand.

This is the other holder. The rail invokes it per delivery; it runs the seat's
runtime once, takes what the runtime said, and posts it on the thread under the
seat's identity, stamped with the conversation the rail bound. To the plane the
result is indistinguishable from a Claude holder answering: same identity, same
thread, same conversation id, same receipt.

It implements the adapter contract of doc 265 (codex, 2026-09-10) — the
capability vocabulary, the delivery outcomes, the journal, and the rule that
the adapter reports success only after the receiving boundary accepted the
message.

    samcloud-adapter --key ticket-748 --sid <conversation-id> \
        --prompt-file /path/to/turn.txt --reply-to "ticket 748" \
        --ids '[1201,1202]' [--binding-thread 1691]

Configuration rides ~/.config/samcloud/wake.env beside the rail's:

    RAIL_RUNTIME          a name, for the log and the binding    (e.g. hermes)
    RAIL_RUNTIME_CMD      argv template. {prompt} / {prompt_file} mark where
                          the turn's text goes; with neither, it goes on stdin.
    RAIL_RUNTIME_RESUME   argv fragment appended when this thread already has a
                          runtime conversation. {conv} is its handle.
    RAIL_RUNTIME_OUTPUT   text (default) or hermes-stream-json. The latter takes
                          the handle and final answer from this turn's events,
                          ignores CONV_CMD, and requires a successful result.
    RAIL_RUNTIME_CONV_CMD optional: a command that prints the handle of the
                          conversation the last turn used or created.
    RAIL_RUNTIME_CONV_RE  regex, group 1 = the handle. Applied to CONV_CMD's
                          output, or to the turn's own output when there is no
                          CONV_CMD.
    RAIL_RUNTIME_FAIL_RE  regex marking the runtime's own failure text. A
                          harness that reports "the provider is unavailable" on
                          stdout and exits 0 is telling you it failed in the
                          only channel it has; without this the adapter posts
                          that sentence on the thread as though the model had
                          said it.
    RAIL_RUNTIME_TIMEOUT  seconds a single turn may take            (def 1800)
    RAIL_RUNTIME_WAIT     seconds to wait for the seat's turn lock   (def TIMEOUT+300)
                          Either cap firing posts a notice on the thread naming
                          it, the elapsed time and the runtime's conversation
                          handle (#895). Note the default is not the only cap in
                          play: the LISTENER kills the whole wake at
                          MAX_SESSION_MIN * 60 + 120 (def 1920), so raising
                          TIMEOUT past that moves the kill to a path this file
                          never sees and cannot report. Keep
                          TIMEOUT < WAIT < listener, or the notice is the thing
                          you lose.
    RAIL_RUNTIME_MAX_CHARS  cap on the posted reply                 (def 12000)
    RAIL_RUNTIME_LEASE    resource id to hold exclusively for the turn. Empty = no
                          lease, which is what every seat runs today.
                          DO NOT name the pool your runtime reaches through a
                          gateway. That resource is exclusive, one active exclusive
                          lease per resource is enforced regardless of identity, and
                          the gateway takes its own per request — so a turn-long
                          lease held by this seat refuses the runtime's own
                          generations 409/503 for the whole turn. The unit that needs
                          the mutex is the request, and the gateway already has it.
                          Set this only for a resource the runtime occupies end to
                          end with nothing else leasing it (#827).

Two things this deliberately does NOT do:

  - Pretend the runtime can be interrupted. One turn at a time per seat, taken
    under a local exclusive lock. A local model usually sits behind a pool that
    serves one request at a time; two turns at once is the twenty-minute
    collision of 2026-09-20, and exo has no cancellation path, so the loser
    does not fail — it wedges the pool for everyone.
  - Post anything the runtime did not say AS THOUGH THE RUNTIME SAID IT. A turn
    that produced no text is a failed delivery, not an empty comment on the
    thread.

    Refined by #895, because the second half of that sentence used to read "a
    failed delivery that stays visible" and it was not true. A turn killed at a
    cap posted nothing, left the task `todo` with no comment, and reported
    `last_session_rc: ok` — indistinguishable from a healthy long turn, with the
    only evidence in a file inside the seat. On a container seat the requester
    generally cannot read that file: the device is in a different group, by
    design. So a cap now posts ONE comment, in the adapter's own voice and clearly
    labelled as such, saying which cap fired and where the runtime's working can
    be found. That is not the runtime speaking and is not an answer; it is the
    holder declining to be silent about its own kill.

Stdlib only. The bearer is read in-process and never enters argv (#582).
"""
import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.path.expanduser("~"))


def _read_env_file(path: Path) -> dict:
    """KEY=value lines, quotes stripped — the same wake.env the rail reads."""
    conf = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            conf[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return conf


CONF = _read_env_file(HOME / ".config/samcloud/wake.env")


def _conf(key: str, default: str) -> str:
    return os.environ.get(key) or CONF.get(key) or default


# The plane that served this copy, rendered in (#1010); wake.env's API wins.
API = _conf("API", "__API__").rstrip("/")
STATE_DIR = Path(os.path.expanduser(_conf("RAIL_STATE_DIR", "~/.local/samcloud/rail")))
TOKEN_PATH = Path(os.path.expanduser(_conf("TOKEN_PATH", "~/.samcloud/token")))
RUNTIME = _conf("RAIL_RUNTIME", "")
RUNTIME_CMD = _conf("RAIL_RUNTIME_CMD", "")
RUNTIME_RESUME = _conf("RAIL_RUNTIME_RESUME", "")
CONV_CMD = _conf("RAIL_RUNTIME_CONV_CMD", "")
CONV_RE = _conf("RAIL_RUNTIME_CONV_RE", "")
OUTPUT_MODE = _conf("RAIL_RUNTIME_OUTPUT", "text")
# An exit code is the honest channel for a failure and not every runtime uses
# it. Hermes prints "Custom endpoint reported it was overloaded … Provider
# said: HTTP 503" to stdout and exits 0; on 2026-09-21 the adapter posted that
# on ticket #803 as the seat's answer, outcome `accepted`, while the exo pool
# was down. Doc 265 §4 asks for the outcome to be honest; this is how a
# runtime that reports in-band gets to be.
FAIL_RE = _conf("RAIL_RUNTIME_FAIL_RE", "")
TURN_TIMEOUT = int(_conf("RAIL_RUNTIME_TIMEOUT", "1800"))
# A delivery that arrives mid-turn waits for the turn in front of it. The wait
# used to be a fixed 1800s while a turn could run RAIL_RUNTIME_TIMEOUT (5400s on
# one local-model seat), so the waiter always gave up first and its message was dropped
# with "not answered" — nothing re-delivers it (#832). Default: one full turn
# plus a margin, so a queued message is answered in order.
LOCK_WAIT = int(_conf("RAIL_RUNTIME_WAIT", str(TURN_TIMEOUT + 300)))
MAX_CHARS = int(_conf("RAIL_RUNTIME_MAX_CHARS", "12000"))
# A merged turn must still fit the runtime's context. Measured on a local-model seat: real
# deliveries are 2.9k-7.5k tokens against a 12,288 gate, so ONE merge fits with ~590
# to spare and two do not — an unbounded merge of a nine-deep queue would build a
# prompt the gateway refuses 413, which loses the content in effect while the journal
# correctly says it was carried. So: absorb while it fits, and let the delivery that
# would cross the bound be the next waiter instead, exactly as it would have been
# without merging. Nothing is dropped and nothing is truncated.
# UNSET (0) MEANS UNBOUNDED: a seat that does not set this behaves as 1.2.0 did.
MAX_PROMPT_CHARS = int(_conf("RAIL_RUNTIME_MAX_PROMPT_CHARS", "0"))
LEASE_RESOURCE = _conf("RAIL_RUNTIME_LEASE", "")
LOG_PATH = Path(os.path.expanduser(_conf("LOG_FILE", "~/logs/samcloud-wake.log")))


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] adapter: {msg}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr)


# ── the plane ────────────────────────────────────────────────────────────────

def _token() -> str:
    return TOKEN_PATH.read_text().strip()


def _api(method: str, path: str, body=None, timeout: int = 30,
         session_id: str = "") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    if data:
        req.add_header("Content-Type", "application/json")
    if session_id:
        # Every write carries the conversation that made it (doc 265 §2): this
        # is the whole reason the reply comes back here and not to a new thread.
        req.add_header("X-Session-Id", session_id)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw else {}


def _post_reply(reply_to: str, text: str, sid: str) -> dict:
    """`ticket 748` / `doc 300` / `task 678` → the comment endpoint for it."""
    kind, _, oid = reply_to.replace("#", "").strip().partition(" ")
    path = {"ticket": "tickets", "doc": "docs", "task": "tasks"}.get(kind)
    if not path or not oid.strip().isdigit():
        raise ValueError(f"cannot address a reply to {reply_to!r}")
    return _api("POST", f"/{path}/{oid.strip()}/comments",
                {"body": text}, session_id=sid)


def cap_notice(cap_name: str, cap_value: int, took: float, conv: str,
               attempted: bool, ids=()) -> str:
    """The comment a turn posts when a CAP ended it instead of an answer (#895).

    A turn that hits a cap used to tell nobody: the journal recorded
    `outcome: uncertain, comment: null`, the task stayed `todo` with no comment, and
    the only evidence was a file inside the seat — which on a container seat the
    requester generally cannot reach, the device being in a different group by
    design. Every other signal read positive, so it was indistinguishable from a
    healthy long turn.

    Deliberate wording, both halves measured:

    * "produced no output" would be FALSE of the turn and true only of stdout. A
      Hermes `-z` run emits at COMPLETION, so a killed turn has written 0 bytes BY
      CONSTRUCTION — the same artifact `_CAUSE_TRUNCATING` in the registry documents
      for the session watchdog, where 29 of 29 kills were working, none stalled.
      Saying the seat hung would be a lie in the overwhelming majority of cases.
    * the conversation handle is the whole of "capture partial output": the runtime
      already persisted the transcript, so the notice points at it rather than
      duplicating it. No new capture machinery, and nothing runtime-specific beyond
      the handle the adapter already obtains for `--resume`.

    `ids` is NOT decoration, and leaving it out was a real defect in 1.3.0. The
    plane refuses a byte-identical body re-posted on one thread inside 24h
    (`_replay_guard`, >=200 chars) — correctly, because in this fleet that is
    always a stale-payload replay. But a seat that starves N deliveries behind one
    long turn renders N notices that differ only in `elapsed`, and `{took:.0f}`
    rounds most of them together. Measured: of nine starved deliveries, FIVE were
    refused HTTP 409 and those threads were never told, with `elapsed` rounding
    deciding which four got through. The delivery's notification ids are what
    makes two reports of two different messages two different bodies — and they
    are also the only thing in the notice that says WHICH message went unanswered,
    which is the first question a reader has.
    """
    lines = [
        f"**No answer on this one — a cap ended the turn, not the work.**",
        "",
        f"- runtime: `{RUNTIME or 'unknown'}`",
        f"- cap that fired: `{cap_name}` = {cap_value}s",
        f"- elapsed: {took:.0f}s",
    ]
    if ids:
        lines.append("- unanswered message: notification "
                     + ", ".join(f"`{i}`" for i in ids))
    if attempted:
        lines += [
            "",
            "The turn ran and was killed at the cap. **Nothing was posted, but the "
            "working is not lost** — this runtime emits only a final answer, so a "
            "killed turn writes nothing to stdout by construction. Its transcript "
            "was persisted as it went.",
        ]
        if conv:
            lines += [
                "",
                f"Recoverable from conversation `{conv}` on the seat, and the next "
                f"delivery on this thread resumes it rather than starting over.",
            ]
        else:
            lines += [
                "",
                "No conversation handle was recovered, so this turn probably died "
                "before the runtime opened one.",
            ]
    else:
        lines += [
            "",
            "**This delivery was never attempted.** It waited behind another turn on "
            "the same seat and gave up before reaching the front, so no runtime ran "
            "and there is nothing to recover. Re-send it, or raise the cap.",
        ]
    lines += ["", "_Posted by the rail adapter because a turn that ends on a cap "
                  "must say so (#895)._"]
    return "\n".join(lines)


def _is_conflict(e: Exception) -> bool:
    return isinstance(e, urllib.error.HTTPError) and e.code == 409


def _notice_tiebreak(took: float) -> str:
    """A footer that makes two otherwise-identical notices distinguishable.

    Only reached when the delivery carried NO notification ids, which is the one
    case the ids cannot separate. Not a nonce: the pid is what guarantees two
    starved deliveries differ (each waits in its own adapter process), and the
    unrounded elapsed is the thing the line above rounds away. Both are what a
    reader needs to tell two reports apart, which a random token would not be.
    """
    return (f"\n\n_This delivery carried no notification id. It waited "
            f"{took:.3f}s in adapter pid {os.getpid()} — recorded here because the "
            f"rounded elapsed above is not enough to tell two starved deliveries "
            f"apart, and without it this report would have been refused as a "
            f"duplicate of another one._")


# ── the seat's turn lock ─────────────────────────────────────────────────────

class TurnLock:
    """One turn at a time on this seat. Held for the whole turn, released by
    the kernel if this process dies — so a crashed adapter never wedges the
    seat, the same property the rail's dispatch lock relies on."""

    def __init__(self, path: Path, wait_s: int):
        self.path, self.wait_s, self.fh = path, wait_s, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        deadline = time.time() + self.wait_s
        waited = False
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if waited:
                    log("turn lock acquired after waiting")
                return self
            except OSError:
                if not waited:
                    log(f"another turn is running — waiting up to {self.wait_s}s")
                    waited = True
                if time.time() > deadline:
                    self.fh.close()
                    raise TimeoutError(
                        f"the seat's runtime was busy for {self.wait_s}s")
                time.sleep(2)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        except OSError:
            pass
        return False


# ── one turn for the deliveries queued on one thread ────────────────────────
#
# The seat lock above is seat-wide and stays that way: a runtime that cannot be
# fed while it runs must run one turn at a time, whatever thread the turn belongs
# to. What was wrong is that N deliveries on ONE thread each ran a turn of their
# own. On a local-model seat, nine deliveries on one thread queued behind an
# in-flight turn on another, and would have woken as nine turns — nine full
# prefills of a 12,288-token budget to read one discussion, in series, on a pool
# that had wedged twice that week (#842).
#
# Every delivery still queues for the seat lock, exactly as before. What changes
# is what it does once it HOLDS the lock: it answers every delivery queued for
# its thread, not only its own, and the ones it answered for skip their turn when
# their own wait ends. So N deliveries cost one turn and N-1 exits.
#
# Nothing can be stranded, and that is the point of doing it this way round: a row
# is only ever answered by a process that holds the lock, and every row's own
# process is alive and waiting for the lock the whole time. A turn that gives up,
# or dies, costs its own delivery and nobody else's.


class ThreadQueue:
    """The deliveries queued for one thread, and which of them are already
    answered. Its own lock: the seat lock is what we are queueing for, so it
    cannot also be what guards the queue."""

    def __init__(self, tdir: Path):
        self.box = tdir / "queued"        # one json row per waiting delivery
        self.answered = tdir / "answered"  # one marker per delivery someone answered for
        self.lock_path = tdir / "queue.lock"
        self._fh = None

    def _locked(self):
        self._fh = open(self.lock_path, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self._fh

    def _unlock(self):
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
        except OSError:
            pass

    def enqueue(self, pid: int, row: dict) -> None:
        """Say what we are waiting to have answered, before we start waiting."""
        self._locked()
        try:
            self.box.mkdir(exist_ok=True)
            (self.box / f"{pid}.json").write_text(json.dumps(row, ensure_ascii=False))
        finally:
            self._unlock()

    def take(self, pid: int) -> tuple:
        """Called once the seat lock is HELD. Returns (mine_answered, others, left).

        `mine_answered` — an earlier turn on this thread already carried our
        delivery, so we have nothing to do and should not run a turn.

        Otherwise `others` are the queued deliveries we are answering for as well
        as our own, oldest first, while they fit inside MAX_PROMPT_CHARS, and
        `left` counts those that did not fit. A row left here is not lost: the
        process that enqueued it is still waiting for this same lock and will
        answer it next.

        All of it in ONE critical section, so no two turns can take the same row
        and no row can arrive into a prompt that is already built."""
        self._locked()
        try:
            marker = self.answered / str(pid)
            if marker.exists():
                try:
                    marker.unlink()
                except OSError:
                    pass
                return True, [], 0
            mine = self.box / f"{pid}.json"
            total = 0
            try:
                total = int(json.loads(mine.read_text()).get("chars") or 0)
                mine.unlink()
            except (OSError, ValueError):
                pass
            others, left = [], 0
            if self.box.is_dir():
                rows = []
                for f in self.box.iterdir():
                    try:
                        rows.append((f, json.loads(f.read_text())))
                    except (OSError, ValueError) as e:
                        log(f"queued row {f.name} unreadable ({e}) — left in place")
                rows.sort(key=lambda r: r[1].get("at") or 0)
                self.answered.mkdir(exist_ok=True)
                for f, row in rows:
                    chars = int(row.get("chars") or 0)
                    if MAX_PROMPT_CHARS and total + chars > MAX_PROMPT_CHARS:
                        left += 1
                        continue
                    others.append(row)
                    total += chars
                    # Tell that delivery's own process not to run a turn: it is
                    # still waiting for this lock and will read this when it wins.
                    try:
                        (self.answered / str(row.get("pid"))).touch()
                        f.unlink()
                    except OSError:
                        pass
            return False, others, left
        finally:
            self._unlock()

    def withdraw(self, pid: int) -> None:
        """Our turn is over. Drop anything of ours still lying about so a later
        turn does not answer for a delivery that has already been reported."""
        self._locked()
        try:
            for f in (self.box / f"{pid}.json", self.answered / str(pid)):
                try:
                    f.unlink()
                except OSError:
                    pass
        finally:
            self._unlock()


MERGE_RULE = ("\n\n---\n\n[merged delivery — these arrived while a turn was "
              "running on this seat, and are answered together]\n\n")


# ── the exclusive backend, when there is one ─────────────────────────────────

def _acquire_lease(resource: str, seconds: int):
    """A lease on the pool says TAKEN to every other seat, not just this one.
    The local lock covers this seat; the lease covers the fleet.

    The body is the plane's `LeaseRequest` and nothing else: it is a StrictModel
    (`extra="forbid"`), so an unknown field is a 422 and not a field the plane
    ignores. Until 1.2.1 this sent `duration_seconds` and `purpose`, neither of
    which exists, so every call was refused and every turn ran unleased — visible
    once per turn as "refused … running without it" and easy to read as transient
    (#843). `ttl_seconds` is the documented alias for `duration_s`; `service_id` is
    where a label belongs.
    """
    try:
        r = _api("POST", f"/resources/{resource}/leases",
                 {"ttl_seconds": seconds, "exclusive": True,
                  "service_id": "rail turn"})
        lid = r.get("lease_id") or (r.get("lease") or {}).get("id")
        log(f"lease {lid} on {resource}")
        return lid
    except Exception as e:
        log(f"lease on {resource} refused ({e}) — running without it")
        return None


def _release_lease(resource: str, lease_id) -> None:
    """Release by lease id. `DELETE /leases/{id}` is the only release route the
    plane has; until 1.2.1 this sent `DELETE /resources/{r}/leases/{id}`, where
    `{resource_id:path}` swallows the tail and the plane answers
    `Resource <r>/leases/<id> not found` — it never reached a lease (#843)."""
    if not lease_id:
        return
    try:
        _api("DELETE", f"/leases/{lease_id}")
        log(f"lease {lease_id} released")
    except Exception as e:
        log(f"lease {lease_id} release failed: {e}")


# ── the runtime ──────────────────────────────────────────────────────────────

def _build_argv(prompt: str, prompt_file: Path, conv: str):
    """(argv, stdin_text). Placeholders are substituted per-token after the
    template is split, so a prompt containing quotes, newlines or $ is never
    re-parsed by a shell."""
    if not RUNTIME_CMD:
        raise RuntimeError(
            "RAIL_RUNTIME_CMD is unset — this seat has no runtime to run. "
            "Set it in ~/.config/samcloud/wake.env (wake-install writes it).")
    template = shlex.split(RUNTIME_CMD)
    if conv and RUNTIME_RESUME:
        template += shlex.split(RUNTIME_RESUME)
    argv, uses_prompt = [], False
    for tok in template:
        if "{prompt_file}" in tok:
            tok, uses_prompt = tok.replace("{prompt_file}", str(prompt_file)), True
        elif "{prompt}" in tok:
            tok, uses_prompt = tok.replace("{prompt}", prompt), True
        if "{conv}" in tok:
            tok = tok.replace("{conv}", conv)
        argv.append(tok)
    return argv, (None if uses_prompt else prompt)


def _runtime_env(sid: str, thread_key: str) -> dict:
    """The turn's environment. SAMCLOUD_SESSION_ID is the conversation the rail
    bound, so anything the runtime writes for itself — sc-say, sc-new — stamps
    the same id this adapter stamps. A runtime that can write for itself and one
    that cannot then land in the same conversation."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "AI_AGENT"))}
    env["SAMCLOUD_SESSION_ID"] = sid
    env["SAMCLOUD_THREAD_KEY"] = thread_key
    env["SAMCLOUD_API"] = API
    env.pop("SAMCLOUD_TOKEN", None)     # the bearer is a file, not an env var
    # NOT a holder. The rail sets SAMCLOUD_HOLDER=1 on everything it launches,
    # and it reaches us because the rail launched the adapter — but the skill
    # reads that flag as "you are a held process; end it at the wrap-up with
    # kill -TERM $PPID". A turn's parent is this adapter, mid-post. Doc 265 §5.
    env.pop("SAMCLOUD_HOLDER", None)
    return env


def _extract_conv(turn_output: str) -> str:
    """The handle the runtime uses for the conversation this turn ran in.

    Read while the turn lock is still held, so `the most recent session` is
    unambiguously the one that just ran — that is what makes the cheap
    formulation (`hermes sessions list --limit 1`) correct rather than racy."""
    if not CONV_RE:
        return ""
    haystack = turn_output
    if CONV_CMD:
        try:
            r = subprocess.run(shlex.split(CONV_CMD), capture_output=True,
                               text=True, timeout=60)
            haystack = r.stdout + r.stderr
        except Exception as e:
            log(f"conversation probe failed ({e}) — the next turn starts fresh")
            return ""
    m = re.search(CONV_RE, haystack)
    return m.group(1) if m else ""


def _hermes_result(output, expected: str = "") -> tuple[str, str, bool]:
    """Read only this subprocess's protocol, never a global session listing.

    A unique init can identify unfinished work, but only one matching successful
    result can be posted. Human progress lines are not the final answer.
    """
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    events = []
    malformed = False
    for line in (output or "").splitlines():
        if not line.lstrip().startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # A truncated event after an init is normal on a killed process.
            malformed = True
            continue
        if isinstance(event, dict):
            events.append(event)
    inits = [e for e in events if e.get("type") == "system"
             and e.get("subtype") == "init"]
    results = [e for e in events if e.get("type") == "result"]
    ids = [e.get("session_id") for e in inits + results]
    if (len(inits) != 1 or events[0] is not inits[0]
            or any(not isinstance(s, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", s) for s in ids)
            or len(set(ids)) != 1 or (expected and ids[0] != expected)):
        return "", "", False
    conv = ids[0]
    if len(results) != 1:
        return conv, "", False
    result = results[0]
    answer = result.get("text")
    ok = (not malformed and events[-1] is result
          and not any(e.get("type") == "error" for e in events)
          and type(result.get("exit_code")) is int and result["exit_code"] == 0
          and isinstance(answer, str) and bool(answer.strip()))
    return conv, answer.strip() if ok else "", ok


def run_turn(key: str, sid: str, prompt: str, prompt_file: Path, tdir: Path):
    """(outcome, text, seconds, conv). Outcomes are doc 265 §4's vocabulary:
    accepted / unavailable / uncertain / failed.

    `conv` is the runtime's conversation handle for this turn, or "" when there is
    none. It is returned — not just written to the conv file — because a turn killed
    at the cap posts it to the thread, so the requester can reach the working the
    kill did not destroy (#895)."""
    conv_file = tdir / "conv"
    conv = ""
    try:
        conv = conv_file.read_text().strip()
    except OSError:
        pass
    # What the runtime calls its most recent conversation BEFORE this turn. A
    # handle is recorded afterwards only if the turn produced a different one:
    # a turn that fails before starting a session leaves "the most recent" as
    # someone else's. On 2026-09-25 that re-pinned the 108,753-token Hermes
    # session whose prefill had panicked the host two hours earlier (#838).
    structured = OUTPUT_MODE == "hermes-stream-json"
    if OUTPUT_MODE not in {"text", "hermes-stream-json"}:
        return "unavailable", "", 0.0, conv
    before = _extract_conv("") if CONV_CMD and not structured else ""
    argv, stdin_text = _build_argv(prompt, prompt_file, conv)
    log(f"{key}: {RUNTIME or 'runtime'} turn "
        f"({'resume ' + conv if conv else 'new conversation'}), "
        f"{len(prompt)} chars in")
    started = time.time()
    try:
        r = subprocess.run(argv, input=stdin_text, capture_output=True,
                           text=True, timeout=TURN_TIMEOUT,
                           env=_runtime_env(sid, key), cwd=str(Path.home()))
    except subprocess.TimeoutExpired as exc:
        # Not "failed": a local runtime that overran its deadline may still be
        # holding the backend, and we cannot know whether it would have
        # answered. Uncertain is the honest word, and nothing is replayed.
        #
        # But the turn's WORKING is not lost, and until #895 this path threw away
        # the only handle to it. The runtime persists its own conversation (Hermes
        # keeps the full transcript in ~/.hermes/state.db, which survives the
        # kill): measured on one seat's first real job, 33 messages, 14
        # assistant turns and 18 tool calls were all still there after a 1800s
        # kill that reported `chars_out: 0`. The probe reads the runtime's own
        # store, NOT the dead process's stdout, so it works after a kill exactly
        # as it does after a clean exit.
        #
        # Recording it is what makes the next delivery RESUME that session instead
        # of starting a fresh one and orphaning the work. The `before` guard is the
        # same one the clean path uses (#838): if the runtime's most recent
        # conversation still predates this turn, this turn never opened one and the
        # handle belongs to someone else.
        took = time.time() - started
        killed_conv = (_hermes_result(exc.stdout, conv)[0] if structured
                       else _extract_conv("") if CONV_CMD else "")
        if killed_conv and killed_conv == before:
            killed_conv = ""
        if killed_conv and killed_conv != conv:
            try:
                conv_file.write_text(killed_conv)
                log(f"{key}: killed at the cap; conversation handle "
                    f"{killed_conv} recorded — the next turn resumes it")
            except OSError as e:
                log(f"{key}: could not record the conversation handle: {e}")
        return "uncertain", "", took, killed_conv or conv
    except (OSError, RuntimeError) as e:
        log(f"{key}: runtime could not be started: {e}")
        return "unavailable", "", time.time() - started, conv
    took = time.time() - started
    try:
        with (tdir / "err.log").open("a") as err:
            err.write(r.stderr[-20000:])
    except OSError:
        pass
    text = (r.stdout or "").strip()
    protocol_ok = True
    if structured:
        new_conv, text, protocol_ok = _hermes_result(r.stdout, conv)
    else:
        new_conv = _extract_conv(r.stdout + "\n" + r.stderr)
    if new_conv and new_conv == before and new_conv != conv:
        log(f"{key}: the runtime's most recent conversation is still {new_conv}, "
            f"which predates this turn — not recording it")
        new_conv = ""
    if new_conv and new_conv != conv:
        try:
            conv_file.write_text(new_conv)
            log(f"{key}: conversation handle {new_conv}")
        except OSError as e:
            log(f"{key}: could not record the conversation handle: {e}")
    # The seat declares what its runtime's own failure text looks like, and that
    # declaration is about the TEXT, not the exit code. Hermes prints a provider
    # failure to stdout and exits 0 — which is why FAIL_RE exists — but on
    # 2026-09-21 it printed one and exited 2, and checking the pattern only on a
    # clean exit posted that second failure as an answer. Check it first.
    if FAIL_RE and text:
        try:
            if re.search(FAIL_RE, text):
                log(f"{key}: the runtime reported its own failure in {took:.0f}s "
                    f"(exit {r.returncode}, {text.strip()[:120]!r}) — "
                    f"journalled, not posted")
                return "unavailable", text, took, new_conv or conv
        except re.error as e:
            log(f"{key}: RAIL_RUNTIME_FAIL_RE does not compile ({e}) — "
                f"treating the turn as an answer")
    if r.returncode != 0 or not protocol_ok:
        log(f"{key}: runtime exited {r.returncode}, protocol_ok={protocol_ok} after {took:.0f}s; "
            f"stderr tail: {r.stderr.strip()[-300:]!r}")
        # Partial stdout is useful recovery evidence, not a successful answer.
        return "failed", text, took, new_conv or conv
    if not text:
        log(f"{key}: runtime said nothing in {took:.0f}s — nothing is posted")
        return "failed", "", took, new_conv or conv
    return "accepted", text, took, new_conv or conv


# ── the journal ──────────────────────────────────────────────────────────────

def journal(tdir: Path, record: dict) -> None:
    record["ts"] = int(time.time())
    try:
        with open(tdir / "adapter.jsonl", "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"journal append failed (non-fatal): {e}")


def _finish(a, live: Path, me: Path, rc: int, close_binding: bool = True) -> int:
    """Deregister this turn and, if it was the last one on the thread, close the
    binding.

    `close_binding=False` for a delivery that MERGED into a waiter. It never ran a
    turn — the waiter answers for it — and it can exit within milliseconds, before
    that waiter has registered under `live/`. Closing the binding on that scan
    would close a thread whose turn is about to start, which is #832's bug from a
    new direction. The waiter closes it when it ends, which is the rule."""
    try:
        me.unlink()
    except OSError:
        pass
    if not close_binding:
        if a.binding_thread:
            log(f"{a.key}: merged, so the binding stays with the waiter")
        return rc
    others = []
    for f in live.iterdir():
        try:
            pid = int(f.name)
            os.kill(pid, 0)
            others.append(pid)
        except (ValueError, ProcessLookupError):
            try:
                f.unlink()                     # a turn that died without cleaning up
            except OSError:
                pass
        except PermissionError:
            others.append(f.name)
    if a.binding_thread and others:
        log(f"{a.key}: another turn on this thread is still running ({others}) — binding stays open")
    elif a.binding_thread:
        # The last turn IS the holder: when it ends the thread is no longer held,
        # exactly as a stream holder's launcher reports at exit. No timer
        # decides this and no sweep has to notice it later.
        try:
            _api("PATCH", f"/wake/threads/{a.binding_thread}", {"state": "closed"})
        except Exception as e:
            log(f"{a.key}: binding close failed: {e}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description=__doc__.split("\n")[0])
    ap.add_argument("--key", required=True, help="the rail's thread key, e.g. ticket-748")
    ap.add_argument("--sid", required=True, help="the conversation id the rail bound")
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--reply-to", required=True, help='e.g. "ticket 748"')
    ap.add_argument("--ids", default="[]", help="notification ids to ack after a posted reply")
    ap.add_argument("--binding-thread", default="", help="plane thread id to close on exit")
    a = ap.parse_args()

    tdir = STATE_DIR / "threads" / a.key
    tdir.mkdir(parents=True, exist_ok=True)
    try:
        tdir.chmod(0o700)
    except OSError:
        pass
    prompt = Path(a.prompt_file).read_text()
    ids = json.loads(a.ids or "[]")
    # Every turn on this thread, running or waiting, is registered here. The
    # binding closes only when the LAST of them ends (#832): a waiter that gave
    # up, or a turn that finished while another was queued behind it, used to
    # close a thread that was still being worked.
    live = tdir / "live"
    live.mkdir(exist_ok=True)
    me = live / str(os.getpid())
    me.touch()

    outcome, text, took, posted = "failed", "", 0.0, None
    # Which cap ended this turn, if one did: (name, seconds). Two of them can, and
    # they are different stories to tell — one killed a running turn, the other never
    # let it start (#895).
    cap: tuple = ()                 # (name, seconds, attempted)
    conv = ""
    notice = None
    notice_duplicate = False        # the thread already carried this delivery's notice
    notice_tiebroken = False        # an idless notice needed a discriminator to land
    lease_id = None
    merged_in: list = []
    left_behind = 0
    queue = ThreadQueue(tdir)
    # Say what we are waiting on before we wait, so whichever turn wins the lock
    # can answer for us instead of leaving us to run a turn of our own.
    queue.enqueue(os.getpid(), {"pid": os.getpid(), "prompt": prompt, "ids": ids,
                                "chars": len(prompt), "at": time.time()})
    # When the lock starves us, `took` is 0 — run_turn never ran — and reporting
    # "elapsed: 0s" for a delivery that waited RAIL_RUNTIME_WAIT would read as an
    # instant refusal. Measure the wait so the notice says what actually happened.
    waiting_since = time.time()
    try:
        with TurnLock(STATE_DIR / "runtime.lock", LOCK_WAIT):
            already, merged_in, left_behind = queue.take(os.getpid())
            if already:
                # An earlier turn on this thread carried our delivery. Running a
                # turn now would ask the runtime the same question twice, which on
                # a seat behind a single-slot pool is the whole cost we are trying
                # to avoid.
                log(f"{a.key}: already answered by an earlier turn on this thread "
                    f"— no turn of my own")
                outcome = "merged"
            else:
                if left_behind:
                    log(f"{a.key}: {left_behind} queued deliver"
                        f"{'y' if left_behind == 1 else 'ies'} left for the next "
                        f"turn — this one is at RAIL_RUNTIME_MAX_PROMPT_CHARS="
                        f"{MAX_PROMPT_CHARS}")
                prompt_path = Path(a.prompt_file)
                if merged_in:
                    prompt = prompt + "".join(
                        MERGE_RULE + r["prompt"] for r in merged_in)
                    ids = ids + [i for r in merged_in for i in r.get("ids", [])]
                    # _build_argv may hand the runtime a PATH ({prompt_file}), so
                    # the merged text has to exist as a file or that runtime reads
                    # only the first delivery.
                    merged_file = tdir / f"turn-merged-{os.getpid()}.txt"
                    merged_file.write_text(prompt)
                    prompt_path = merged_file
                    log(f"{a.key}: answering {len(merged_in) + 1} queued deliveries "
                        f"in one turn of {len(prompt)} chars")
                if LEASE_RESOURCE:
                    lease_id = _acquire_lease(LEASE_RESOURCE, TURN_TIMEOUT)
                try:
                    outcome, text, took, conv = run_turn(a.key, a.sid, prompt,
                                                        prompt_path, tdir)
                    if outcome == "uncertain":
                        # run_turn returns `uncertain` from exactly ONE place: the
                        # subprocess.TimeoutExpired handler. Asserted by
                        # test_adapter_cap_notice.py::test_uncertain_comes_only_from_the_cap,
                        # which fails if another return ever starts using the word.
                        cap = ("RAIL_RUNTIME_TIMEOUT", TURN_TIMEOUT, True)
                finally:
                    if LEASE_RESOURCE:
                        _release_lease(LEASE_RESOURCE, lease_id)
    except TimeoutError as e:
        log(f"{a.key}: {e} — this delivery is not answered")
        outcome = "unavailable"
        # Starvation, and the quietest of the three: this delivery was never
        # attempted, so there is no journal of a turn and no transcript anywhere.
        # Nothing but the notice records that the message existed (#895).
        cap = ("RAIL_RUNTIME_WAIT", LOCK_WAIT, False)
        took = time.time() - waiting_since
    finally:
        queue.withdraw(os.getpid())

    if outcome == "accepted" and text:
        body = text
        if len(body) > MAX_CHARS:
            body = (body[:MAX_CHARS]
                    + f"\n\n… [cut at {MAX_CHARS} characters of {len(text)}]")
        try:
            resp = _post_reply(a.reply_to, body, a.sid)
            posted = resp.get("comment_id") or resp.get("id")
            log(f"{a.key}: replied on {a.reply_to} (comment {posted}) "
                f"after {took:.0f}s")
        except Exception as e:
            # The turn happened and the plane did not take it. Say so; the rows
            # are already delivered, so nothing replays, and the journal is the
            # record of a turn whose answer never landed.
            log(f"{a.key}: reply REFUSED by the plane ({e}) — "
                f"{len(text)} chars are in the journal, not on the thread")
            outcome = "uncertain"

    # A cap ended the turn: say so on the thread. Placed AFTER the answer path so it
    # can never race it, and gated on `not posted` so a turn that somehow both hit a
    # cap and answered says one thing, not two.
    #
    # The ids are deliberately NOT acked below: a notice is not an answer, and acking
    # would mark the requester's message handled when nothing handled it.
    if cap and not posted:
        body = cap_notice(cap[0], cap[1], took, conv, attempted=cap[2], ids=ids)
        try:
            resp = _post_reply(a.reply_to, body, a.sid)
            notice = resp.get("comment_id") or resp.get("id")
            log(f"{a.key}: {cap[0]} fired at {cap[1]}s — notice posted on "
                f"{a.reply_to} (comment {notice})")
        except Exception as e:
            # A 409 is the replay guard, and it means something DIFFERENT depending
            # on whether this delivery had ids to be identified by.
            if _is_conflict(e) and ids:
                # The body carries this delivery's own ids, so an identical body
                # can only be this delivery's own notice, already on the thread —
                # the plane re-offered rows a previous adapter had already reported
                # (they are deliberately never acked, two comments down). Told once
                # is told; a second copy is the replay the guard exists to stop.
                notice_duplicate = True
                log(f"{a.key}: {cap[0]} fired and {a.reply_to} ALREADY carries this "
                    f"delivery's notice (notification {ids}) — not posting a second")
            elif _is_conflict(e):
                # No ids, so two starved deliveries render the same text and the
                # guard cannot tell them apart. Without this the thread is simply
                # never told, which is #895's own bug reintroduced by its fix.
                try:
                    resp = _post_reply(a.reply_to, body + _notice_tiebreak(took),
                                       a.sid)
                    notice = resp.get("comment_id") or resp.get("id")
                    notice_tiebroken = True
                    log(f"{a.key}: {cap[0]} fired — notice was refused as a replay "
                        f"(no ids to tell it apart), re-posted with the pid and "
                        f"unrounded elapsed (comment {notice})")
                except Exception as e2:
                    log(f"{a.key}: {cap[0]} fired and BOTH the notice and its "
                        f"tie-broken retry were refused ({e2}) — {a.reply_to} was "
                        f"not told")
            else:
                # The thread stays silent and that is now a known, logged state
                # rather than an invisible one. Nothing is replayed: the rows are
                # unacked, so the plane may offer them again.
                log(f"{a.key}: {cap[0]} fired and the cap notice was REFUSED by the "
                    f"plane ({e}) — {a.reply_to} was not told")

    if posted and ids:
        try:
            _api("POST", "/notifications/ack", {"ids": ids, "surface": "wake"})
        except Exception as e:
            log(f"{a.key}: ack failed (bookkeeping only): {e}")

    if outcome == "merged":
        # Answered by another turn on this thread. Record it — a silent merge and a
        # dropped delivery look identical afterwards, which is what #832 turns on —
        # and leave the binding alone: this process ran no turn, and the one that
        # answered for us closes it when it ends.
        journal(tdir, {"key": a.key, "sid": a.sid, "runtime": RUNTIME,
                       "outcome": "merged", "reply_to": a.reply_to,
                       "chars_in": len(prompt), "ids": ids})
        return _finish(a, live, me, 0, close_binding=False)
    record = {"key": a.key, "sid": a.sid, "runtime": RUNTIME,
              "outcome": outcome, "seconds": round(took, 1),
              "reply_to": a.reply_to, "comment": posted,
              "chars_in": len(prompt), "chars_out": len(text), "ids": ids}
    if cap:
        # `outcome: uncertain, comment: null` was true and unreadable: it did not say
        # WHICH cap, nor that the thread had been told, nor where the work went. A
        # reader of this line should not have to go and ask (#895).
        record["cap"] = cap[0]
        record["cap_seconds"] = cap[1]
        record["notice"] = notice
        record["attempted"] = cap[2]
        # Present only when they happened, because `notice: null` alone cannot
        # distinguish "the thread was not told" from "an earlier adapter already
        # told it" — in 1.3.0 those were the same row. Bare null still means NOT
        # told; null beside `notice_duplicate` means the report is on the thread.
        if notice_duplicate:
            record["notice_duplicate"] = True
        if notice_tiebroken:
            record["notice_tiebroken"] = True
        if conv:
            record["conv"] = conv
    if merged_in:
        record["merged"] = [{"pid": r.get("pid"), "chars": r.get("chars"),
                             "ids": r.get("ids", [])} for r in merged_in]
        record["deliveries"] = len(merged_in) + 1
    if left_behind:
        # Named, because a reader counting turns cannot otherwise tell a boundary
        # from a merge that failed.
        record["left_for_next_turn"] = left_behind
        record["prompt_bound"] = MAX_PROMPT_CHARS
    if outcome != "accepted" and text:
        # What it said instead. A failure whose text is only in a log line is
        # a failure you have to go and find; the journal is the record of the
        # turn and it should carry the reason the thread stayed silent.
        record["unposted"] = text[:2000]
    journal(tdir, record)

    return _finish(a, live, me, 0 if outcome == "accepted" else 1)


if __name__ == "__main__":
    sys.exit(main())
