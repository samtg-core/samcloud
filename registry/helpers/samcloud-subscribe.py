# SUBSCRIBE_VERSION: 2.13.0
# samcloud-subscribe — the interactive session's inbound edge (rail 3.10).
#
# A headless holder is written into: the rail puts one thread's messages in its
# fifo, marks them delivered, and never re-offers them. This is the same thing
# for the session a HUMAN types into. It registers the harness session with the
# plane, asks only for what the rail would deliver to that conversation, prints
# it, and receipts what it printed.
#
# Run it as a BACKGROUND SUBROUTINE of a live session (Claude Code: the Monitor
# tool with persistent=true; any harness: whatever runs a child process and
# surfaces its stdout lines as events). Each stdout line becomes an in-session
# notification — that is the push edge; the long-poll underneath is transport.
#
# What changed in 2.0.0, and why (an interactive surface got every message and
# had to pull them, while held sessions were pushed theirs):
#   - REGISTERS (POST /wake/surfaces) so the plane knows this conversation
#     exists. Before this the plane could not see the surface that coordinates
#     the fleet, because it did not create it.
#   - SCOPED to `?for_conversation=<id>` — the threads this conversation is on
#     (2.9.0 / 0.12.69: a mention or assignment on a thread none of your
#     conversations is on is a cold wake for the rail, never pushed here). Measured on
#     one admin inbox before the change: 13% of 200 rows were addressed to the
#     reader, 56 distinct threads, whole conversations belonging to other
#     agents arriving in full.
#   - RECEIPTS every row it prints (POST /notifications/delivered) and pages
#     `?undelivered=true`, so nothing is offered twice and NOBODY has to ack to
#     keep the window moving. The old build leased rows `seen` and paged
#     `unread`, so an unacked row sat in the window forever and the watch page
#     starved (#539) until a human hand-cranked the cursor.
#   - No claims refresh, no presence, no `seen` lease: rail 3.9 routes on the
#     thread record and delivery receipts, not on leases (doc 174).
#
# `ack` goes back to meaning what it always said: I handled this. Nothing in
# the transport depends on it.
#
# Usage (fetch-and-run, no install):
#   curl -s -H @"$HOME/.samcloud/token.hdr" __API__/helpers/subscribe | python3 -   (#582: header file, never the bearer in argv)
# Env: SAMCLOUD_SESSION_ID  — the harness session id. STRONGLY preferred: it is
#        what the UserPromptSubmit hook already stamps, so the digest, your
#        writes and this feed become one conversation instead of three.
#      SAMCLOUD_TOKEN_FILE (default: ./.samcloud/token then ~/.samcloud/token)
#      SAMCLOUD_API (default: serve-time host), SAMCLOUD_SUB_SEEN,
#      SAMCLOUD_SUB_TIMEOUT, SAMCLOUD_SUB_THREAD (watch ONE lane),
#      SAMCLOUD_SUB_SCOPE=all (opt out of scoping — the old firehose, for an
#        operator who deliberately wants every row this identity can see).
import atexit
import json
import os
import pathlib
import signal
import socket
import sys
import time
import urllib.request

# Single source of truth — keep the header stamp above in sync (it is the
# grep-able fleet convention; this constant is what the banner prints).
SUBSCRIBE_VERSION = "2.13.0"

_served = "__API__"
API = os.environ.get("SAMCLOUD_API") or (_served if _served.endswith("/api/v1") else "")
if not API:
    # An unrendered copy (run from a checkout) names no plane, and guessing one
    # would watch somebody else's (#1010). The served copy carries its plane.
    sys.exit("samcloud-subscribe: set SAMCLOUD_API to your plane's API, e.g. https://cloud.example.org/api/v1")
if not API.rstrip("/").endswith("/api/v1"):
    API = API.rstrip("/") + "/api/v1"

tok = ""
_cands = [os.environ.get("SAMCLOUD_TOKEN_FILE"),
          os.path.join(os.getcwd(), ".samcloud", "token"),
          os.path.expanduser("~/.samcloud/token")]
for c in _cands:
    if c and os.path.exists(c):
        tok = pathlib.Path(c).read_text().strip()
        if tok:
            break
if not tok:
    print(f"samcloud-subscribe: no token found (tried {_cands})", flush=True)
    sys.exit(1)

# Verify the token against THIS API before watching (staging #86: a prod
# token pointed at staging 401-looped forever behind a healthy-looking
# banner — a silent failure). Die loudly, name the mismatch.
try:
    _vreq = urllib.request.Request(f"{API}/auth/verify",
                                   headers={"Authorization": f"Bearer {tok}"})
    _v = json.load(urllib.request.urlopen(_vreq, timeout=15))
    IDENTITY = _v.get("username") or _v.get("owner_id") or "?"
except Exception as e:
    print(f"samcloud-subscribe: token does not verify against {API} "
          f"({e}) — wrong plane's token? Set SAMCLOUD_TOKEN_FILE to the "
          f"token for THIS api. Exiting instead of 401-looping.", flush=True)
    sys.exit(1)

# The conversation this surface IS. Prefer the harness id the rest of the
# session already uses; fall back to a per-process id so an unstamped harness
# still gets scoping and receipts (it just cannot share a conversation with
# its own writes — which is the drift this variable exists to close).
# The conversation this feed IS: the rail's env for a holder, else the harness's
# own id for a Claude Code session (CLAUDE_CODE_SESSION_ID — set in every tool
# shell, so the feed and the session's writes share one id with nothing to export).
CONV = (os.environ.get("SAMCLOUD_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()[:64]
_derived = False
if not CONV:
    CONV = f"subscribe-{socket.gethostname()}-{os.getpid()}"[:64]
    _derived = True

LANE = os.environ.get("SAMCLOUD_SUB_THREAD", "").strip()
# 2.8.0: "jsonl" — one JSON object per message on stdout, for samcloud-channel.
# Anything else (the default) is the Monitor shape: wrapped lines, split pieces.
SINK = os.environ.get("SAMCLOUD_SUB_SINK", "").strip().lower()
SCOPED = os.environ.get("SAMCLOUD_SUB_SCOPE", "").strip().lower() != "all"
seen_p = pathlib.Path(os.environ.get(
    "SAMCLOUD_SUB_SEEN", os.path.expanduser("~/.cache/samcloud-sub-seen")))
timeout = int(os.environ.get("SAMCLOUD_SUB_TIMEOUT", "120"))
seen_p.parent.mkdir(parents=True, exist_ok=True)
seen = set(seen_p.read_text().split()) if seen_p.exists() else set()


def _post(path, payload, quiet=True):
    try:
        req = urllib.request.Request(
            f"{API}{path}", data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {tok}",
                     "Content-Type": "application/json",
                     "X-Session-Id": CONV})
        return json.load(urllib.request.urlopen(req, timeout=15))
    except Exception as e:
        if not quiet:
            print(f"samcloud-subscribe: {path} failed ({e})", flush=True)
        return None


# Register the surface. A failure here is not fatal — an older registry has no
# such route — but it IS the difference between scoped delivery and the
# firehose, so say which one the reader is getting rather than pretending.
_reg = _post("/wake/surfaces", {"conversation_id": CONV, "surface": "interactive",
                                "host": socket.gethostname(), "client": "subscribe"})
registered = bool(_reg)
if SCOPED and not registered:
    SCOPED = False
    print("samcloud-subscribe: /wake/surfaces not available on this registry — "
          "falling back to the identity-wide feed (every row you can see, "
          "delivered without receipts). Upgrade the registry for scoped "
          "delivery.", flush=True)

# 2.4.0 — say goodbye on the way out. The plane models `ended` and the API
# documents it ("{ended:true} on the way out"); this file never sent it, so the
# only durable signal that a surface is gone did not exist and the rail fell
# back to `connected` — a live-poll flag that any registry deploy clears. A
# holder's binding ends when its process exits; a surface's ends here.
# ...and 2.5.0 takes the automatic half of it back out, because a process
# exiting is not a conversation ending and this file cannot tell the two
# apart. The surface IS the conversation, not the process that reads for it:
# re-arming a background subscriber (Claude Code's Monitor expires every 30
# minutes) kills one reader and starts another on the SAME conversation, and
# the dying one was announcing that the person had left.
#
# Measured on the admin seat: 22:37:35 the rail's sweep found no
# registered surface and closed the person's bindings on three threads —
# `conversation 060ee6b1 is gone (no registered surface)` — while that session
# was mid-sentence. 22:41:26 a headless holder was born on one of them. It was
# still there 21 hours later, answering. Two more like it were resident on
# other threads. The goodbye landed in the seconds between one reader exiting
# and the next registering, and the sweep ran inside that window.
#
# So `ended` stays the ONLY way a surface is retired, and it stays exactly
# where it was — but it is now something a person or an operator says on
# purpose, not something a SIGTERM says on their behalf. The cost is that a
# session which truly ends leaves its threads held until someone says so; that
# is the trade 0.12.13 already made in the other direction and wrote down: an
# unread message is not an outage, a twin acting under your name is.
def _say_goodbye():
    """Retire this surface. Explicit only — SAMCLOUD_SUB_GOODBYE=1 restores
    the 2.4.0 behaviour for a runtime that genuinely knows its session is
    over (a one-shot wrapper, a harness with a real session-end hook)."""
    try:
        _post("/wake/surfaces", {"conversation_id": CONV, "surface": "interactive",
                                 "host": socket.gethostname(), "client": "subscribe",
                                 "ended": True})
    except Exception:
        pass


if os.environ.get("SAMCLOUD_SUB_GOODBYE") == "1":
    atexit.register(_say_goodbye)
for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    try:
        signal.signal(_sig, lambda *_a: sys.exit(0))   # let atexit run
    except (ValueError, OSError):
        pass

_holding = (_reg or {}).get("holding") or []
# The plane echoes the raw id back to its owner as `conversation_id` and the
# fingerprint as `conversation` (same shape as a thread row). We already know
# our own id; this is only a sanity check that the plane agrees.
_echo = ((_reg or {}).get("surface") or {}).get("conversation_id")
if _echo and _echo != CONV:
    print(f"samcloud-subscribe: the plane registered {_echo[:12]}… but this "
          f"process is using {CONV[:12]}… — refusing to watch a conversation "
          f"that is not mine.", flush=True)
    sys.exit(1)
print(f"samcloud-subscribe {SUBSCRIBE_VERSION}: {IDENTITY} on {API}"
      + (f" [lane {LANE}]" if LANE else "")
      + (f" · conversation {CONV[:8]}" if not _derived else
         f" · conversation {CONV[:8]} (derived — export SAMCLOUD_SESSION_ID so "
         f"your writes and this feed are the same conversation)")
      + (f" · holding {', '.join(_holding)}" if _holding else "")
      + (" · scoped: the threads this conversation is on; delivered as "
         "printed, never re-offered" if SCOPED else
         " · UNSCOPED (every row this identity can see)"), flush=True)

# 2.1.1 — the backlog is history, not arrivals (old rows should not appear
# as a list on connect). A holder is told what rang and reads the
# thread from the plane if it wants the past; it is never re-fed. So on connect
# the rows that were waiting are RECEIPTED to this conversation — no other
# conversation of this identity will be offered them — and summarised in one
# line (which threads, how many); only what arrives from now on is printed.
# The rows stay in the inbox unread: `GET /notifications?unread=true` is the
# place to read history, on purpose.
if SCOPED and registered:
    try:
        _breq = urllib.request.Request(
            f"{API}/notifications?unread=true&undelivered=true&limit=200&for_conversation={CONV}",
            headers={"Authorization": f"Bearer {tok}", "X-Session-Id": CONV})
        _backlog = json.load(urllib.request.urlopen(_breq, timeout=20)).get("notifications") or []
    except Exception:
        _backlog = []
    if _backlog:
        _bykey = {}
        for r in _backlog:
            k = (f"#{r['ticket_id']}" if r.get("ticket_id") else f"doc #{r['doc_id']}" if r.get("doc_id")
                 else f"task #{r['task_id']}" if r.get("task_id") else "lobby")
            _bykey[k] = _bykey.get(k, 0) + 1
        print(f"backlog: {len(_backlog)} older message{'s' if len(_backlog) != 1 else ''} on "
              f"{len(_bykey)} thread{'s' if len(_bykey) != 1 else ''} ("
              + ", ".join(f"{k}×{n}" if n > 1 else k for k, n in sorted(_bykey.items(), key=lambda kv: -kv[1])[:12])
              + (", …" if len(_bykey) > 12 else "")
              + ") — receipted, not shown; read them with GET /notifications?unread=true", flush=True)
        _post("/notifications/delivered",
              {"ids": [int(r["id"]) for r in _backlog if "id" in r], "conversation_id": CONV})
        seen |= {str(r["id"]) for r in _backlog}
        seen_p.write_text(" ".join(sorted(seen, key=int)[-500:]))

# ── the doorbell (2.6.0) ─────────────────────────────────────────────────────
# 2.0.0 read with `GET /notifications/watch?timeout=120` and slept 2s between
# turns, while every headless seat's listener held the plane's SSE stream open
# and ran on arrival. That asymmetry was the last one left: same delivery rule,
# same rendering, different transport — the interactive surface polling for
# what the rail is pushed.
#
# So: hold `GET /events?for_conversation=<id>` (0.12.20 scopes it with the same
# rule the read applies) and fetch when it says something happened. The stream
# is the doorbell; the inbox is the post. Nothing here is a clock — the 15s
# keepalive is the server's, and it is what makes a dead socket detectable
# rather than what paces the loop.
_STREAM = {"fh": None}
_EV_TIMEOUT = int(os.environ.get("SAMCLOUD_SUB_STREAM_TIMEOUT", "60"))


def _wait_for_event() -> bool:
    """Block until the plane says something happened for this conversation.

    True = go read. On a fresh connection that is immediately true: a
    reconnect reconciles, exactly as the listener does, so anything that
    landed while the socket was down costs nothing. A keepalive is not an
    event and does not wake the reader."""
    fh = _STREAM.get("fh")
    if fh is None:
        req = urllib.request.Request(
            f"{API}/events?for_conversation={CONV}",
            headers={"Authorization": f"Bearer {tok}",
                     "Accept": "text/event-stream", "X-Session-Id": CONV})
        _STREAM["fh"] = urllib.request.urlopen(req, timeout=_EV_TIMEOUT)
        return True                       # reconcile on connect
    line = fh.readline()
    if not line:                          # the stream ended: reconnect next turn
        _STREAM["fh"] = None
        return False
    return line.startswith(b"data:")


while True:
    try:
        if not _wait_for_event():
            continue
        _url = f"{API}/notifications?unread=true&undelivered=true&limit=50&delivery=true"
        if LANE:
            _url += f"&thread={LANE}"
        if SCOPED:
            _url += f"&for_conversation={CONV}"
        req = urllib.request.Request(
            _url, headers={"Authorization": f"Bearer {tok}",
                           "X-Session-Id": CONV})
        rows = json.load(urllib.request.urlopen(req, timeout=30)) \
            .get("notifications") or []
    except Exception:
        rows = []
        _STREAM["fh"] = None
        time.sleep(5)
    new = [r for r in rows if str(r.get("id")) not in seen]
    # 2.1.0 — print exactly what the rail delivers to a holder (a surface
    # should match the rail): rows grouped by thread, chronological,
    # `actor (HH:MM UTC):` and the whole body, status changes as sentences,
    # one footer with the thread ref and the reply line. No thread tail: a
    # tail rides a delivery that starts a process, and a surface is a running
    # conversation — it gets what was added (2.9.1, rail 3.13.4, doc 355).
    # Same shapes as samcloud-rail.py's `_digest` / `continue_prompt` /
    # `wake_prompt` / `_footer`, minus `holding` (a footer is not a census,
    # #694) and minus the wrap-up's exit command (a surface has no process to
    # end). One implementation would be better than two — the plane rendering
    # a delivery for whichever edge asks is the step after this.
    def _key(r):
        """One unit of work, one key (doc 239): the row's thread stamp; a
        pre-v19 row keys on its object."""
        if r.get("thread_id") is not None:
            return f"thread-{r['thread_id']}"
        for f, pl in (("ticket_id", "ticket"), ("doc_id", "doc"), ("task_id", "task")):
            if r.get(f) is not None:
                return f"{pl}-{r[f]}"
        return "lobby"
    def _obj(r):
        """`task 648` — the member object a row sits on."""
        for f, pl in (("ticket_id", "ticket"), ("doc_id", "doc"), ("task_id", "task")):
            if r.get(f) is not None:
                return f"{pl} {r[f]}"
        return ""
    _tinfo = {}
    def _thread(key):
        """GET /threads/{id} for a `thread-N` key, once per pass."""
        if key not in _tinfo:
            _tinfo[key] = {}
            if key.startswith("thread-") and key[7:].isdigit():
                try:
                    req = urllib.request.Request(f"{API}/threads/{key[7:]}",
                                                 headers={"Authorization": f"Bearer {tok}", "X-Session-Id": CONV})
                    _tinfo[key] = json.load(urllib.request.urlopen(req, timeout=15)) or {}
                except Exception:
                    pass
        return _tinfo[key]
    def _ref(key):
        if key == "lobby":
            return "general inbox (lobby)"
        if key.startswith("thread-"):
            return f"thread {key[7:]}"
        return key.replace("-", " #", 1)
    def _sentence(r):
        p = r.get("payload") or {}; who = r.get("actor") or "someone"
        plane = (_obj(r).split(" ", 1)[0] if _obj(r) else "") or "thread"   # the row's own object, not the key
        st = str(p.get("status") or "")
        et = str(r.get("event_type") or "")
        if et in ("task.created", "ticket.created"):          # a create is a sentence, as the rail says it (2.2.3)
            name = p.get("title") or p.get("summary") or ""
            return f"{who} created the {plane}" + (f" · {name}" if name else "")
        if et == "thread.closed":
            return f"{who} closed the thread"
        if et == "doc.assigned":
            return f"{who} assigned the doc to {p.get('owner')}"
        changed = p.get("changed")
        if et.endswith(".updated") and isinstance(changed, (list, dict)) and changed:
            fields = ", ".join(str(field) for field in changed)
            if "status" not in changed:
                return f"{who} updated the {plane} ({fields})"
        if st:
            verb = {"resolved": "resolved", "closed": "closed", "open": "reopened",
                    "done": "marked done", "cancelled": "cancelled"}.get(st, f"set status {st}")
            return f"{who} {verb} the {plane}"
        if p.get("assignee"):
            tgt = "you" if p["assignee"] == IDENTITY else p["assignee"]
            return f"{who} assigned it to {tgt}" + (f" · priority {p['priority']}" if p.get("priority") else "")
        if et.endswith(".mentioned"):
            return f"{who} mentioned you"
        if et == "doc.revised":
            return f"{who} revised the doc (rev {p.get('rev')})" + (f"; author's note: {p.get('note')}" if p.get("note") else "")
        if et == "doc.linked":
            return f"{who} linked the doc" + (f" to {p.get('target')}" if p.get("target") else "")
        if p.get("priority"):
            return f"{who} set priority {p['priority']}"
        return f"{who}: {et}"
    def _message(r, on=""):
        p = r.get("payload") or {}
        when = str(r.get("created_at") or "")[11:16]
        b = str(p.get("body") or p.get("detail") or "").strip()
        if str(r.get("event_type") or "") == "thread.closed":
            return f"{r.get('actor') or 'someone'} closed the thread"
        on = f" on {on.replace(' ', ' #', 1)}" if on else ""
        mark = " mentioned you" if str(r.get("event_type") or "").endswith(".mentioned") else ""   # 2.10.0, as the rail
        author = r.get('actor') or '?'
        if p.get('actor_home_thread_id') is not None:
            author += f"@{p['actor_home_thread_id']}"
        if p.get('actor_session'):
            author += f" [{p['actor_session']}]"
        head = f"{author} ({when} UTC){on}{mark}:" if b else _sentence(r) + on
        lines = [head] + ([b] if b else [])
        if not b and r.get("read_object"):
            title = str(p.get("title") or p.get("summary") or "")[:240]
            lines.append((f"{title} — " if title else "") + r["read_object"])
        # `thread_tail` on a mention row is for a process that has not seen the
        # thread; this one has. 2.2.2 quoted it whole, 2.9.1 leaves it out.
        return "\n".join(lines)
    _held_cache = {}
    def _held(key):
        """This conversation's own binding row on the thread, if any — the
        footer's `thread-id`, and whether this is a thread I hold or one that
        just opened toward me."""
        if key not in _held_cache:
            _held_cache[key] = None
            plane, _, oid = key.partition("-")
            if oid.isdigit():
                try:
                    q = f"thread_id={oid}" if plane == "thread" else f"thread={plane}:{oid}"
                    req = urllib.request.Request(f"{API}/wake/threads?{q}",
                                                 headers={"Authorization": f"Bearer {tok}", "X-Session-Id": CONV})
                    for t in json.load(urllib.request.urlopen(req, timeout=15)).get("threads") or []:
                        if t.get("identity") == IDENTITY and t.get("state") in ("open", "closing"):
                            _held_cache[key] = t
                except Exception:
                    pass
        return _held_cache[key]
    def _emit(text, key):
        """The whole message, always. A harness that surfaces stdout as events
        (Claude Code's Monitor) caps a line at ~500 chars and an event at a few
        thousand, and marks the cut "...(truncated)" — a reader that acts on a
        cut message is how the fleet's worst spiral started (2.1.3). So: every
        line is wrapped under the line cap, the message goes out in pieces
        under the event cap with a pause between them so each is its own
        event, and the full text is also written to ~/.samcloud/feed/<thread>.md
        for a re-read that needs nothing from the plane."""
        import textwrap
        try:
            fd = os.path.expanduser("~/.samcloud/feed"); os.makedirs(fd, exist_ok=True)
            with open(os.path.join(fd, f"{key}.md"), "a") as f:
                f.write(text + "\n--\n")
        except Exception:
            pass
        if SINK == "jsonl":
            # 2.8.0 — a channel (samcloud-channel) reads one JSON object per
            # message: no line cap to wrap under, no event cap to split at.
            print(json.dumps({"key": key, "text": text}), flush=True)
            return
        lines = []
        for ln in text.split("\n"):
            if len(ln) <= 400:
                lines.append(ln)
            else:
                lines.extend(textwrap.wrap(ln, 400, break_long_words=False, break_on_hyphens=False) or [ln])
        piece, n = [], 0
        for ln in lines:
            if n + len(ln) + 1 > 2400 and piece:
                print("\n".join(piece) + "\n(continues)", flush=True); time.sleep(0.4)
                piece, n = [], 0
            piece.append(ln); n += len(ln) + 1
        print("\n".join(piece), flush=True)
        print("--", flush=True)
    groups = {}
    for r in sorted(new, key=lambda r: r.get("id") or 0):
        groups.setdefault(_key(r), []).append(r)
    for key, grp in groups.items():
        ref = _ref(key)
        newest = max(grp, key=lambda r: r.get("id") or 0)
        payload = newest.get("payload") or {}
        title = str(payload.get("summary") or payload.get("title") or "").strip()
        root = str(_thread(key).get("root") or "").replace(":", " ") if key.startswith("thread-") else ""
        multi = len({_obj(r) for r in grp if _obj(r)}) > 1 or any(_obj(r) and _obj(r) != root for r in grp if root)
        body = "\n\n".join(_message(r, _obj(r) if multi else "") for r in grp)
        row = _held(key)
        if row is None:
            latest = _obj(newest)
            head = f"Shared conversation — {ref}" + (f" · latest on {latest}: {title}" if title else "") + "\n"
        elif len(grp) > 1:
            head = f"{len(grp)} messages on {ref}:\n"
        else:
            head = ""
        if newest.get("siblings_text"):
            head += newest["siblings_text"] + "\n"
        if newest.get("pending_inbox_text"):
            head += newest["pending_inbox_text"] + "\n"
        where = _obj(newest) or (root if root else "")
        if not where and not key.startswith("thread-"):
            plane, _, oid = key.partition("-")
            where = f"{plane} {oid}" if oid.isdigit() else ""
        footer = f"— samcloud · {ref}" + (f" · sc-say {where} to reply" if where else "")   # the helper bar (doc 358 §8)
        _emit(f"{head}\n{body}\n\n{footer}".strip("\n"), key)
    if new:
        # 2.7.0 — no current-thread pointer. It named the thread of the newest
        # message shown so `sc-new` would pour a create into it; one file per
        # seat, shared by every session there and outliving them (a 6-day-old
        # pointer was steering a new session's creates into an unrelated
        # thread, 2026-10-06). The plane owns this now: what a conversation
        # makes joins the conversation's own thread (#886).
        # The receipt, after printing: these rows reached a live conversation,
        # so the plane will never offer them again — to this surface or any
        # other. Printed-then-died consumes a row; that is the same trade the
        # rail makes for a holder's fifo, and it is why nothing here needs a
        # human to advance a cursor.
        if registered:
            _post("/notifications/delivered",
                  {"ids": [int(r["id"]) for r in new if "id" in r],
                   "conversation_id": CONV})
        seen |= {str(r["id"]) for r in new}
        seen_p.write_text(" ".join(sorted(seen, key=int)[-500:]))
    # No sleep. The loop blocks on the doorbell, not on a clock (2.6.0).
