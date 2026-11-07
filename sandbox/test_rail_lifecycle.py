#!/usr/bin/env python3
"""Rail v3 holder lifecycle — the verb the rail picks for every holder state.

Drives the REAL registry/helpers/samcloud-rail.py with:
  - a stub `claude` on PATH that speaks stream-json (resident holder) or
    answers once and exits (`-p --resume`, the one-shot), and can be told per
    turn to answer ok / usage-limit pause / not-logged-in / die, and to exit
    when it is told to wrap up;
  - a stub registry (local HTTP) serving exactly the routes the rail calls:
    inbox, delivery receipts, acks, seen-leases, thread bindings, object reads,
    claim release, whoami. The inbox the rail sees is the harness's rows minus
    what the stub has recorded as acked or delivered-to-an-open-conversation —
    the same predicate `GET /notifications?undelivered=true` applies on the
    plane (unit-tested in registry/test_registry.py).

Asserts, after every dispatch: the verb, whether a NEW process was spawned,
the generation count, what the plane was told (bindings, receipts, acks), and
the shape of what the holder was handed. No network, no real claude.

Why (#623 → doc 174): 3.8.0 read a usage-limit pause as death and RESUMEd on
every heartbeat — 1155 times in a day on one seat — because nothing exercised
the dispatch state machine. Step 3 is that regression. Rail 3.9 adds the
lifecycle (delivery rule, delivery-is-receipt, close/idle/one-shot); each rule has
a step here that fails on 3.8.1.

Run from repo root: python3 sandbox/test_rail_lifecycle.py
RAIL_UNDER_TEST=<file> points it at another rail build (e.g. a git show
extract) to prove a step fails on the version it guards against.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

REPO = Path(__file__).resolve().parent.parent


def lease_request_fields():
    """The real `LeaseRequest` field names, read out of `registry/main.py` with `ast`.

    The first version of the lease-validating stub below used a hand-copied set and
    I named the cost in the PR: it drifts, so a new field on the model makes a
    legitimate body 422 inside the tests. One seat's answer (samcloud#31,
    same mechanism in `test_restart_when_idle.py`) is the right one — we do not need
    the model, only its field names, and those are readable from the source without
    importing anything, standing up the app, or a writable GATEWAY_KEY in a test
    built to have no registry.

    It FAILS rather than falling back if the class cannot be found. A stub that
    quietly stops checking is the bug this exists to prevent (#843), and a silent
    fallback would be that bug wearing the fix as a hat.

    There are two copies of this, one per sandbox test, because sandbox tests are
    standalone by design — no cross-imports anywhere in this directory. If a third
    one appears, that is the moment for a shared module rather than now.
    """
    import ast
    src = (REPO / "registry/main.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef) and node.name == "LeaseRequest":
            return {t.target.id for t in node.body
                    if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)}
    raise AssertionError("LeaseRequest not found in registry/main.py — the stub "
                         "cannot validate a body against a model it cannot read")


LEASE_FIELDS = lease_request_fields()
RAIL = Path(os.environ.get("RAIL_UNDER_TEST") or REPO / "registry/helpers/samcloud-rail.py")

STUB = r'''#!/usr/bin/env python3
# stub claude. Stream mode: a resident holder reading stream-json user turns.
# One-shot mode (`-p --resume` without --input-format): read stdin, answer once, exit.
import json, os, sys, time
args = sys.argv[1:]
sid = None; flag = None
for i, a in enumerate(args):
    if a in ("--session-id", "--resume") and i + 1 < len(args):
        sid, flag = args[i + 1], a
T = os.environ["STUB_DIR"]
stream = "--input-format" in args
with open(os.path.join(T, "spawns.log"), "a") as f:
    f.write(json.dumps({"pid": os.getpid(), "flag": flag, "sid": sid,
                        "mode": "stream" if stream else "oneshot"}) + "\n")
proj = os.path.join(os.path.expanduser("~"), ".claude", "projects", "stub")
os.makedirs(proj, exist_ok=True)
open(os.path.join(proj, sid + ".jsonl"), "a").close()
def mode():
    try: return open(os.path.join(T, "mode")).read().strip()
    except OSError: return "ok"
def result(kind):
    if kind == "limit-past":
        import datetime as _dt
        from zoneinfo import ZoneInfo as _Z
        t = _dt.datetime.now(_Z("Europe/Berlin")) - _dt.timedelta(hours=1)
        hh = t.strftime("%I%p").lstrip("0").lower()
        return {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
                "duration_ms": 0, "total_cost_usd": 0, "api_error_status": 429,
                "result": f"You've hit your session limit · resets {hh} (Europe/Berlin)"}
    if kind == "limit":
        return {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
                "duration_ms": 1345, "total_cost_usd": 0,
                "result": "You've hit your session limit · resets 11am (Europe/Berlin)"}
    if kind == "notlogged":
        return {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                "duration_ms": 900, "total_cost_usd": 0, "result": "Not logged in · Please run /login"}
    return {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
            "duration_ms": 2000, "total_cost_usd": 0.01, "result": "ok"}
if not stream:
    text = sys.stdin.read()
    with open(os.path.join(T, "oneshots.log"), "a") as f:
        f.write(json.dumps({"sid": sid, "chars": len(text), "head": text[:60]}) + "\n")
    print(json.dumps(result("ok"))); sys.exit(0 if mode() != "die" else 1)
sys.stdout.write(json.dumps({"type": "system", "subtype": "init"}) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    if not line.strip():
        continue
    try:
        text = json.loads(line)["message"]["content"]
    except Exception:
        text = ""
    m = mode()
    if m == "die":
        sys.exit(1)
    if ("THREAD CLOSE" in str(text) or "kill -TERM $PPID" in str(text)) and m != "ignore-close":
        sys.stdout.write(json.dumps(result("ok")) + "\n"); sys.stdout.flush()
        sys.exit(0)                              # a good holder wraps up and leaves
    sys.stdout.write(json.dumps({"type": "assistant", "message": {"content": "..."}}) + "\n")
    sys.stdout.write(json.dumps(result(m)) + "\n"); sys.stdout.flush()
'''

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  \033[32m✓\033[0m {label}")
    else:
        failed += 1
        print(f"  \033[31m✗\033[0m {label}  {str(detail)[:220]}")


class StubRegistry:
    """The plane, as far as the rail can tell."""

    def __init__(self):
        self.rows = []                 # inbox rows the harness has "posted"
        self.acked = set()
        self.delivered = {}            # row id -> conversation id
        self.seen = set()
        self.threads = {}              # (kind, object_id) -> row
        self.tickets = {}              # id -> {"status", "assignee"}
        self.tasks = {}                # id -> {"status", "assignee"}      (Phase C)
        self.docs = {}                 # id -> {"source"}                  (Phase C: author owns)
        self.peers = []                # other identities' holder rows, served on ?thread= (3.9.12)
        self.surfaces = []             # my identity's interactive surfaces (0.9.1: each with `connected`)
        self.work_threads = {}         # id -> {"root": "ticket:706", "state", "title", "members": {kind: [ids]}} (doc 239)
        self.calls = []                # (method, path, body)
        self.comments = []             # what an adapter turn posted on a thread
        # Leases, validated the way the PLANE validates them. LeaseRequest is a
        # StrictModel (extra="forbid"), so a stub that accepts any body is how
        # #843 sat unnoticed: the adapter sent `duration_seconds`/`purpose` for
        # months, the real plane answered 422 on every turn, and nothing in this
        # suite could tell. A stub is only a test if it is as strict as the thing
        # it stands in for.
        self.leases = {}               # id -> {"status", "resource", "body"}
        self.lease_rejects = []        # bodies the plane would have refused 422
        self.identity = "tester"
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}") if n else {}

            def do_GET(self):
                u = urlparse(self.path); q = parse_qs(u.query); p = u.path
                outer.calls.append(("GET", p, dict(q)))
                if p == "/api/v1/auth/verify":
                    return self._send(200, {"username": None, "role": None})   # admin-class bearer (F4)
                if p == "/api/v1/users/me/briefing":
                    return self._send(200, {"identity": outer.identity})
                if p == "/api/v1/notifications":
                    rows = [r for r in outer.rows if r["id"] not in outer.acked]
                    if q.get("undelivered") == ["true"]:
                        rows = [r for r in rows if r["id"] not in outer.delivered]   # delivered is final
                    return self._send(200, {"notifications": rows})
                if p == "/api/v1/wake/surfaces":
                    return self._send(200, {"scope": "self", "surfaces": [dict(x) for x in outer.surfaces]})
                if p == "/api/v1/wake/threads":
                    st = q.get("state", [None])[0]
                    rows = [dict(t) for t in outer.threads.values() if not st or t["state"] == st]
                    if q.get("thread_id"):
                        wt = int(q["thread_id"][0])
                        rows = [t for t in rows if t.get("thread_id") == wt]
                        rows += [dict(t) for t in outer.peers if t.get("thread_id") == wt]
                        return self._send(200, {"scope": f"thread_id:{wt}", "thread_id": wt, "threads": rows})
                    if q.get("thread"):
                        kind, _, oid = q["thread"][0].partition(":")
                        rows = [t for t in rows if t["kind"] == kind and t["object_id"] == int(oid)]
                        rows += [dict(t) for t in outer.peers if t["kind"] == kind and t["object_id"] == int(oid)]
                        return self._send(200, {"scope": f"thread:{kind}:{oid}", "threads": rows})
                    return self._send(200, {"scope": "self", "threads": rows})
                if p.startswith("/api/v1/threads/of/"):
                    kind, oid = p.rsplit("/", 2)[1:]
                    if int(oid) in getattr(outer, "refuse_threads_of", set()):
                        return self._send(403, {"detail": "not in your scope"})
                    wt = outer.thread_of(kind, int(oid))
                    return self._send(200, outer.thread_public(wt)) if wt else self._send(404, {"detail": "no thread"})
                if p.startswith("/api/v1/threads/"):
                    wt = int(p.rsplit("/", 1)[1])
                    return self._send(200, outer.thread_public(wt)) if wt in outer.work_threads else self._send(404, {"detail": "no thread"})
                if p.startswith("/api/v1/tickets/"):
                    tid = int(p.rsplit("/", 1)[1])
                    t = outer.tickets.get(tid, {"status": "open", "assignee": None})
                    return self._send(200, {"id": tid, **t})
                if p.startswith("/api/v1/tasks/"):
                    tid = int(p.rsplit("/", 1)[1])
                    t = outer.tasks.get(tid, {"status": "open", "assignee": None})
                    return self._send(200, {"id": tid, **t})
                if p.startswith("/api/v1/docs/"):
                    did = int(p.rsplit("/", 1)[1])
                    d = outer.docs.get(did, {"source": None, "status": "published"})
                    return self._send(200, {"id": did, **d})
                return self._send(404, {"detail": "stub: no route " + p})

            def do_POST(self):
                p = urlparse(self.path).path; b = self._body()
                outer.calls.append(("POST", p, b))
                if p.startswith("/api/v1/") and p.endswith("/comments"):
                    parts = p.split("/")          # /api/v1/<tickets|docs|tasks>/<id>/comments
                    outer.comments.append({
                        "kind": parts[3], "id": int(parts[4]),
                        "body": b.get("body", ""),
                        "session": self.headers.get("X-Session-Id", "")})
                    return self._send(201, {"comment_id": 900 + len(outer.comments)})
                if p == "/api/v1/notifications/delivered":
                    for i in b.get("ids", []):
                        outer.delivered[int(i)] = b.get("conversation_id")
                    return self._send(200, {"status": "delivered", "delivered": len(b.get("ids", []))})
                if p == "/api/v1/notifications/ack":
                    outer.acked.update(int(i) for i in b.get("ids", []))
                    return self._send(200, {"status": "acked"})
                if p == "/api/v1/notifications/seen":
                    outer.seen.update(int(i) for i in b.get("ids", []))
                    return self._send(200, {"status": "seen"})
                if p == "/api/v1/wake/threads":
                    wt = b.get("thread_id")
                    if wt is not None and int(wt) in outer.work_threads:      # bound by thread: keyed on its first member
                        rk, _, rid = outer.work_threads[int(wt)]["root"].partition(":")
                        k = (rk, int(rid))
                    else:
                        k = (b["kind"], int(b["object_id"]))
                        wt = outer.thread_of(*k) if outer.work_threads else None
                    row = outer.threads.get(k, {"id": len(outer.threads) + 1, "kind": k[0],
                                                "object_id": k[1], "identity": outer.identity})
                    # The plane's no-twin refusal (0.12.18), mirrored: a rail
                    # holder may not take a thread a live conversation of the
                    # same identity holds. The stub is the only place the rail's
                    # side of this is reachable by the harness.
                    live = {x.get("conversation") or x.get("conversation_id")
                            for x in outer.surfaces if not x.get("ended")}
                    if (row.get("holder") == "conversation"
                            and row.get("state") in ("open", "closing")
                            and row.get("conversation_id") != b["conversation_id"]
                            and row.get("conversation_id") in live
                            and b.get("holder") in ("stream", "tmux", "oneshot", "adapter")):
                        return self._send(409, {"detail": (
                            f"{outer.identity} already holds {k[0]} {k[1]} in another "
                            f"conversation (a registered surface)")})
                    row.update({"conversation_id": b["conversation_id"], "state": b.get("state", "open"),
                                "holder": b.get("holder"), "pid": b.get("pid"),
                                "rail_version": b.get("rail_version"), "thread_id": int(wt) if wt else None})
                    outer.threads[k] = row
                    return self._send(200, {"thread": row})
                if p == "/api/v1/wake/surfaces":
                    conv = b.get("conversation_id")
                    for x in outer.surfaces:
                        if (x.get("conversation") or x.get("conversation_id")) == conv:
                            x["ended"] = bool(b.get("ended"))
                    if b.get("ended"):      # the plane closes them itself (0.12.18)
                        for row in outer.threads.values():
                            if row.get("holder") == "conversation" and \
                                    row.get("conversation_id") == conv:
                                row["state"] = "closed"
                    return self._send(200, {"surface": {"conversation_id": conv}})
                if p == "/api/v1/wake/session":
                    return self._send(200, {"status": "ok"})
                if p.startswith("/api/v1/resources/") and p.endswith("/leases"):
                    # LeaseRequest's own fields, read from the model rather than
                    # copied beside it, so this cannot drift from what the plane
                    # accepts (samcloud#31's mechanism).
                    extra = sorted(set(b) - LEASE_FIELDS)
                    if extra:
                        outer.lease_rejects.append(b)
                        return self._send(422, {"detail": [
                            {"type": "extra_forbidden", "loc": ["body", k],
                             "msg": "Extra inputs are not permitted"} for k in extra]})
                    res = p[len("/api/v1/resources/"):-len("/leases")]
                    lid = f"lease_{len(outer.leases) + 1:04d}"
                    outer.leases[lid] = {"status": "active", "resource": res, "body": b}
                    return self._send(201, {"lease_id": lid, "id": lid,
                                            "resource_id": res, "status": "active"})
                return self._send(404, {"detail": "stub: no route " + p})

            def do_PATCH(self):
                p = urlparse(self.path).path; b = self._body()
                outer.calls.append(("PATCH", p, b))
                if p.startswith("/api/v1/wake/threads/"):
                    tid = int(p.rsplit("/", 1)[1])
                    for row in outer.threads.values():
                        if row["id"] == tid:
                            row.update({k: v for k, v in b.items() if k in ("state", "pid")})
                            return self._send(200, {"thread": row})
                    return self._send(404, {})
                return self._send(404, {})

            def do_DELETE(self):
                p = urlparse(self.path).path
                outer.calls.append(("DELETE", p, {}))
                if p.endswith("/claim"):
                    return self._send(200, {"status": "released", "released": 0})
                # The plane has exactly one release route (main.py:10978). A
                # resource-scoped path is swallowed by {resource_id:path} and
                # answered "Resource …/leases/<id> not found", which is the second
                # half of #843 — so this stub must refuse it too.
                if p.startswith("/api/v1/leases/"):
                    lid = p.rsplit("/", 1)[1]
                    if lid in outer.leases:
                        outer.leases[lid]["status"] = "released"
                        return self._send(200, {"status": "released", "lease_id": lid})
                    return self._send(404, {"detail": f"Lease {lid} not found"})
                if "/leases/" in p:
                    return self._send(404, {"detail": "Resource " + p.split(
                        "/api/v1/resources/")[-1] + " not found"})
                return self._send(404, {})

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    # ── doc 239: the universal thread, as the plane models it ──
    def declare_thread(self, wt: int, root: str, members: dict = None, title: str = "", state: str = "open"):
        """A universal thread with its members: {"ticket": [706, 707], "task": [648], "doc": [237]}."""
        rk, _, rid = root.partition(":")
        m = {"ticket": [], "task": [], "doc": []}
        m[rk].append(int(rid))
        for k, ids in (members or {}).items():
            m[k] += [int(i) for i in ids if int(i) not in m[k]]
        self.work_threads[wt] = {"root": root, "state": state, "title": title, "members": m}
        return wt

    def thread_of(self, kind: str, oid: int):
        for wt, t in self.work_threads.items():
            if oid in t["members"].get(kind, []):
                return wt
        if not self.work_threads:
            return None                      # a pre-v19 plane: no threads at all
        wt = max(max(self.work_threads) + 1, 100000)   # lazily: the object starts its own (ids far from the declared ones)
        self.declare_thread(wt, f"{kind}:{oid}")
        return wt

    def thread_public(self, wt: int) -> dict:
        t = self.work_threads[wt]
        terminal = {"ticket": ("resolved", "closed"), "task": ("done", "cancelled")}
        open_members = False
        for kind, ids in t["members"].items():
            if kind == "doc":
                continue
            store = self.tickets if kind == "ticket" else self.tasks
            for i in ids:
                if store.get(i, {"status": "open"}).get("status", "open") not in terminal[kind]:
                    open_members = True
        owners = set()
        for i in t["members"]["ticket"]:
            a = self.tickets.get(i, {}).get("assignee")
            if a: owners.add(a)
        for i in t["members"]["task"]:
            a = self.tasks.get(i, {}).get("assignee")
            if a: owners.add(a)
        for i in t["members"]["doc"]:
            a = self.docs.get(i, {}).get("source")
            if a: owners.add(a)
        return {"id": wt, "title": t["title"], "state": t["state"], "root": t["root"],
                "open_members": open_members, "owners": sorted(owners),
                "members": {k + "s": [{"id": i} for i in ids] for k, ids in t["members"].items()},
                "bindings": [dict(b) for b in self.threads.values() if b.get("thread_id") == wt]}

    @property
    def api(self):
        return f"http://127.0.0.1:{self.port}/api/v1"

    def posts(self, path):
        return [b for m, p, b in self.calls if m == "POST" and p == path]

    def stop(self):
        self.srv.shutdown()


class Rig:
    def __init__(self):
        self.t = Path(tempfile.mkdtemp(prefix="rail-lifecycle-"))
        self.home = self.t / "home"
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".samcloud").mkdir()
        (self.home / ".samcloud/token").write_text("stub-token\n")
        stub = self.home / ".local/bin/claude"
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.state = self.t / "state"
        self.inbox = self.t / "inbox.json"
        self.log = self.t / "wake.log"
        self.spawns = self.t / "spawns.log"
        (self.t / "mode").write_text("ok")
        self.reg = StubRegistry()
        self.next_id = 100

    def env(self, **extra):
        e = {k: v for k, v in os.environ.items()
             if not k.startswith(("CLAUDE", "RAIL_", "SAMCLOUD"))}
        e.update({
            "HOME": str(self.home), "STUB_DIR": str(self.t),
            "SAMCLOUD_WAKE_CONF": "/dev/null",
            "API": self.reg.api,
            "TOKEN_PATH": str(self.home / ".samcloud/token"),
            "RAIL_STATE_DIR": str(self.state), "LOG": str(self.log),
            "RAIL_HOLDER": "stream", "RAIL_CWD": str(self.home),
            "RAIL_FAKE_INBOX": str(self.inbox), "RAIL_SETTLE_SEC": "0",
            "RAIL_ONESHOT_ATTEMPTS": "8",             # the rig one-shots ticket-900 more than a seat would
            "MODEL": "stub", "PERMISSION_ARGS": "",
        })
        e.update(extra)
        return e

    def mode(self, m):
        (self.t / "mode").write_text(m)

    def row(self, ticket, event="ticket.mentioned", body="hello", actor="alice", thread_id=None, **payload):
        self.next_id += 1
        r = {"id": self.next_id, "event_type": event, "ticket_id": ticket,
             "actor": actor, "created_at": "2026-09-06T00:00:00+00:00",
             "payload": {"summary": f"t{ticket}", "body": body, **payload}}
        if thread_id is None and self.reg.work_threads:          # the plane stamps every row (doc 239)
            thread_id = self.reg.thread_of("ticket", ticket)
        if thread_id is not None:
            r["thread_id"] = thread_id
        self.reg.rows.append(r)
        return r

    def thread_row(self, thread_id, event="thread.closed", actor="alice", **payload):
        """A row that sits on the thread itself, on no member (thread.closed)."""
        self.next_id += 1
        r = {"id": self.next_id, "event_type": event, "actor": actor, "thread_id": thread_id,
             "created_at": "2026-09-06T00:00:00+00:00", "payload": {"summary": "thread closed", **payload}}
        self.reg.rows.append(r)
        return r

    def row_on(self, kind, oid, event, body="hello", actor="alice", **payload):
        """A row on a task or doc (Phase C) — same shape the registry writes,
        keyed by `task_id` / `doc_id`; a row with neither is a lobby row."""
        self.next_id += 1
        r = {"id": self.next_id, "event_type": event, "actor": actor,
             "created_at": "2026-09-06T00:00:00+00:00",
             "payload": {"title": f"{kind}{oid}", "body": body, **payload}}
        if kind in ("task", "doc"):
            r[f"{kind}_id"] = oid
            if self.reg.work_threads:
                r["thread_id"] = self.reg.thread_of(kind, oid)
        self.reg.rows.append(r)
        return r

    def dispatch(self, rows=None, **extra):
        """Hand the rail what the plane would: unacked rows not delivered to an
        open conversation (the ?undelivered=true predicate), plus `rows`."""
        visible = [r for r in self.reg.rows if r["id"] not in self.reg.acked
                   and r["id"] not in self.reg.delivered]          # delivered is final
        self.inbox.write_text(json.dumps({"notifications": visible}))
        mark = self.log.stat().st_size if self.log.exists() else 0
        r = subprocess.run([sys.executable, str(RAIL)], env=self.env(**extra),
                           input=b'{"reason":"test"}', capture_output=True, timeout=60)
        # The launcher reports its child PID before Python has necessarily
        # reached the stub's first log write. Wait for that observable startup
        # event, not a fixed assumption about host load. Keep all spawn-count,
        # argv and PID assertions below; a missing/dead stub still fails them.
        registry = self.state / "threads.json"
        entries = json.loads(registry.read_text()).values() if registry.exists() else ()
        pending = {entry['pid'] for entry in entries if entry.get('holder') == 'stream'
                   and entry.get('pid') and self.alive(entry['pid'])}
        deadline = time.monotonic() + 5
        while pending - {spawn['pid'] for spawn in self.spawns_()}:
            if time.monotonic() >= deadline or not any(self.alive(pid) for pid in pending):
                break
            time.sleep(.02)
        time.sleep(0.7)          # let the stub answer and flush its result
        # slice in BYTES: the mark is st_size, and the log carries multibyte
        # characters (— …), so a str slice drifts and chops the new text
        new = self.log.read_bytes()[mark:].decode("utf-8", "replace") if self.log.exists() else ""
        return r.returncode, new

    def verb(self, logtext, key):
        """The dispatch verb for `key` in this pass — the LAST verb line, since
        the sweep's `closed (…)` note precedes the dispatch line."""
        found = None
        for line in logtext.splitlines():
            if f"rail3: {key}: " in line:
                v = line.split(f"rail3: {key}: ", 1)[1].split(" ", 1)[0]
                if v in ("wake", "resume", "continue", "idle", "closing", "oneshot", "closed"):
                    found = v
        return found

    def spawns_(self):
        return [json.loads(l) for l in self.spawns.read_text().splitlines()] if self.spawns.exists() else []

    def spawn_count(self):
        return len(self.spawns_())

    def last_spawn(self):
        s = self.spawns_()
        return s[-1] if s else {}

    def binding(self, key):
        p = self.state / "threads.json"
        return json.loads(p.read_text()).get(key, {}) if p.exists() else {}

    def generations(self, key):
        return len(list((self.state / "threads" / key).glob("out-*.jsonl")))

    def deliveries(self, key):
        p = self.state / "threads" / key / "in.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def gone(self, pid, within=3.0):
        """True once the pid has exited, polling up to `within` seconds."""
        end = time.time() + within
        while time.time() < end:
            if not self.alive(pid):
                return True
            time.sleep(0.1)
        return not self.alive(pid)

    def cleanup(self):
        for s in self.spawns_():
            try:
                os.kill(s["pid"], signal.SIGKILL)
            except Exception:
                pass
        self.reg.stop()
        shutil.rmtree(self.t, ignore_errors=True)


def main():
    rig = Rig()
    K = "ticket-900"
    try:
        print("Rail v3 holder lifecycle (stub claude + stub registry)")

        print("  [1] birth — a mention on an unbound thread")
        r900 = rig.row(900)
        rc, out = rig.dispatch()
        check("rc 0", rc == 0, out[-300:])
        check("verb: wake", rig.verb(out, K) == "wake", rig.verb(out, K))
        # Dispatch returns before the asynchronous stub has necessarily logged birth.
        deadline = time.monotonic() + 5
        while not rig.spawn_count() and time.monotonic() < deadline:
            time.sleep(0.05)
        check("one holder spawned with --session-id (stream)",
              rig.spawn_count() == 1 and rig.last_spawn()["flag"] == "--session-id"
              and rig.last_spawn()["mode"] == "stream", str(rig.last_spawn()))
        p1 = rig.binding(K).get("pid")
        check("binding pid is the live stub", p1 and rig.alive(p1), str(p1))
        check("1 generation file", rig.generations(K) == 1, str(rig.generations(K)))
        th = rig.reg.threads.get(("ticket", 900), {})
        check("binding posted to the plane: open, stream, this pid, this sid",
              th.get("state") == "open" and th.get("holder") == "stream"
              and th.get("pid") == p1 and th.get("conversation_id") == rig.binding(K).get("sid"), str(th))
        check("delivery receipt posted for the row",
              rig.reg.delivered.get(r900["id"]) == rig.binding(K).get("sid"), str(rig.reg.delivered))
        text = rig.deliveries(K)[-1]["text"]
        check("mention birth names the latest object, speaker and reply footer",
              text.startswith("You are tester") and "alice mentioned you on ticket #900 · latest on ticket #900: t900" in text and "alice (00:00 UTC) mentioned you:" in text
              and "Replies to you arrive here" in text
              and "— samcloud · ticket #900" in text and "sc-say ticket 900 to reply" in text, text[:220])
        check("no mechanism talk: no 'untrusted', no 'ack', no header-file lecture",
              "untrusted" not in text.lower() and "ack" not in text.lower() and "SAMCLOUD_AUTH_HDR" not in text, text[:200])

        print("  [2] continue — a reply reaches the SAME process")
        rig.row(900, "ticket.commented", "reply")
        rc, out = rig.dispatch()
        check("verb: continue", rig.verb(out, K) == "continue", rig.verb(out, K))
        check("no new spawn, same pid", rig.spawn_count() == 1 and rig.binding(K).get("pid") == p1)
        check("footer: thread ref + reply line, no holding census (3.9.22)",
              "— samcloud · ticket #900" in rig.deliveries(K)[-1]["text"]
              and "sc-say ticket 900 to reply" in rig.deliveries(K)[-1]["text"]
              and "holding" not in rig.deliveries(K)[-1]["text"],
              rig.deliveries(K)[-1]["text"][-200:])

        print("  [2b] delivery is receipt — the same unacked rows are NOT re-offered")
        n_before = len(rig.deliveries(K))
        rc, out = rig.dispatch()                         # nothing new on the plane
        check("rail found nothing to deliver", rig.verb(out, K) is None and "dispatching" not in out, out[-200:])
        check("no re-delivery into the live holder", len(rig.deliveries(K)) == n_before)

        print("  [3] usage-limit pause — is_error:true, cost 0 — is NOT death (#623)")
        rig.mode("limit")
        rig.row(900, "ticket.commented", "paused turn")
        rc, out = rig.dispatch()
        check("row delivered as continue", rig.verb(out, K) == "continue", rig.verb(out, K))
        rig.row(900, "ticket.commented", "next row")
        rc, out = rig.dispatch()
        check("REGRESSION: next row is continue, not resume", rig.verb(out, K) == "continue", rig.verb(out, K))
        check("no dead line", "dead" not in out, out.strip()[-200:])
        check("no new spawn, same pid, 1 generation",
              rig.spawn_count() == 1 and rig.binding(K).get("pid") == p1 and rig.generations(K) == 1)
        rig.mode("ok")

        print("  [4] delivery rule — what lands without a turn")
        rig.reg.tickets[901] = {"status": "open", "assignee": "someone-else"}
        q = rig.row(901, "ticket.commented", "chatter", actor="peer")
        rc, out = rig.dispatch()
        check("comment on an unheld thread I don't own: not delivered", "not delivered" in out and "ticket-901" in out, out[-200:])
        check("no spawn, no binding for 901", rig.spawn_count() == 1 and not rig.binding("ticket-901"))
        check("row left unread, not delivered", q["id"] not in rig.reg.delivered and q["id"] not in rig.reg.acked)
        rig.reg.tickets[902] = {"status": "open", "assignee": "tester"}
        rig.row(902, "ticket.commented", "on my ticket", actor="alice")
        rc, out = rig.dispatch()
        check("comment on a ticket I OWN with no instance: wake (via briefing whoami — verify has no username)",
              rig.verb(out, "ticket-902") == "wake", out[-300:])
        rig.row(901, "ticket.updated", "", actor="alice", assignee="tester", status="open")
        rc, out = rig.dispatch()
        check("assignment to me opens a thread: wake", rig.verb(out, "ticket-901") == "wake", rig.verb(out, "ticket-901"))
        check("the new thread's footer names its own thread and reply line only",
              "ticket #901" in rig.deliveries("ticket-901")[-1]["text"] and "holding" not in rig.deliveries("ticket-901")[-1]["text"],
              rig.deliveries("ticket-901")[-1]["text"][-220:])

        print("  [4b] a capability probe is acked by the rail itself, no turn")
        probe = rig.row(None, "wake.probe", "ack it and exit", actor="registry")
        probe["ticket_id"] = None
        rc, out = rig.dispatch()
        check("probe acked without a turn", probe["id"] in rig.reg.acked and "probe acked" in out, out[-200:])
        check("no spawn for the probe", rig.spawn_count() == 3, str(rig.spawn_count()))

        print("  [4c] a reply on a ticket this holder FILED reaches this holder (plane binding, doc 174)")
        sid900 = rig.binding(K).get("sid")
        rig.reg.threads[("ticket", 903)] = {"id": 77, "kind": "ticket", "object_id": 903, "identity": "tester",
                                             "conversation_id": sid900, "state": "open", "holder": "conversation"}
        rig.reg.tickets[903] = {"status": "open", "assignee": "peer"}
        n_before = len(rig.deliveries(K))
        r903 = rig.row(903, "ticket.commented", "reply on the child ticket", actor="peer")
        rc, out = rig.dispatch()
        check("verb on ticket-903: continue (routed into ticket-900's holder)", rig.verb(out, "ticket-903") == "continue", out[-300:])
        check("no spawn, no binding dir for 903", rig.spawn_count() == 3 and not rig.binding("ticket-903"))
        d = rig.deliveries(K)
        check("delivered into ticket-900's FIFO, named for #903, reply line says ticket 903",
              len(d) == n_before + 1 and "sc-say ticket 903 to reply" in d[-1]["text"] and "peer (" in d[-1]["text"], d[-1]["text"][-200:] if d else "")
        check("receipt: delivered to ticket-900's conversation", rig.reg.delivered.get(r903["id"]) == sid900)

        print("  [4d] my own identity never rings me — from ANY of its conversations (3.9.20); my holder's own echo does not either")
        import hashlib as _h
        rig.reg.tickets[904] = {"status": "open", "assignee": "tester"}
        n_spawn_4d = rig.spawn_count()
        rig.row(904, "ticket.created", "brief: go do the thing", actor="tester", assignee="tester", actor_session="1nteract")
        rc, out = rig.dispatch()
        check("a self-assigned ticket from an interactive conversation births NOTHING (own writes never ring you; the surface holds it)",
              rig.verb(out, "ticket-904") is None and rig.spawn_count() == n_spawn_4d and "not delivered" in out, out[-300:])
        fp900 = _h.sha256(rig.binding(K)["sid"].encode()).hexdigest()[:8]
        echo = rig.row(900, "ticket.commented", "what my own holder just posted", actor="tester", actor_session=fp900)
        rc, out = rig.dispatch()
        check("my holder's own echo on its thread: not delivered", "not delivered" in out and "ticket-900" in out, out[-300:])
        check("echo row not delivered", echo["id"] not in rig.reg.delivered)

        print("  [5] close — the object resolves → wrap-up delivered → holder exits → closed")
        rig.reg.tickets[900] = {"status": "resolved", "assignee": "tester"}
        rig.mode("ignore-close")                 # a holder mid-work: told to end, keeps working
        rig.row(900, "ticket.updated", "", actor="alice", status="resolved", assignee="tester")
        rc, out = rig.dispatch()
        check("verb: closing", rig.verb(out, K) == "closing", rig.verb(out, K))
        # 3.9.11 (staging #108): a peer's message on a CLOSING thread carries the
        # wrap-up again — otherwise it supersedes the pending exit and the holder
        # sits in `closing` with a live pid until someone nudges it.
        rig.row(900, "ticket.commented", "peer's closing notes", actor="peer")
        rc, out = rig.dispatch()
        check("a message on a closing thread carries the wrap-up again (multi-party close does not stall)",
              rig.verb(out, K) == "continue" and "kill -TERM $PPID" in rig.deliveries(K)[-1]["text"]
              and "alice resolved ticket #900" in rig.deliveries(K)[-1]["text"]
              and rig.deliveries(K)[-1]["text"].count("— samcloud ·") == 1, rig.deliveries(K)[-1]["text"][-300:])
        rig.mode("ok")
        rig.row(900, "ticket.commented", "last word", actor="peer")
        rc, out = rig.dispatch()
        check("one footer per delivery, even with the wrap-up appended (staging #102 finding 4)",
              rig.deliveries(K)[-1]["text"].count("— samcloud ·") == 1, rig.deliveries(K)[-1]["text"][-300:])
        check("wrap-up message delivered (v2: 'alice resolved ticket #900 … kill -TERM $PPID')",
              "alice resolved ticket #900" in rig.deliveries(K)[-1]["text"] and "kill -TERM $PPID" in rig.deliveries(K)[-1]["text"],
              rig.deliveries(K)[-1]["text"][:200])
        # (3.9.10: the stub wraps up at once and its launcher reports the exit,
        # so by now the row may already read `closed` — assert the transition
        # was POSTED, not that it is the current state)
        check("binding → closing on the plane; claim released",
              any(b.get("state") == "closing" and b.get("object_id") == 900
                  for b in rig.reg.posts("/api/v1/wake/threads"))
              and rig.reg.threads[("ticket", 900)]["state"] in ("closing", "closed")
              and any(m == "DELETE" and p.endswith("/tickets/900/claim") for m, p, _ in rig.reg.calls))
        check("holder exited on its own (the stub exits on THREAD CLOSE)", rig.gone(p1), str(p1))
        # 3.9.10: the exit is the event — the launcher PATCHes the row closed
        # with NO dispatch in between (staging #104: 15m28s of `closing` with
        # a dead pid once the heartbeat was gone).
        end = time.time() + 5
        while time.time() < end and rig.reg.threads[("ticket", 900)]["state"] != "closed":
            time.sleep(0.1)
        check("binding → closed on the plane by the holder's own exit, before any further pass",
              rig.reg.threads[("ticket", 900)]["state"] == "closed"
              and any(m == "PATCH" and p.endswith(f"/wake/threads/{rig.reg.threads[('ticket', 900)]['id']}")
                      for m, p, b in rig.reg.calls), str(rig.reg.threads[("ticket", 900)]))
        rig.row(902, "ticket.commented", "keep the pass busy", actor="alice")
        rc, out = rig.dispatch()                          # any pass runs the sweep
        check("sweep: ticket-900 closed (wrapped up)", "ticket-900: closed (wrapped up)" in out, out[-300:])
        check("binding → closed on the plane, local state closed",
              rig.reg.threads[("ticket", 900)]["state"] == "closed" and rig.binding(K).get("state") == "closed")

        print("  [5b] a resolve by an identity that ALSO holds the thread reaches every live holder (3.9.18, prod #606 — the fleet was dried for this)")
        rig.reg.tickets[909] = {"status": "open", "assignee": "tester"}
        rig.row(909, "ticket.mentioned", "@tester hold this with me", actor="peer3")
        rc, out = rig.dispatch()
        p9 = rig.binding("ticket-909").get("pid")
        check("held", rig.verb(out, "ticket-909") == "wake" and p9 and rig.alive(p9), rig.verb(out, "ticket-909"))
        rig.reg.peers.append({"id": 992, "kind": "ticket", "object_id": 909, "identity": "peer3",
                              "holder": "stream", "state": "open", "conversation": "cafebabe"})
        rig.reg.tickets[909] = {"status": "resolved", "assignee": "tester"}
        rig.mode("ignore-close")
        rig.row(909, "ticket.updated", "", actor="peer3", status="resolved", assignee="tester", changed=["status"])
        rc, out = rig.dispatch()
        check("the close signal from a peer who also holds the thread is DELIVERED to the live holder (closing), not dropped as a former holder's row",
              rig.verb(out, "ticket-909") == "closing" and "kill -TERM $PPID" in rig.deliveries("ticket-909")[-1]["text"], out[-300:])
        rig.mode("ok")
        rig.row(909, "ticket.commented", "closing notes from the peer", actor="peer3")
        rc, out = rig.dispatch()
        check("a peer's closing notes on the closing thread still reach the live holder (with the wrap-up again)",
              rig.verb(out, "ticket-909") == "continue" and "kill -TERM $PPID" in rig.deliveries("ticket-909")[-1]["text"], rig.verb(out, "ticket-909"))
        rig.gone(p9)

        print("  [5c] the settle-window race (staging #110): the holder ends AFTER the sweep looked — dispatch must not resume it as a stream on a resolved object")
        # Re-arm: a closing holder that ignores the wrap-up, then dies; RAIL_SWEEP_AT_DISPATCH=0
        # reproduces "the sweep saw it alive, the settle window saw it die".
        rig.reg.tickets[900] = {"status": "open", "assignee": "tester"}
        rig.row(900, "ticket.updated", "", actor="alice", status="open")
        rig.mode("ignore-close")
        rc, out = rig.dispatch()                                   # closed + reopened → resume (stream)
        pr = rig.binding(K).get("pid")
        rig.reg.tickets[900] = {"status": "resolved", "assignee": "tester"}
        rig.row(900, "ticket.updated", "", actor="alice", status="resolved")
        rc, out = rig.dispatch()                                   # closing; the stub stays (ignore-close)
        check("re-armed: closing with a live holder", rig.verb(out, K) == "closing" and rig.alive(pr), rig.verb(out, K))
        os.kill(pr, signal.SIGKILL); rig.gone(pr)                  # it ends after the sweep would have looked
        rig.mode("ok")
        rig.row(900, "ticket.commented", "a reply from a human, 47 s after the wrap-up", actor="alice")
        rc, out = rig.dispatch()
        check("a resolved thread whose holder has ended answers as a one-shot — never a stream resume",
              rig.verb(out, K) == "oneshot" and rig.last_spawn().get("mode") == "oneshot"
              and "closed" in out, (rig.verb(out, K), rig.last_spawn(), out[-200:]))

        print("  [5e] the served artifact never puts a bearer in argv (#632 / #582): content check on the rail itself")
        src = RAIL.read_text()
        check("no `Authorization: Bearer ` followed by a shell expansion anywhere in generated shell",
              'Bearer "\n' not in src and "Bearer \"\n" not in src
              and not re.search(r"Authorization: Bearer [^\n]*\$\(cat", src)
              and not re.search(r'Authorization: Bearer "\s*\n\s*f"\$\(cat', src), "argv-bearer form present")
        check("the tmux mint passes a header file by path (-H @…)", "-H @{shlex.quote(str(mint_hdr))}" in src)
        check("the default holder class is stream", 'HOLDER = _conf("RAIL_HOLDER", "stream")' in src)
        check("credential files are born 0600 — no write_text/chmod pair on a header file",
              not re.search(r"hdr\.write_text\(", src) and "_write_0600(" in src, "write_text on a header file")
        mode = oct(os.stat(rig.state / "auth.hdr").st_mode & 0o777)
        check("the seat's auth.hdr is 0600 on disk", mode == "0o600", mode)
        # 3.9.19: "am I a holder?" is a flag, not a heuristic. The
        # interactive-surface hooks go silent on it, and since rail 3.10 a
        # registered interactive session carries SAMCLOUD_SESSION_ID of its
        # own — so inferring holder-ness from that variable would silence the
        # one surface that needs the digest.
        check("every holder class carries SAMCLOUD_HOLDER=1 (stream env + tmux launcher)",
              '"SAMCLOUD_HOLDER": "1"' in src and "SAMCLOUD_HOLDER=1 " in src,
              "holder marker missing from one of the two launch paths")

        print("  [5d] a mention on a RESOLVED object this seat never held → a one-off with a fresh session, not a resident (3.9.14, the un-dry backlog)")
        rig.reg.tickets[906] = {"status": "resolved", "assignee": "someone"}
        n_spawn = rig.spawn_count()
        rig.row(906, "ticket.mentioned", "@tester one more thing on this old one", actor="alice")
        rc, out = rig.dispatch()
        s6 = rig.last_spawn()
        check("verb: oneshot, spawned with --session-id (fresh), one-shot mode, no resident",
              rig.verb(out, "ticket-906") == "oneshot" and rig.spawn_count() == n_spawn + 1
              and s6.get("mode") == "oneshot" and s6.get("flag") == "--session-id", (rig.verb(out, "ticket-906"), s6))
        time.sleep(1.5)
        check("its binding was open·oneshot and is closed by the wrapper; nothing resident holds #906",
              rig.reg.threads.get(("ticket", 906), {}).get("state") == "closed"
              and rig.binding("ticket-906").get("state") == "closed", str(rig.reg.threads.get(("ticket", 906))))
        # 3.9.15 (prod #615 on the un-dry): a former holder's reply that @-mentions a never-held
        # seat opens nothing on a resolved object; a human's mention still opens a one-off.
        rig.reg.tickets[907] = {"status": "resolved", "assignee": "someone"}
        rig.reg.peers.append({"id": 991, "kind": "ticket", "object_id": 907, "identity": "peer",
                              "holder": "oneshot", "state": "closed", "conversation": "beefdead"})
        n_spawn = rig.spawn_count()
        rig.row(907, "ticket.mentioned", "@tester see my answer above", actor="peer")
        rc, out = rig.dispatch()
        check("a former holder's mention on a resolved object opens nothing for a seat that never held it",
              "open nothing" in out and rig.spawn_count() == n_spawn and not rig.binding("ticket-907"), out[-250:])
        rig.row(907, "ticket.mentioned", "@tester one question from a human", actor="alice")
        rc, out = rig.dispatch()
        check("a human's mention on the same resolved object still opens a fresh one-off",
              rig.verb(out, "ticket-907") == "oneshot" and rig.spawn_count() == n_spawn + 1, rig.verb(out, "ticket-907"))
        # owner rule opens on conversational rows only
        rig.reg.tickets[908] = {"status": "open", "assignee": "tester"}
        n_spawn = rig.spawn_count()
        rig.row(908, "ticket.updated", "", actor="alice", priority="high", assignee="tester", changed=["priority"])
        rc, out = rig.dispatch()
        check("a metadata row on an owned open ticket with no instance does not birth (assignment unchanged: priority only)",
              rig.spawn_count() == n_spawn and not rig.binding("ticket-908"), out[-200:])

        print("  [6] late message on a closed thread → one-shot native resume, then acked")
        late = rig.row(900, "ticket.commented", "one more thing", actor="alice")
        rc, out = rig.dispatch()
        check("verb: oneshot", rig.verb(out, K) == "oneshot", rig.verb(out, K))
        time.sleep(1.5)
        s = rig.last_spawn()
        check("spawned `-p --resume <old sid>` (oneshot mode), not a resident",
              s.get("mode") == "oneshot" and s.get("flag") == "--resume"
              and s.get("sid") == rig.binding(K).get("sid"), str(s))
        check("the late row was acked by the wrapper on success", late["id"] in rig.reg.acked, str(rig.reg.acked))
        check("binding was open(oneshot) while it ran, closed by the wrapper at exit — an event, not a clock",
              any(b.get("holder") == "oneshot" and b.get("state") == "open" for b in rig.reg.posts("/api/v1/wake/threads"))
              and rig.reg.threads[("ticket", 900)]["state"] == "closed", str(rig.reg.threads[("ticket", 900)]))
        check("the one-shot row named no pid — never the dead resident's (staging #100 finding 3)",
              all(b.get("pid") is None for b in rig.reg.posts("/api/v1/wake/threads") if b.get("holder") == "oneshot"),
              str([b for b in rig.reg.posts("/api/v1/wake/threads") if b.get("holder") == "oneshot"]))
        check("no resident pid on the closed binding", rig.binding(K).get("state") == "closed")
        rc, out = rig.dispatch()
        check("nothing re-offered after the one-shot", rig.verb(out, K) in (None, "idle"), out[-200:])

        print("  [6b] on a RESOLVED thread, former holders do not answer each other (3.9.12, staging #108)")
        rig.reg.peers.append({"id": 990, "kind": "ticket", "object_id": 900, "identity": "peer",
                              "holder": "stream", "state": "closed", "conversation": "deadbeef"})
        n_spawn = rig.spawn_count()
        rig.row(900, "ticket.commented", "a one-shot's reply from a peer who also held this thread", actor="peer")
        rc, out = rig.dispatch()
        check("a former holder's late message is not delivered (no one-shot chain)",
              "open nothing" in out and rig.spawn_count() == n_spawn
              and rig.verb(out, K) is None, out[-300:])
        rig.row(900, "ticket.commented", "a human's late question", actor="alice")
        rc, out = rig.dispatch()
        check("a late message from someone who never held the thread still gets its one-shot",
              rig.verb(out, K) == "oneshot" and rig.spawn_count() == n_spawn + 1, rig.verb(out, K))
        time.sleep(1.5)
        # A `conversation` row (a ticket this identity filed) is a binding; a
        # message on a held thread still delivers as a plain continue beside it.
        rig.reg.threads[("ticket", 950)] = {"id": 950, "kind": "ticket", "object_id": 950, "identity": "tester",
                                             "conversation_id": "filer-sid", "holder": "conversation", "state": "open"}
        rig.row(902, "ticket.commented", "still here", actor="alice")
        rc, out = rig.dispatch()
        check("a filer binding elsewhere does not change how a held thread is delivered",
              rig.verb(out, "ticket-902") in ("continue", "resume") and "holding" not in rig.deliveries("ticket-902")[-1]["text"],
              out[-200:])

        print("  [7] reopen — the object is open again and someone speaks → the thread streams again (native resume, same sid)")
        rig.reg.tickets[900] = {"status": "open", "assignee": "tester"}
        rig.row(900, "ticket.mentioned", "back again", actor="alice")
        rc, out = rig.dispatch()
        check("verb: resume — the reopened thread streams again on its own conversation", rig.verb(out, K) == "resume", rig.verb(out, K))
        p3 = rig.binding(K).get("pid")
        check("new live pid, same sid, binding open on the plane",
              p3 and rig.alive(p3) and rig.binding(K).get("state") == "open"
              and rig.reg.threads[("ticket", 900)]["state"] == "open"
              and rig.reg.threads[("ticket", 900)]["conversation_id"] == rig.binding(K).get("sid")
              and rig.last_spawn()["flag"] == "--resume", str(rig.last_spawn()))

        print("  [8] auth-dead — 'Not logged in' idle result IS death → resume with the sid")
        rig.mode("notlogged")
        rig.row(900, "ticket.commented", "trigger notlogged", actor="alice")
        rig.dispatch()
        rig.mode("ok")
        rig.row(900, "ticket.commented", "after", actor="alice")
        rc, out = rig.dispatch()
        check("auth-dead logged + verb: resume", "auth-dead" in out and rig.verb(out, K) == "resume", out[-200:])
        check("new spawn with --resume <same sid>",
              rig.last_spawn()["flag"] == "--resume" and rig.last_spawn()["mode"] == "stream"
              and rig.last_spawn()["sid"] == rig.binding(K).get("sid"), str(rig.last_spawn()))
        p4 = rig.binding(K).get("pid")
        check("binding moved to the new pid", p4 and p4 != p3 and rig.alive(p4))

        print("  [9] the process ends (wrap-up, rage-quit, crash, reboot alike) → closed; next message on an OPEN object → stream again via native resume, same sid")
        os.kill(p4, signal.SIGKILL)
        check("holder gone", rig.gone(p4), str(p4))
        end = time.time() + 5
        while time.time() < end and rig.reg.threads[("ticket", 900)]["state"] != "closed":
            time.sleep(0.1)
        check("a killed holder's exit is reported the same way (launcher PATCH, no traffic)",
              rig.reg.threads[("ticket", 900)]["state"] == "closed", str(rig.reg.threads[("ticket", 900)]))
        rig.row(900, "ticket.commented", "after the process ended", actor="alice")
        rc, out = rig.dispatch()
        check("sweep: ticket-900 closed (process ended) — no forensics, no reaping",
              "ticket-900: closed (process ended)" in out, out[-300:])
        check("verb: resume — the client reconnects to its own log, same sid, as a stream", rig.verb(out, K) == "resume", rig.verb(out, K))
        p5 = rig.binding(K).get("pid")
        check("new live pid, SAME sid (--resume, stream mode), binding open on the plane",
              p5 and p5 != p4 and rig.alive(p5) and rig.reg.threads[("ticket", 900)]["state"] == "open"
              and rig.last_spawn()["flag"] == "--resume" and rig.last_spawn()["mode"] == "stream"
              and rig.last_spawn()["sid"] == rig.binding(K).get("sid"), str(rig.last_spawn()))
        check("footer names the thread and how to reply", "— samcloud · ticket #900 · sc-say ticket 900 to reply" in rig.deliveries(K)[-1]["text"],
              rig.deliveries(K)[-1]["text"][-200:])

        print("  [10] a tmux holder has no pid — its window is the process; when the window is gone the thread closes like any other (#633 item 17)")
        reg_path = rig.state / "threads.json"
        reg_now = json.loads(reg_path.read_text())
        reg_now["ticket-905"] = {"key": "ticket-905", "sid": "tmux-sid-905", "holder": "tmux",
                                 "born": 1, "contract_v": "3.9.4", "state": "open"}
        reg_path.write_text(json.dumps(reg_now))
        rig.reg.tickets[905] = {"status": "resolved", "assignee": "tester"}
        rig.row(902, "ticket.commented", "any traffic runs the sweep", actor="alice")
        rc, out = rig.dispatch()
        check("sweep: ticket-905 closed (process ended) — no window, no pid needed",
              "ticket-905: closed (process ended)" in out, out[-300:])
        check("tmux thread closed on the plane and locally",
              rig.reg.threads.get(("ticket", 905), {}).get("state") == "closed"
              and rig.binding("ticket-905").get("state") == "closed", str(rig.binding("ticket-905")))
        late905 = rig.row(905, "ticket.commented", "late, on a resolved object", actor="alice")
        rc, out = rig.dispatch()
        check("a late message on the closed tmux thread of a resolved object is a one-shot, not a stream",
              rig.verb(out, "ticket-905") == "oneshot", rig.verb(out, "ticket-905"))

        # ── Phase C (doc 174): the same lifecycle on tasks, docs and events ──
        print("  [11] tasks — assignment opens, comments continue, done closes, a late comment is a one-shot")
        n_spawn = rig.spawn_count()
        rig.reg.tasks[700] = {"status": "open", "assignee": "tester"}
        rig.row_on("task", 700, "task.assigned", "do the thing", project="p")
        rc, out = rig.dispatch()
        check("task.assigned → task-700: wake", rig.verb(out, "task-700") == "wake", out[-300:])
        pt = rig.binding("task-700").get("pid")
        check("stream holder born; binding posted as kind task",
              pt and rig.alive(pt) and rig.reg.threads.get(("task", 700), {}).get("state") == "open",
              str(rig.reg.threads.get(("task", 700))))
        rig.row_on("task", 700, "task.commented", "how is it going")
        rc, out = rig.dispatch()
        check("task.commented → continue, same pid", rig.verb(out, "task-700") == "continue"
              and rig.binding("task-700").get("pid") == pt, rig.verb(out, "task-700"))
        rig.reg.tasks[700]["status"] = "done"
        rig.row_on("task", 700, "task.updated", "", status="done")
        rc, out = rig.dispatch()
        check("task.updated done → closing (wrap-up delivered: 'alice marked done task #700')",
              rig.verb(out, "task-700") == "closing" and "marked done" in rig.deliveries("task-700")[-1]["text"],
              rig.deliveries("task-700")[-1]["text"][-200:])
        check("holder exited on its own", rig.gone(pt), str(pt))
        rig.row(902, "ticket.commented", "traffic", actor="alice")
        rc, out = rig.dispatch()
        check("sweep: task-700 closed (wrapped up)", "task-700: closed (wrapped up)" in out, out[-300:])
        rig.row_on("task", 700, "task.commented", "one more, late")
        rc, out = rig.dispatch()
        check("late comment on a done task → oneshot", rig.verb(out, "task-700") == "oneshot", rig.verb(out, "task-700"))

        print("  [12] docs — the author owns; a comment opens, a revision continues; no close event: the holder's own ending closes it; the next message is a STREAM resume (a doc is never resolved)")
        rig.reg.docs[800] = {"source": "tester", "status": "published"}
        rig.reg.docs[801] = {"source": "someone", "status": "published"}
        rig.row_on("doc", 801, "doc.commented", "on a doc I did not write")
        rc, out = rig.dispatch()
        check("comment on a doc I do not own and do not hold: not delivered",
              "not delivered" in out and "doc-801" in out and not rig.binding("doc-801"), out[-200:])
        rig.row_on("doc", 800, "doc.commented", "question on your doc")
        rc, out = rig.dispatch()
        check("comment on a doc I authored, no instance → doc-800: wake (owner = author)",
              rig.verb(out, "doc-800") == "wake", out[-300:])
        pd = rig.binding("doc-800").get("pid")
        rig.row_on("doc", 800, "doc.revised", "", note="tightened §2")
        rc, out = rig.dispatch()
        check("doc.revised → continue, same pid", rig.verb(out, "doc-800") == "continue"
              and rig.binding("doc-800").get("pid") == pd, rig.verb(out, "doc-800"))
        os.kill(pd, signal.SIGKILL)
        check("holder gone (the only close a doc thread has)", rig.gone(pd), str(pd))
        rig.row_on("doc", 800, "doc.commented", "another question")
        rc, out = rig.dispatch()
        check("sweep closed it; the next comment RESUMES the same conversation as a stream — never a one-shot",
              "doc-800: closed (process ended)" in out and rig.verb(out, "doc-800") == "resume"
              and rig.last_spawn().get("mode") == "stream" and rig.last_spawn().get("flag") == "--resume"
              and rig.last_spawn().get("sid") == rig.binding("doc-800").get("sid"), out[-300:])

        print("  [13] events — a schedule rides its task and is PARKED (inbox only); seat notices are not delivered; nothing wakes the lobby")
        n_spawn = rig.spawn_count()
        rig.reg.tasks[701] = {"status": "open", "assignee": "tester"}       # my own open task, no instance
        rig.row_on("task", 701, "schedule.fired", "Scheduled: standup", actor="registry", recurrence="daily")
        rc, out = rig.dispatch()
        check("schedule.fired on a task I own → parked, no holder (a recurring schedule would hold a task forever)",
              "parked (inbox only): schedule.fired@task-701" in out and rig.spawn_count() == n_spawn
              and not rig.binding("task-701"), out[-300:])
        rig.row_on("lobby", 0, "wake.dark", "", actor="registry", identity="tester", reason="silent-past-bound")
        rc, out = rig.dispatch()
        check("wake.dark → not delivered (lobby key, never delivered)", "not delivered" in out and "lobby" in out
              and rig.verb(out, "lobby") is None and rig.spawn_count() == n_spawn, out[-200:])
        check("the lobby never posts a binding (seat-local, no object)",
              not any(b.get("kind") not in ("ticket", "task", "doc") for b in rig.reg.posts("/api/v1/wake/threads")))

        print("  [14] the twin (3.9.20, doc 221) — own writes never ring; a connected surface holds; dead bindings are swept")
        n_spawn = rig.spawn_count()
        # (a) my identity writes on my own open ticket from an interactive conversation:
        #     the row is mine, whoever's conversation wrote it — no holder.
        rig.reg.tickets[801] = {"status": "open", "assignee": "tester"}
        rig.row(801, "ticket.commented", "note from my terminal", actor="tester", actor_session="e8cfae4f")
        rc, out = rig.dispatch()
        check("my own write on my own open ticket births nothing (own writes never ring you)",
              rig.spawn_count() == n_spawn and not rig.binding("ticket-801"), out[-300:])
        # (b) a peer replies on a ticket my CONNECTED interactive surface holds: the
        #     surface's feed delivers it; this seat must not birth a twin.
        rig.reg.surfaces = [{"id": 1, "identity": "tester", "surface": "interactive", "ended": False,
                             "conversation": "e8cfae4f", "conversation_id": "surface-conv-1"}]
        rig.reg.threads[("ticket", 802)] = {"id": 802, "kind": "ticket", "object_id": 802, "identity": "tester",
                                             "conversation_id": "surface-conv-1", "holder": "conversation",
                                             "state": "open", "pid": None}
        rig.reg.tickets[802] = {"status": "open", "assignee": "tester"}
        rig.row(802, "ticket.mentioned", "hey @tester", actor="peer")
        n_patch = len([c for c in rig.reg.calls if c[0] == "PATCH"])
        rc, out = rig.dispatch()
        check("a mention on a thread held by a REGISTERED surface births no holder (the surface delivers)",
              rig.spawn_count() == n_spawn and "interactive surface" in out
              and rig.reg.threads[("ticket", 802)]["holder"] == "conversation", out[-300:])
        check("a registered surface's binding is NOT swept",
              rig.reg.threads[("ticket", 802)]["state"] == "open"
              and not any(p.endswith("/wake/threads/802") for m, p, _ in rig.reg.calls[n_patch:] if m == "PATCH"),
              str([c for c in rig.reg.calls if c[0] == "PATCH"][-3:]))
        # (c) the surface disconnects: the thread is unheld; the next message births a holder as usual
        rig.reg.surfaces[0]["ended"] = True
        rig.row(802, "ticket.commented", "still there?", actor="peer")
        rc, out = rig.dispatch()
        check("the same thread with the surface ENDED births one holder (unheld, not lost)",
              rig.spawn_count() == n_spawn + 1 and rig.verb(out, "ticket-802") in ("wake", "resume"), out[-300:])
        # (d) a `conversation` binding this seat cannot account for is LEFT ALONE
        # (3.13.0). This cell used to assert the opposite, and the behaviour it
        # asserted is what closed a live person's bindings on three tickets at
        # 2026-09-20 22:37:35 — the rail could not find the surface in a list it
        # had read, called the conversation gone, and a twin was born four
        # minutes later. Retiring a surface is the plane's job now, on the one
        # event that means it; a seat guessing at another conversation's
        # liveness is the whole family of bug this release is about.
        rig.reg.threads[("ticket", 803)] = {"id": 803, "kind": "ticket", "object_id": 803, "identity": "tester",
                                             "conversation_id": "dead-nonce-2c2d8c4a", "holder": "conversation",
                                             "state": "open", "pid": None}
        rc, out = rig.dispatch()
        check("a `conversation` binding is never swept by a seat — only the plane retires it",
              rig.reg.threads[("ticket", 803)]["state"] == "open"
              and "binding closed (conversation" not in out, out[-300:])

        print("  [14e] a registered surface holds with NO poll open — the 2026-09-20 incident")
        # The cell that did not exist. Every surface cell above pre-binds the
        # thread AND implies a live poll, so the failure was unreachable here:
        # `connected` went false (a deploy cleared the registry's in-process
        # dict; a subscriber printing a backlog is not polling), the sweep read
        # the binding as dead, and a twin was born on a thread the person held.
        n_e = rig.spawn_count()
        rig.reg.surfaces = [{"id": 9, "identity": "tester", "surface": "interactive",
                             "ended": False, "conversation": "e8cfae4f",
                             "conversation_id": "surface-live"}]
        rig.reg.threads[("ticket", 820)] = {"id": 820, "kind": "ticket", "object_id": 820,
                                            "identity": "tester", "conversation_id": "surface-live",
                                            "holder": "conversation", "state": "open", "pid": None}
        rig.reg.tickets[820] = {"status": "open", "assignee": "tester"}
        rig.row(820, "ticket.mentioned", "hey @tester", actor="peer")
        rc, out = rig.dispatch()
        check("no poll, no liveness: the surface still holds and no twin is born",
              rig.spawn_count() == n_e and "interactive surface" in out
              and rig.reg.threads[("ticket", 820)]["state"] == "open", out[-300:])
        check("the sweep leaves a registered surface's binding alone",
              rig.reg.threads[("ticket", 820)]["holder"] == "conversation",
              str(rig.reg.threads[("ticket", 820)]))

        print("  [14f] `ended` is what gives the thread up — then a holder IS born")
        rig.reg.surfaces[0]["ended"] = True
        rig.row(820, "ticket.commented", "still there?", actor="peer")
        rc, out = rig.dispatch()
        check("an ended surface holds nothing; the next message births one holder",
              rig.spawn_count() == n_e + 1, out[-300:])

        print("  [14g] quiet marks survive a rail ship (3.11.6)")
        # They were keyed on RAIL_VERSION: every release re-judged everything
        # every seat had ever ignored. Shipping 3.11.5 woke claude-ios as
        # holder of a pre-v19 thread-615 entry, bodies dated two weeks earlier,
        # whose reply target 404s. A ship is not a reason to reconsider.
        import json as _json
        _qp = rig.state / "quiet.json"
        _qp.write_text(_json.dumps({"rail": "3.0.0-ancient", "marks": {"ticket-901": 999999}}))
        _n = rig.spawn_count()
        rig.reg.tickets[901] = {"status": "open", "assignee": "nobody-else"}
        rig.row(901, "ticket.commented", "an old row already judged inbox-only", actor="peer")
        rc, out = rig.dispatch()
        check("a mark written by an older rail version is still honoured — no re-judging",
              rig.spawn_count() == _n, out[-300:])
        _after = _json.loads(_qp.read_text())
        check("marks are written under an epoch, not a version",
              _after.get("epoch") == 1, str(_after)[:160])

        print("  [15] a stale close in a batch never wraps up an object that is open now (3.9.21, #686)")
        # a held thread gets [resolve, reopen, comment] in one batch — the object is OPEN now
        rig.reg.tickets[804] = {"status": "open", "assignee": "tester"}
        rig.row(804, "ticket.mentioned", "hey @tester, take this", actor="peer")
        rc, out = rig.dispatch()
        check("setup: holder born on 804", rig.verb(out, "ticket-804") == "wake", out[-200:])
        rig.row(804, "ticket.updated", "", actor="peer", status="resolved")
        rig.row(804, "ticket.updated", "", actor="peer", status="open")
        rig.row(804, "ticket.commented", "carry on", actor="peer")
        rc, out = rig.dispatch()
        d = rig.deliveries("ticket-804")
        check("the batch is a plain continue: no wrap-up text, entry stays open",
              rig.verb(out, "ticket-804") == "continue" and "This thread is done" not in d[-1]["text"]
              and rig.binding("ticket-804")["state"] == "open", (d[-1]["text"][-200:] if d else out[-200:]))
        print("  [16] re-kick after a usage-limit 429 once its named reset has passed (3.9.21, doc 221 M5)")
        rig.mode("ok")
        rig.reg.tickets[805] = {"status": "open", "assignee": "tester"}
        rig.row(805, "ticket.mentioned", "@tester one for you", actor="peer")
        rc, out = rig.dispatch()
        check("setup: holder born on 805", rig.verb(out, "ticket-805") == "wake", out[-200:])
        rig.mode("limit-past")
        rig.row(805, "ticket.commented", "and this one lands on the ceiling", actor="peer")
        rc, out = rig.dispatch()
        time.sleep(1.0)
        rig.mode("ok")
        import glob as _g
        for f in _g.glob(str(rig.state / "threads" / "ticket-805" / "out-*.jsonl")):
            os.utime(f, (time.time() - 7200, time.time() - 7200))   # the 429 was written two hours ago
        n_del = len(rig.deliveries("ticket-805"))
        rig.reg.tickets[806] = {"status": "open", "assignee": "someone"}
        rig.row(806, "ticket.commented", "unrelated traffic, same identity", actor="peer")   # any event runs the pass
        rc, out = rig.dispatch()
        d = rig.deliveries("ticket-805")
        check("one CONTINUE re-kicks the parked holder after the reset it named",
              "re-kicked after the usage limit" in out and len(d) == n_del + 1
              and "usage limit your last turn hit has reset" in d[-1]["text"], out[-300:])
        rig.row(806, "ticket.commented", "more unrelated traffic", actor="peer")
        rc, out = rig.dispatch()
        check("and only once per reset", len(rig.deliveries("ticket-805")) == n_del + 1 and "re-kicked" not in out, out[-200:])

        rig.reg.tickets[804]["status"] = "resolved"
        rig.row(804, "ticket.updated", "", actor="peer", status="resolved")
        rc, out = rig.dispatch()
        check("a close on an object that IS closed now still wraps up",
              "This thread is done" in rig.deliveries("ticket-804")[-1]["text"]
              and rig.binding("ticket-804")["state"] in ("closing", "closed"), rig.deliveries("ticket-804")[-1]["text"][-200:])

        # ── doc 239: the universal thread ─────────────────────────────────────
        print("  [17] one thread, several members, ONE holder (3.10.0, doc 239)")
        rig.mode("ok")
        rig.reg.tickets[706] = {"status": "open", "assignee": "tester"}
        rig.reg.tasks[648] = {"status": "open", "assignee": None}
        rig.reg.docs[237] = {"source": "tester", "status": "active"}
        # a pre-v19 holder bound under the OBJECT key, before the plane learned threads
        rig.row(706, "ticket.created", "the brief", actor="alice", assignee="tester")
        rc, out = rig.dispatch()
        check("setup: holder born under the object key (a pre-v19 plane)", rig.verb(out, "ticket-706") == "wake", out[-200:])
        pid0 = rig.binding("ticket-706").get("pid"); sid0 = rig.binding("ticket-706").get("sid")
        # the plane now models the thread: 706 is its first member, task 648 and doc 237 are members too
        rig.reg.declare_thread(331, "ticket:706", {"task": [648], "doc": [237]}, title="Wide stage v1 — audit your own bounds")
        rig.reg.threads[("ticket", 706)]["thread_id"] = 331
        rig.row_on("task", 648, "task.commented", "taking step 2", actor="alice")
        rc, out = rig.dispatch()
        check("migration: the entry is re-keyed to its thread, once, and the old dir is linked",
              "re-keyed ticket-706 -> thread-331" in out and rig.binding("thread-331").get("sid") == sid0
              and not rig.binding("ticket-706") and (rig.state / "threads" / "thread-331").exists(), out[-300:])
        check("a row on a MEMBER task continues the thread's holder (same pid, no birth)",
              rig.verb(out, "thread-331") == "continue" and rig.binding("thread-331").get("pid") == pid0
              and rig.alive(pid0), out[-200:])
        d = rig.deliveries("ticket-706")[-1]["text"]
        check("the message names the member it sits on, the footer names the thread and where to reply",
              "on task #648" in d and "— samcloud · thread 331 · sc-say task 648 to reply" in d, d[-260:])
        rig.row_on("doc", 237, "doc.revised", "", actor="alice", rev=3, note="stale section")
        rig.row(706, "ticket.mentioned", "@tester see the doc", actor="alice")
        rc, out = rig.dispatch()
        check("a doc revision and a mention on the root, one batch, one continue, one holder",
              rig.verb(out, "thread-331") == "continue" and rig.binding("thread-331").get("pid") == pid0
              and "2 messages on thread 331" in rig.deliveries("ticket-706")[-1]["text"], out[-200:])
        check("the plane's binding for the thread is one row, keyed on its first member, carrying thread_id",
              rig.reg.threads[("ticket", 706)].get("thread_id") == 331 and ("task", 648) not in rig.reg.threads
              and ("doc", 237) not in rig.reg.threads, str(list(rig.reg.threads)))
        # a member the holder MADE (the plane put ticket 707 in thread 331): a reply on it is this thread's
        rig.reg.tickets[707] = {"status": "open", "assignee": "tester"}
        rig.reg.work_threads[331]["members"]["ticket"].append(707)
        rig.row(707, "ticket.commented", "on the follow-up", actor="alice")
        rc, out = rig.dispatch()
        check("a reply on a ticket the holder made inside the thread continues the same holder",
              rig.verb(out, "thread-331") == "continue" and rig.binding("thread-331").get("pid") == pid0
              and "sc-say ticket 707 to reply" in rig.deliveries("ticket-706")[-1]["text"], out[-200:])
        # a pre-v19 row (no stamp) on a member still finds the holder through the plane's binding
        r = rig.row(706, "ticket.commented", "an old-shaped row", actor="alice"); r.pop("thread_id", None)
        rc, out = rig.dispatch()
        check("an unstamped row on a member routes to the thread's holder via the plane's binding",
              rig.verb(out, "ticket-706") == "continue" and rig.binding("thread-331").get("pid") == pid0, out[-200:])

        print("  [18] the lifecycle is the thread's open work: a resolve with a task still open is not a close")
        rig.reg.tickets[706]["status"] = "resolved"
        rig.row(706, "ticket.updated", "", actor="alice", status="resolved")
        rc, out = rig.dispatch()
        d = rig.deliveries("ticket-706")[-1]["text"]
        check("root resolved, task 648 open: a plain continue, no wrap-up, entry open",
              rig.verb(out, "thread-331") == "continue" and "This thread is done" not in d
              and rig.binding("thread-331")["state"] == "open" and rig.alive(pid0), d[-200:])
        rig.reg.tasks[648]["status"] = "done"; rig.reg.tickets[707]["status"] = "resolved"
        rig.row_on("task", 648, "task.updated", "", actor="alice", status="done")
        rig.row(707, "ticket.updated", "", actor="alice", status="resolved")
        rc, out = rig.dispatch()
        d = rig.deliveries("ticket-706")[-1]["text"]
        check("the last open member done: the wrap-up is delivered and the entry is closing (the doc never counted)",
              rig.verb(out, "thread-331") in ("continue", "closing") and "This thread is done" in d
              and rig.binding("thread-331")["state"] in ("closing", "closed"), d[-260:])

        print("  [19] a person closes a thread with open work: thread.closed wraps up every instance")
        rig.reg.tickets[710] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(340, "ticket:710", {"task": [651]}, title="closed by hand")
        rig.reg.tasks[651] = {"status": "open", "assignee": None}
        rig.row(710, "ticket.created", "work", actor="alice", assignee="tester")
        rc, out = rig.dispatch()
        check("setup: holder born on thread 340; the birth binding names the thread",
              rig.verb(out, "thread-340") == "wake" and rig.reg.threads[("ticket", 710)].get("thread_id") == 340
              and any(b.get("thread_id") == 340 and b.get("state") == "open" for b in rig.reg.posts("/api/v1/wake/threads")),
              out[-300:])
        p1 = rig.binding("thread-340").get("pid")
        rig.reg.work_threads[340]["state"] = "closed"
        rig.thread_row(340, "thread.closed", actor="admin", by="admin")
        rc, out = rig.dispatch()
        d = rig.deliveries("thread-340")[-1]["text"]
        check("thread.closed → wrap-up delivered, entry closing, though a task is still open",
              "This thread is done" in d and "closed the thread" in d
              and rig.binding("thread-340")["state"] in ("closing", "closed"), d[-260:])

        print("  [20] a mention of someone already on the thread is a message, not a birth")
        rig.reg.tickets[720] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(350, "ticket:720", {"doc": [240]})
        rig.reg.docs[240] = {"source": "peer", "status": "active"}
        rig.row(720, "ticket.created", "w", actor="alice", assignee="tester")
        rc, out = rig.dispatch()
        p2 = rig.binding("thread-350").get("pid")
        check("setup: one holder on thread 350", rig.verb(out, "thread-350") == "wake" and p2, out[-200:])
        rig.row_on("doc", 240, "doc.mentioned", "@tester read this", actor="peer")
        rc, out = rig.dispatch()
        check("a mention on a member doc of a held thread continues the holder; no second instance",
              rig.verb(out, "thread-350") == "continue" and rig.binding("thread-350").get("pid") == p2
              and rig.generations("thread-350") == 1 if (rig.state / "threads" / "thread-350").exists() else True, out[-200:])

        print("  [21] a thread with no open work: former holders' replies open nothing; a person's late message is a one-shot")
        rig.reg.tickets[730] = {"status": "resolved", "assignee": "tester"}
        rig.reg.declare_thread(360, "ticket:730", title="done work")
        rig.reg.peers.append({"id": 9001, "kind": "ticket", "object_id": 730, "identity": "peer", "thread_id": 360,
                              "conversation_id": "peer-conv", "holder": "stream", "state": "closed"})
        rig.row(730, "ticket.commented", "one more thing", actor="peer")
        rc, out = rig.dispatch()
        check("a former holder's reply on a thread with nothing open is not delivered",
              rig.verb(out, "thread-360") is None and "former holders' replies" in out, out[-300:])
        rig.row(730, "ticket.mentioned", "@tester one question", actor="alice")
        rc, out = rig.dispatch()
        check("a person's mention on it answers once (one-shot), no resident",
              rig.verb(out, "thread-360") == "oneshot", out[-300:])

        print("  [22] a thread held by a connected interactive surface births nothing, whichever member the row sits on")
        rig.reg.tickets[740] = {"status": "open", "assignee": "tester"}
        rig.reg.tasks[660] = {"status": "open", "assignee": None}
        rig.reg.declare_thread(370, "ticket:740", {"task": [660]}, title="held from a terminal")
        rig.reg.surfaces.append({"id": 7, "identity": "tester", "surface": "interactive", "conversation": "surf0001",
                                 "conversation_id": "surface-0001", "ended": False})
        rig.reg.threads[("ticket", 740)] = {"id": 77, "kind": "ticket", "object_id": 740, "identity": "tester",
                                            "conversation_id": "surface-0001", "holder": "conversation",
                                            "state": "open", "pid": None, "thread_id": 370}
        rig.row_on("task", 660, "task.commented", "on the task", actor="alice")
        rc, out = rig.dispatch()
        check("a row on a member task of a surface-held thread: no holder, the feed delivers",
              rig.verb(out, "thread-370") is None and "interactive surface" in out, out[-300:])
        rig.reg.surfaces[-1]["ended"] = True
        rig.row_on("task", 660, "task.commented", "again, surface gone", actor="alice")
        rc, out = rig.dispatch()
        check("the surface disconnected: the same thread births one holder (the binding sweep closed the dead one)",
              rig.verb(out, "thread-370") == "wake", out[-300:])

        print("  [23] thread.closed with no instance here is inbox-only")
        rig.reg.declare_thread(380, "ticket:750", title="never mine")
        rig.reg.tickets[750] = {"status": "open", "assignee": "peer"}
        rig.thread_row(380, "thread.closed", actor="admin")
        rc, out = rig.dispatch()
        check("nothing to wrap up: not delivered", rig.verb(out, "thread-380") is None and "thread-380(1)" in out, out[-300:])

        print("  [24] a mention-birth says so and carries the thread's work; an assignment-birth says 'you hold' (3.11.0, doc 246 D2)")
        rig.reg.tickets[760] = {"status": "open", "assignee": "peer"}
        rig.reg.declare_thread(390, "ticket:760", title="P4: C's own thread")
        rig.row(760, "ticket.mentioned", "@tester which rail version runs on your seat?", actor="peer",
                root={"kind": "ticket", "id": 760, "title": "P4: C's own thread", "read": "GET /api/v1/tickets/760"})
        rc, out = rig.dispatch()
        d = rig.deliveries("thread-390")[-1]["text"]
        check("born from a mention: 'You were mentioned on … (held by peer)', the rule, no 'you hold'",
              d.startswith("You are tester on thread 390.") and "peer mentioned you on thread 390" in d
              and "background, not an assignment" in d
              and "Replies to you arrive here" in d and "You are on this thread" not in d, d[:300])
        check("the thread's work is introduced by an authoritative read",
              "GET /api/v1/tickets/760" in d and "Reply here once and wait." not in d, d[:400])
        rig.reg.tickets[762] = {"status": "open", "assignee": "peer"}
        rig.reg.declare_thread(392, "ticket:762", title="named in the body")
        rig.row(762, "ticket.mentioned", "the brief itself, naming @tester", actor="alice",
                root={"kind": "ticket", "id": 762, "title": "named in the body", "body": "the brief itself, naming @tester"})
        rc, out = rig.dispatch()
        d = rig.deliveries("thread-392")[-1]["text"]
        check("the mention IS the work (a ticket body naming you): shown once, as the message",
              d.count("the brief itself, naming @tester") == 1 and "The thread's work" not in d, d[:400])
        rig.reg.tickets[761] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(391, "ticket:761", title="mine")
        rig.row(761, "ticket.created", "the brief", actor="alice", assignee="tester")
        rc, out = rig.dispatch()
        d = rig.deliveries("thread-391")[-1]["text"]
        check("born from an assignment: shared context, addressed work in the message",
              d.startswith("You are tester on thread 391.") and "New thread — thread 391" in d and "shared conversation context" in d, d[:200])

        print("  [25] the exit window (3.11.0, doc 246 D4): a peer's closing note after the wrap-up is not a one-shot")
        rig.reg.tickets[770] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(400, "ticket:770", title="T1b")
        rig.reg.peers.append({"id": 9002, "kind": "ticket", "object_id": 770, "identity": "peer", "thread_id": 400,
                              "conversation_id": "peer-conv-2", "holder": "stream", "state": "closing"})
        rig.row(770, "ticket.created", "work", actor="alice", assignee="tester")
        rc, out = rig.dispatch()
        p3 = rig.binding("thread-400").get("pid")
        check("setup: holder on thread 400", rig.verb(out, "thread-400") == "wake" and p3, out[-200:])
        rig.reg.tickets[770]["status"] = "resolved"
        rig.row(770, "ticket.updated", "", actor="alice", status="resolved")
        rc, out = rig.dispatch()
        check("the wrap-up: closing, and the stub leaves", rig.verb(out, "thread-400") == "closing" and rig.gone(p3), out[-200:])
        n0 = rig.spawn_count()
        rig.row(770, "ticket.commented", "Closing note (peer): done here.", actor="peer")
        rc, out = rig.dispatch()
        check("the peer's closing note lands with the entry closing and the process gone: closed now, not delivered, no one-shot",
              "closed (wrapped up" in out and rig.verb(out, "thread-400") in (None, "closed")
              and "former holders' replies and metadata" in out and rig.spawn_count() == n0, out[-400:])
        rig.row(770, "ticket.commented", "one thing from a person", actor="alice")
        rc, out = rig.dispatch()
        check("a person's late message still answers once", rig.verb(out, "thread-400") == "oneshot" and rig.spawn_count() == n0 + 1, out[-300:])

        print("  [25b] a late message that names someone is for them (3.11.1, doc 247 P9)")
        n0b = rig.spawn_count()
        rig.row(770, "ticket.commented", "@peer one question for you", actor="alice", mentions=["peer"])
        rc, out = rig.dispatch()
        check("named to a peer, not me: inbox only, no one-shot here", rig.spawn_count() == n0b and "thread-400(1)" in out, out[-300:])
        rig.row(770, "ticket.commented", "@tester and one for you", actor="alice", mentions=["tester"])
        rc, out = rig.dispatch()
        check("named to me: one-shot", rig.verb(out, "thread-400") == "oneshot" and rig.spawn_count() == n0b + 1, out[-300:])
        rig.row(770, "ticket.commented", "a word to the thread, naming nobody", actor="alice", mentions=[])
        rc, out = rig.dispatch()
        check("names nobody: every former holder once", rig.verb(out, "thread-400") == "oneshot" and rig.spawn_count() == n0b + 2, out[-300:])

        print("  [26] metadata never one-shots (3.11.0, doc 246 D5): a status row, a link, a priority on a closed thread")
        n1 = rig.spawn_count()
        rig.reg.work_threads[400]["members"]["doc"].append(241)
        rig.row(770, "ticket.updated", "", actor="admin", status="closed")
        rig.row_on("doc", 241, "doc.linked", "", actor="admin", target="ticket:770")
        rig.row(770, "ticket.updated", "", actor="admin", priority="low")
        rc, out = rig.dispatch()
        check("three rows with nothing to answer: inbox only, no spawn",
              rig.spawn_count() == n1 and "thread-400(3)" in out, out[-300:])

        print("  [27] a close the plane knows and no row carried (3.11.0, doc 246 D3): the holder still gets its wrap-up")
        rig.reg.tickets[780] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(410, "ticket:780", title="P13")
        rig.row(780, "ticket.created", "you plus you", actor="alice", assignee="tester")
        rc, out = rig.dispatch()
        p4 = rig.binding("thread-410").get("pid")
        check("setup: holder on thread 410", rig.verb(out, "thread-410") == "wake" and p4 and rig.alive(p4), out[-200:])
        # the identity's OTHER conversation resolves the ticket: no row for this identity, ever
        rig.reg.tickets[780]["status"] = "resolved"
        rc, out = rig.dispatch()
        d = rig.deliveries("thread-410")[-1]["text"]
        check("a pass with no rows: the wrap-up comes from the plane's word, the entry is closing, the stub leaves",
              "This thread is done" in d and "kill -TERM $PPID" in d
              and rig.binding("thread-410")["state"] in ("closing", "closed") and rig.gone(p4), out[-300:] + d[-200:])
        rig.reg.tickets[781] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(411, "ticket:781", title="P13 with traffic")
        rig.row(781, "ticket.created", "again", actor="alice", assignee="tester")
        rc, out = rig.dispatch()
        p5 = rig.binding("thread-411").get("pid")
        rig.reg.tickets[781]["status"] = "resolved"
        rig.row(781, "ticket.commented", "a peer speaks after the silent resolve", actor="peer")
        rc, out = rig.dispatch()
        d = rig.deliveries("thread-411")[-1]["text"]
        check("a pass with a peer's row and no close row: the message and the plane's wrap-up in one delivery",
              rig.verb(out, "thread-411") == "closing" and "a peer speaks" in d and "This thread is done" in d
              and rig.gone(p5), out[-300:] + d[-300:])
        rig.reg.docs[250] = {"source": "peer", "status": "active"}
        rig.reg.declare_thread(412, "doc:250", title="a doc-only thread")
        rig.row_on("doc", 250, "doc.mentioned", "@tester have a look", actor="peer")
        rc, out = rig.dispatch()
        check("a doc-rooted thread has no open members and is not 'done' for want of them: no wrap-up from the plane",
              "the plane says" not in out and "plane says so" not in (rig.deliveries("thread-412") or [{"text": ""}])[-1]["text"], out[-300:])

        print("  [27b] a pre-v19 entry the plane refuses is asked about once (3.11.1)")
        reg_path = rig.state / "threads.json"
        regd = json.loads(reg_path.read_text())
        regd["ticket-632"] = {"key": "ticket-632", "sid": "dead-sid", "holder": "stream", "state": "closed", "born": 1}
        reg_path.write_text(json.dumps(regd))
        rig.reg.refuse_threads_of = {632}
        rc, out = rig.dispatch()
        n_first = out.count("ticket-632: thread lookup failed")
        rc, out = rig.dispatch()
        check("asked once, then left alone", n_first == 1 and "ticket-632: thread lookup failed" not in out
              and json.loads(reg_path.read_text()).get("ticket-632", {}).get("unmigratable") is True, out[-300:])

        print("  [28] a row judged inbox-only is not judged again (3.11.0): quiet marks")
        rig.reg.tickets[790] = {"status": "open", "assignee": "peer"}
        rig.reg.declare_thread(420, "ticket:790", title="someone else's work")
        rig.row(790, "ticket.commented", "a comment on work I only watch", actor="peer")
        rc, out = rig.dispatch()
        check("first pass: judged, logged", "thread-420(1)" in out, out[-200:])
        rc, out = rig.dispatch()
        check("second pass: not re-judged, not re-logged", "thread-420" not in out, out[-200:])
        rig.row(790, "ticket.mentioned", "@tester now you", actor="peer")
        rc, out = rig.dispatch()
        check("a new row on the same key is judged (and opens the thread)", rig.verb(out, "thread-420") == "wake", out[-200:])
        # a surface-held thread's rows are never marked: the surface may go
        rig.reg.tickets[795] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(425, "ticket:795", title="held from a terminal, again")
        rig.reg.surfaces.append({"id": 8, "identity": "tester", "surface": "interactive", "conversation": "surf0002",
                                 "conversation_id": "surface-0002", "ended": False})
        rig.reg.threads[("ticket", 795)] = {"id": 78, "kind": "ticket", "object_id": 795, "identity": "tester",
                                            "conversation_id": "surface-0002", "holder": "conversation",
                                            "state": "open", "pid": None, "thread_id": 425}
        rig.row(795, "ticket.commented", "while the surface holds", actor="alice")
        rc, out = rig.dispatch()
        rig.reg.surfaces[-1]["ended"] = True
        rc, out = rig.dispatch()
        check("the surface gone: the same row, judged again, births the holder", rig.verb(out, "thread-425") == "wake", out[-300:])

        print("  [29] a doc answers every question once (3.11.2, doc 250 D10): no cap on a doc-rooted thread; the one-off says what it is and carries the work")
        rig.reg.docs[260] = {"source": "peer", "status": "active"}
        rig.reg.declare_thread(430, "doc:260", title="a doc people ask about")
        n2 = rig.spawn_count()
        for i in range(int(os.environ.get("RAIL_ONESHOT_ATTEMPTS_TEST", "9"))):
            rig.row_on("doc", 260, "doc.mentioned", f"@tester question {i}?", actor="alice",
                       root={"kind": "doc", "id": 260, "title": "a doc people ask about", "read": "GET /api/v1/docs/260"})
            rc, out = rig.dispatch()
        check("nine questions, nine one-offs — the cap of eight did not apply",
              rig.spawn_count() == n2 + 9 and "attempts exhausted" not in out, out[-300:])
        first = sorted((rig.state / "threads" / "thread-430").glob("oneshot-*.in.txt"))[0].read_text()
        check("the first one-off, fresh: 'Message on thread 430 · doc #260 (one-off — answer what it asks…)' and the work",
              first.startswith("You are tester on thread 430.") and "Message on thread 430. One turn, then finish" in first
              and "GET /api/v1/docs/260" in first and "the doc's text" not in first
              and "resolved" not in first, first[:300])

        print("  [30] a plane holder row of mine with a dead pid and no local entry is closed by the sweep (3.11.2)")
        rig.reg.declare_thread(440, "ticket:800", title="left over from a park")
        rig.reg.tickets[800] = {"status": "open", "assignee": "tester"}
        rig.reg.threads[("ticket", 800)] = {"id": 88, "kind": "ticket", "object_id": 800, "identity": "tester",
                                            "conversation_id": "old-conv-0001", "holder": "stream", "pid": 999999,
                                            "host": os.uname().nodename.split(".")[0], "state": "open", "thread_id": 440}
        rc, out = rig.dispatch()
        check("swept: the plane row is closed and the log says why",
              rig.reg.threads[("ticket", 800)]["state"] == "closed" and "holder pid 999999 is gone" in out, out[-300:])
        rig.reg.threads[("ticket", 801)] = {"id": 89, "kind": "ticket", "object_id": 801, "identity": "tester",
                                            "conversation_id": "elsewhere-0001", "holder": "stream", "pid": 4,
                                            "host": "another-box", "state": "open", "thread_id": 441}
        rig.reg.declare_thread(441, "ticket:801", title="another host's")
        rc, out = rig.dispatch()
        check("a row on another host is not mine to close", rig.reg.threads[("ticket", 801)]["state"] == "open", out[-200:])

        print("  [31] a create row is a sentence (3.11.2)")
        rig.reg.tickets[810] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(450, "ticket:810", {"task": [670]}, title="sentences")
        rig.reg.tasks[670] = {"status": "todo", "assignee": None}
        rig.row(810, "ticket.created", "work", actor="alice", assignee="tester")
        rc, out = rig.dispatch()
        rig.row_on("task", 670, "task.created", "", actor="peer", title="a member task", status="todo")
        rc, out = rig.dispatch()
        d = rig.deliveries("thread-450")[-1]["text"]
        check("'peer created the task · a member task' — not 'set status todo'",
              "peer created the task · a member task" in d and "set status" not in d, d[:300])

        # ── the runtime that does not stream (rail 3.12.0) ──────────────
        # A seat running Hermes/codex/a local model has no FIFO to be fed and
        # no ~/.claude/projects to resume. Before 3.12.0 the rail's answer was
        # to refuse it. These cells drive the REAL adapter against a stub
        # runtime, so the holder that is not Claude is reachable by the harness.
        RUNTIME_STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
T = os.environ["STUB_DIR"]
prompt = ""
for i, a in enumerate(args):
    if a == "--prompt" and i + 1 < len(args):
        prompt = args[i + 1]
if not prompt:
    prompt = sys.stdin.read()
conv = ""
for i, a in enumerate(args):
    if a == "--resume" and i + 1 < len(args):
        conv = args[i + 1]
with open(os.path.join(T, "runtime.log"), "a") as f:
    f.write(json.dumps({"argv": args, "conv": conv, "prompt": prompt,
                        "sid": os.environ.get("SAMCLOUD_SESSION_ID", "")}) + "\n")
try:
    say = open(os.path.join(T, "runtime-says")).read().strip()
except OSError:
    say = "the turtle has read it"
try:
    import time as _t; _t.sleep(float(open(os.path.join(T, "runtime-sleep")).read().strip()))
except Exception:
    pass
# a handle the adapter can learn, shaped like a real runtime's session id —
# unless this run is told it failed before starting a session
if not os.path.exists(os.path.join(T, "runtime-no-session")):
    open(os.path.join(T, "runtime-conv"), "w").write("20260920_120000_abcdef")
sys.stdout.write("" if say == "<silence>" else say + "\n")
try:
    _rc = int(open(os.path.join(T, "runtime-exit")).read().strip())
except Exception:
    _rc = 0
sys.exit(_rc)
'''
        rstub = rig.home / ".local/bin/stub-runtime"
        rstub.write_text(RUNTIME_STUB)
        rstub.chmod(0o755)
        # installed into the seat's bin exactly as wake-install puts it there,
        # so the cell does not depend on a mode bit in the checkout
        adapter = rig.home / ".local/bin/samcloud-adapter"
        adapter.write_text((REPO / "registry/helpers/samcloud-adapter.py").read_text())
        adapter.chmod(0o755)
        rt_env = {
            "RAIL_RUNTIME": "stubmodel",
            "RAIL_RUNTIME_CMD": f"{rstub} --prompt {{prompt}}",
            "RAIL_RUNTIME_RESUME": "--resume {conv}",
            "RAIL_RUNTIME_CONV_CMD": f"cat {rig.t}/runtime-conv",
            "RAIL_RUNTIME_CONV_RE": r"([0-9]{8}_[0-9]{6}_[0-9a-f]{6})",
            "RAIL_ADAPTER": str(adapter),
        }
        rt_log = rig.t / "runtime.log"

        def turns():
            return [json.loads(l) for l in rt_log.read_text().splitlines()] if rt_log.exists() else []

        print("  [32] a runtime that does not stream: one delivery, one turn, and the answer lands on the thread (3.12.0)")
        before_claude = rig.spawn_count()
        rig.reg.tickets[820] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(460, "ticket:820", title="the turtle")
        rig.row(820, "ticket.created", "read this and answer", actor="alice", assignee="tester")
        rc, out = rig.dispatch(**rt_env)
        time.sleep(2.0)                      # the adapter runs the turn, then posts
        check("the verb is wake", rig.verb(out, "thread-460") == "wake", out[-300:])
        check("no claude was spawned — the seat does not run one",
              rig.spawn_count() == before_claude, str(rig.spawn_count()))
        check("the runtime ran once, with the delivery as its prompt",
              len(turns()) == 1 and "read this and answer" in turns()[0]["prompt"],
              json.dumps(turns())[:300])
        posted = [c for c in rig.reg.comments if c["id"] == 820]
        check("what it said was posted on the ticket",
              len(posted) == 1 and "the turtle has read it" in posted[0]["body"],
              json.dumps(rig.reg.comments)[:300])
        bound = rig.reg.threads[("ticket", 820)]["conversation_id"]
        check("the reply carries the conversation the rail bound",
              bool(posted) and posted[0]["session"] == bound,
              f"{posted[0]['session'] if posted else None} vs {bound}")
        check("the binding named the adapter as its holder",
              rig.reg.threads[("ticket", 820)]["holder"] == "adapter",
              json.dumps(rig.reg.threads[("ticket", 820)])[:200])
        check("the turn ended, so the thread is no longer held",
              rig.reg.threads[("ticket", 820)]["state"] == "closed",
              json.dumps(rig.reg.threads[("ticket", 820)])[:200])

        print("  [33] the next delivery resumes the runtime's OWN conversation")
        rig.row(820, "ticket.commented", "and now this", actor="alice")
        rc, out = rig.dispatch(**rt_env)
        time.sleep(2.0)
        check("the second turn resumed the handle the adapter recorded",
              len(turns()) == 2 and turns()[1]["conv"] == "20260920_120000_abcdef",
              json.dumps(turns()[-1:])[:300])
        check("it is a resume, not a second birth", rig.verb(out, "thread-460") == "resume", out[-300:])
        check("the same conversation id stamped both replies",
              len({c["session"] for c in rig.reg.comments if c["id"] == 820}) == 1,
              json.dumps(rig.reg.comments)[:400])

        print("  [34] a turn that says nothing posts nothing — a silent runtime is a failed delivery, not an empty comment")
        (rig.t / "runtime-says").write_text("<silence>")
        before = len(rig.reg.comments)
        rig.row(820, "ticket.commented", "say nothing", actor="alice")
        rc, out = rig.dispatch(**rt_env)
        time.sleep(2.0)
        check("nothing was posted", len(rig.reg.comments) == before, json.dumps(rig.reg.comments[-2:])[:300])
        check("and the rows were not acked", rig.reg.acked and turns(), f"acked={sorted(rig.reg.acked)[-3:]}")
        (rig.t / "runtime-says").write_text("wrapping up")

        print("  [35] a close arriving mid-turn is delivered as a wrap-up — and never says kill -TERM $PPID (doc 265 §5)")
        (rig.t / "runtime-sleep").write_text("7")
        rig.row(820, "ticket.commented", "one more thing", actor="alice")
        rc, out = rig.dispatch(**rt_env)          # this turn is now sleeping
        rig.reg.tickets[820]["status"] = "resolved"
        rig.row(820, "ticket.updated", "", actor="alice", status="resolved")
        rc, out = rig.dispatch(**rt_env)          # the close lands while it runs
        (rig.t / "runtime-sleep").write_text("0")
        time.sleep(9.0)
        wrapups = [t["prompt"] for t in turns() if "resolved" in t["prompt"]]
        check("the wrap-up arrived", bool(wrapups), "LOG:" + out[-500:])
        last = wrapups[-1] if wrapups else ""
        check("and says 'this is the last turn on it', not kill -TERM $PPID",
              "kill -TERM" not in last and "last turn on it" in last, last[-300:])
        check("a Claude seat still gets the kill line",
              "kill -TERM $PPID" in (REPO / "registry/helpers/samcloud-rail.py").read_text())
        check("the second turn waited for the first — one turn at a time on a seat",
              len([t for t in turns() if "one more thing" in t["prompt"]]) == 1,
              str(len(turns())))

        # ── the plane refuses the twin (rail 3.13.0 / plane 0.12.18) ────
        # [22] proves the rail's own gate declines to birth. These prove the
        # rail asks BEFORE it births, and takes no for an answer — because on
        # 2026-09-21 the gate was blind (the binding it reads had been
        # overwritten by the previous twin) and three headless holders sat
        # under one identity for a day, one of them answering on a thread the
        # person was typing in.
        print("  [36] the rail asks before it births, and a refusal means no process (3.13.0)")
        rig.reg.tickets[830] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(470, "ticket:830", title="held from a terminal")
        # The surface is registered on the PLANE but the rail cannot resolve it
        # to a conversation id — `_held_by_surface` reads `conversation_id` and
        # this row carries only the fingerprint. That is the blind-gate case
        # exactly: on 2026-09-21 the rail's local view had lost the person's
        # binding (a previous twin's POST overwrote it) while the plane still
        # knew, and the gate had nothing to see. The plane must be the one that
        # says no, and this cell is the only way to reach that path.
        rig.reg.surfaces.append({"id": 12, "identity": "tester", "surface": "interactive",
                                 "conversation": "surface-0003", "ended": False})
        # the person wrote on it first: a live `conversation` binding
        rig.reg.threads[("ticket", 830)] = {
            "id": 9300, "kind": "ticket", "object_id": 830, "identity": "tester",
            "conversation_id": "surface-0003", "holder": "conversation",
            "state": "open", "pid": None, "thread_id": 470}
        # ...and the rail's local view does NOT know it (the blind-gate case)
        before = rig.spawn_count()
        rig.row(830, "ticket.mentioned", "@tester have a look", actor="alice")
        rc, out = rig.dispatch()
        check("no process was started", rig.spawn_count() == before, str(rig.spawn_count()))
        check("the rail's own gate could not see it (the blind-gate case)",
              "held by a registered interactive surface" not in out, out[-400:])
        check("so the PLANE refused it, and the rail took no for an answer",
              "not born" in out and "already holds" in out, out[-400:])
        check("the person's binding is untouched — holder still `conversation`",
              rig.reg.threads[("ticket", 830)]["holder"] == "conversation"
              and rig.reg.threads[("ticket", 830)]["conversation_id"] == "surface-0003",
              json.dumps(rig.reg.threads[("ticket", 830)])[:220])

        print("  [37] the surface says goodbye, the PLANE closes its bindings, and a holder is born")
        rig.reg.surfaces[-1]["ended"] = True
        rig.reg.threads[("ticket", 830)]["state"] = "closed"   # what the plane does on {"ended": true}
        rig.row(830, "ticket.mentioned", "@tester still there?", actor="alice")
        rc, out = rig.dispatch()
        check("now it is born", rig.verb(out, "thread-470") == "wake", out[-300:])
        check("and a process exists for it", rig.spawn_count() == before + 1, str(rig.spawn_count()))
        check("the binding is the holder's now, which is correct once nobody is listening",
              rig.reg.threads[("ticket", 830)]["holder"] == "stream",
              json.dumps(rig.reg.threads[("ticket", 830)])[:220])

        print("  [38] a runtime that reports its own failure in-band posts nothing (adapter 1.1.0)")
        # Hermes prints "Custom endpoint reported it was overloaded … Provider
        # said: HTTP 503" to stdout and exits 0. On 2026-09-21 the adapter
        # posted that on #803 as the seat's answer, outcome `accepted`, while
        # the exo pool was down. An exit code is the honest channel and not
        # every runtime uses it.
        (rig.t / "runtime-says").write_text(
            "Custom endpoint reported it was overloaded on all 3 attempts. Provider said: HTTP 503")
        rt_env_fail = dict(rt_env, RAIL_RUNTIME_FAIL_RE=r"(?i)provider said: HTTP \d{3}|overloaded")
        before = len(rig.reg.comments)
        rig.reg.tickets[840] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(480, "ticket:840", title="the pool is down")
        rig.row(840, "ticket.created", "have a look", actor="alice", assignee="tester")
        rc, out = rig.dispatch(**rt_env_fail)
        time.sleep(2.5)
        check("nothing was posted", len(rig.reg.comments) == before,
              json.dumps(rig.reg.comments[-2:])[:200])
        j = [json.loads(l) for l in
             (rig.state / "threads/thread-480/adapter.jsonl").read_text().splitlines()] \
            if (rig.state / "threads/thread-480/adapter.jsonl").exists() else []
        check("the turn is journalled `unavailable`, not `accepted`",
              bool(j) and j[-1]["outcome"] == "unavailable", json.dumps(j[-1:])[:240])
        check("and the journal carries what it said instead",
              bool(j) and "HTTP 503" in (j[-1].get("unposted") or ""), json.dumps(j[-1:])[:240])
        print("  [39] …and still posts nothing when the runtime ALSO exits non-zero (adapter 1.1.1)")
        # The seat declares what its runtime's failures look like, and that
        # declaration is about the text, not the exit code. Checking FAIL_RE
        # only on a clean exit meant a runtime that printed its failure AND
        # exited 2 was posted as an answer — the 2026-09-21 thread-1694 turn,
        # `runtime exited 2 after 299s`, whose reply the plane then refused.
        (rig.t / "runtime-exit").write_text("2")
        before = len(rig.reg.comments)
        rig.reg.tickets[841] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(481, "ticket:841", title="the pool is down, loudly")
        rig.row(841, "ticket.created", "have a look", actor="alice", assignee="tester")
        rc, out = rig.dispatch(**rt_env_fail)
        time.sleep(2.5)
        check("nothing was posted", len(rig.reg.comments) == before,
              json.dumps(rig.reg.comments[-2:])[:200])
        j2 = [json.loads(l) for l in
              (rig.state / "threads/thread-481/adapter.jsonl").read_text().splitlines()] \
            if (rig.state / "threads/thread-481/adapter.jsonl").exists() else []
        check("the failure is `unavailable`, not `accepted`, whatever the exit code",
              bool(j2) and j2[-1]["outcome"] == "unavailable", json.dumps(j2[-1:])[:240])
        (rig.t / "runtime-exit").write_text("0")
        (rig.t / "runtime-says").write_text("the turtle has read it")

        print("  [40] overlapping adapter turns: a waiter's exit does not close a running turn, and a queued message is answered (rail 3.13.1, adapter 1.1.2, #832)")
        # #817, 2026-09-25: a second delivery started a second adapter
        # that waited on the turn lock for 1800s, gave up ("not answered"), and its
        # exit closed the thread's binding while the first turn ran on. The rail
        # recorded only the newest pid, and the wait was shorter than a turn.
        (rig.t / "runtime-sleep").write_text("5")
        rig.reg.tickets[842] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(482, "ticket:842", title="two at once")
        rig.row(842, "ticket.created", "first ask", actor="alice", assignee="tester")
        started = len(turns())
        rig.dispatch(**rt_env)                               # turn A: running, 5s
        # Wait until A's runtime has actually started (the stub logs on entry), so
        # B arrives behind a turn that holds the lock. A fixed 0.5s sleep raced on
        # a loaded box: A could still be starting when B scanned live/ (#842 review).
        for _ in range(100):
            if len(turns()) > started:
                break
            time.sleep(0.1)
        rig.row(842, "ticket.commented", "second ask", actor="alice")
        rig.dispatch(**dict(rt_env, RAIL_RUNTIME_WAIT="1"))  # turn B: gives up after 1s
        time.sleep(2.0)                                      # B has exited; A still runs
        rig.dispatch(**rt_env)                               # a pass: the sweep runs
        check("the thread is still held while the first turn runs",
              rig.reg.threads[("ticket", 842)]["state"] != "closed",
              json.dumps(rig.reg.threads[("ticket", 842)])[:220])
        time.sleep(4.0)                                      # A ends
        before = len(turns())
        rig.row(842, "ticket.commented", "third ask", actor="alice")
        rig.dispatch(**rt_env)                               # C runs straight away
        rig.row(842, "ticket.commented", "fourth ask", actor="alice")
        rig.dispatch(**rt_env)                               # D queues behind C, default wait
        time.sleep(13.0)
        asked = [t["prompt"] for t in turns()[before:]]
        check("a message queued behind a running turn is answered, not dropped",
              any("fourth ask" in p for p in asked),
              json.dumps([p[-80:] for p in asked])[:300])
        # The drop on 2026-09-25 was arithmetic: a 1800s wait in front of a turn
        # allowed 5400s. The wait has to cover a whole turn, whatever the seat sets.
        probe = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util,importlib.machinery,sys;"
             "l=importlib.machinery.SourceFileLoader('a',sys.argv[1]);"
             "s=importlib.util.spec_from_loader('a',l);"
             "m=importlib.util.module_from_spec(s);l.exec_module(m);"
             "print(m.TURN_TIMEOUT, m.LOCK_WAIT)", str(adapter)],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(rig.home), "RAIL_RUNTIME_TIMEOUT": "5400"})
        tt, lw = (probe.stdout.split() + ["0", "0"])[:2]
        check("the turn-lock wait covers a whole turn (5400s turn -> wait >= 5400s)",
              int(lw) >= int(tt) == 5400, f"turn={tt} wait={lw} {probe.stderr[-200:]}")
        rig.dispatch(**rt_env)
        check("and once the last turn ends, the thread is closed",
              rig.reg.threads[("ticket", 842)]["state"] == "closed",
              json.dumps(rig.reg.threads[("ticket", 842)])[:220])
        (rig.t / "runtime-sleep").write_text("0")

        print("  [41] a turn that never started a session does not pin someone else's (adapter 1.1.3, #838)")
        # 2026-09-25 15:11: a fresh Hermes turn failed in 2s, `sessions list --limit 1`
        # still named the previous session (the 108k one that panicked the host), and
        # the adapter pinned it to the thread.
        (rig.t / "runtime-conv").write_text("20260101_000000_5ca1e0")   # someone else's, older
        (rig.t / "runtime-no-session").write_text("1")
        (rig.t / "runtime-exit").write_text("1")
        rig.reg.tickets[843] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(483, "ticket:843", title="fails before it starts")
        rig.row(843, "ticket.created", "go", actor="alice", assignee="tester")
        rig.dispatch(**rt_env)
        time.sleep(2.5)
        pin = rig.state / "threads/thread-483/conv"
        check("no conversation was pinned from a turn that made none",
              not pin.exists(), pin.read_text() if pin.exists() else "")
        (rig.t / "runtime-no-session").unlink()
        (rig.t / "runtime-exit").write_text("0")

        print("  [42] three deliveries queued behind one running turn produce ONE later turn carrying all three, and the merge is journalled (adapter 1.2.0, #842)")
        # 2026-09-27: nine deliveries on one thread queued behind an
        # in-flight turn on thread 1722 — the lock is seat-wide — and each would
        # have woken as its own turn. For a seat whose runtime is a local model
        # that is nine full prefills of a 12,288-token budget to read one
        # discussion, in series, on a pool that had wedged twice that week. The
        # lock stays seat-wide; what changes is that only ONE waiter per thread
        # queues, and the rest hand it their rows.
        (rig.t / "runtime-sleep").write_text("6")
        rig.reg.tickets[844] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(484, "ticket:844", title="four at once")
        tdir = rig.state / "threads/thread-484"
        before = len(turns())
        rig.row(844, "ticket.created", "ask ONE", actor="alice", assignee="tester")
        rig.dispatch(**rt_env)                  # turn A: takes the seat lock, 6s
        time.sleep(1.0)
        for n in ("TWO", "THREE", "FOUR"):      # all three arrive mid-turn
            rig.row(844, "ticket.commented", f"ask {n}", actor="alice")
            rig.dispatch(**rt_env)
            time.sleep(0.4)
        time.sleep(16.0)                        # A ends, the one waiter runs
        asked = [t["prompt"] for t in turns()[before:]]
        check("exactly two turns ran for four deliveries, not four",
              len(asked) == 2, f"{len(asked)} turns: "
              + json.dumps([a[-60:] for a in asked])[:300])
        check("the first turn carried only the first ask",
              bool(asked) and "ask ONE" in asked[0] and "ask THREE" not in asked[0],
              json.dumps(asked[:1])[:300])
        check("the second turn carried ALL of the queued asks",
              len(asked) > 1 and all(f"ask {n}" in asked[1]
                                     for n in ("TWO", "THREE", "FOUR")),
              json.dumps(asked[1:2])[:400])
        j3 = [json.loads(l) for l in (tdir / "adapter.jsonl").read_text().splitlines()] \
            if (tdir / "adapter.jsonl").exists() else []
        merged = [r for r in j3 if r.get("outcome") == "merged"]
        absorbed = [r for r in j3 if r.get("merged")]
        check("the deliveries answered by another turn are journalled as merged, one record each",
              len(merged) == 2, json.dumps(merged)[:300])
        check("and the turn that absorbed them says how many deliveries it answered",
              bool(absorbed) and absorbed[-1]["deliveries"] == 3
              and len(absorbed[-1]["merged"]) == 2,
              json.dumps(absorbed[-1:])[:300])
        check("the absorbing turn acked the merged rows too, so nothing is re-delivered",
              bool(absorbed) and len(absorbed[-1]["ids"]) >= 3,
              json.dumps(absorbed[-1:].__str__())[:300])
        check("the queue left nothing behind: no waiting rows, no stale markers",
              not any((tdir / "queued").iterdir() if (tdir / "queued").is_dir() else [])
              and not any((tdir / "answered").iterdir() if (tdir / "answered").is_dir() else []),
              str(sorted(x.name for x in tdir.iterdir())))
        (rig.t / "runtime-sleep").write_text("0")

        print("  [43] the merge boundary: absorb while it fits, and the delivery that would cross it waits for its own turn (adapter 1.2.0, #842)")
        # A merged turn still has to fit the runtime's context. Measured on
        # A local-model seat: real deliveries are 2.9k-7.5k tokens against a 12,288 gate,
        # so ONE merge fits with ~590 to spare and two do not. Merged unbounded, a
        # nine-deep queue builds a prompt the gateway refuses 413 — which loses the
        # content in effect while the journal says it was carried. So the bound
        # stops BEFORE the delivery that would cross it, and that delivery waits
        # for a turn of its own, exactly as it would have without merging.
        #
        # The arithmetic is driven directly: through the rail it depends on how the
        # rail happens to group rows into deliveries, which is not what this is
        # about.
        probe2 = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util,importlib.machinery,sys,os,json,pathlib;"
             "l=importlib.machinery.SourceFileLoader('a',sys.argv[1]);"
             "sp=importlib.util.spec_from_loader('a',l);"
             "m=importlib.util.module_from_spec(sp);l.exec_module(m);"
             "t=pathlib.Path(sys.argv[2]);t.mkdir(parents=True,exist_ok=True);"
             "q=m.ThreadQueue(t);me=os.getpid();"
             "row=lambda p,at:{'pid':p,'prompt':'x'*10000,'ids':[p],'chars':10000,'at':at};"
             "q.enqueue(me,row(me,0));q.enqueue(1111,row(1111,1));"
             "q.enqueue(2222,row(2222,2));q.enqueue(3333,row(3333,3));"
             "a1,o1,l1=q.take(me);"
             "a2,o2,l2=q.take(1111);"
             "a3,o3,l3=q.take(2222);"
             "print(json.dumps({'bound':m.MAX_PROMPT_CHARS,"
             "'first_answered_for':len(o1),'first_left':l1,'first_already':a1,"
             "'taken_one_skips':a2,'taken_one_others':len(o2),"
             "'next_turn_already':a3,'next_turn_answered_for':len(o3),"
             "'next_turn_left':l3}))",
             str(adapter), str(rig.t / "queue-probe")],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(rig.home),
                 "RAIL_RUNTIME_MAX_PROMPT_CHARS": "25000"})
        try:
            q = json.loads(probe2.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            q = {}
        check("the bound reached the adapter",
              q.get("bound") == 25000,
              probe2.stdout[-200:] + probe2.stderr[-300:])
        check("a turn answers for as many queued deliveries as fit (10k mine + 10k = 25k)",
              q.get("first_already") is False and q.get("first_answered_for") == 1,
              json.dumps(q))
        check("and leaves the ones that would cross the bound",
              q.get("first_left") == 2, json.dumps(q))
        check("a delivery that was answered for is told to skip its turn",
              q.get("taken_one_skips") is True and q.get("taken_one_others") == 0,
              json.dumps(q))
        check("and the ones left behind are answered by the NEXT turn, not stranded",
              q.get("next_turn_already") is False
              and q.get("next_turn_answered_for") == 1
              and q.get("next_turn_left") == 0, json.dumps(q))

        # …and end to end, where the assertion is only that nothing was lost and
        # nothing oversized was built, because the grouping is the rail's to choose.
        PAD = "x" * 9000
        BOUND = "25000"
        (rig.t / "runtime-sleep").write_text("9")
        rig.reg.tickets[845] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(485, "ticket:845", title="bounded")
        tdir5 = rig.state / "threads/thread-485"
        rt_bounded = dict(rt_env, RAIL_RUNTIME_MAX_PROMPT_CHARS=BOUND)
        before = len(turns())
        rig.row(845, "ticket.created", "ask ONE " + PAD, actor="alice", assignee="tester")
        rig.dispatch(**rt_bounded)                  # turn A: holds the seat lock, 9s
        time.sleep(1.0)
        for n in ("TWO", "THREE", "FOUR"):
            rig.row(845, "ticket.commented", f"ask {n} " + PAD, actor="alice")
            rig.dispatch(**rt_bounded)
            time.sleep(1.2)
        time.sleep(34.0)
        asked = [t["prompt"] for t in turns()[before:]]
        check("every ask reached the runtime — the bound drops nothing",
              all(any(f"ask {n}" in a for a in asked)
                  for n in ("ONE", "TWO", "THREE", "FOUR")),
              f"{len(asked)} turns, sizes " + json.dumps([len(a) for a in asked]))
        check("and no prompt was ever built past the bound",
              asked and all(len(a) <= int(BOUND) for a in asked),
              json.dumps([len(a) for a in asked]))
        check("it took more than one turn, so the boundary really stopped somewhere",
              len(asked) > 1, str(len(asked)))
        j4 = [json.loads(l) for l in (tdir5 / "adapter.jsonl").read_text().splitlines()] \
            if (tdir5 / "adapter.jsonl").exists() else []
        check("the journal names the bound when a turn had to leave something behind",
              all(r.get("prompt_bound") == int(BOUND)
                  for r in j4 if r.get("left_for_next_turn")),
              json.dumps(j4)[:500])
        (rig.t / "runtime-sleep").write_text("0")

        print("  [44] RAIL_RUNTIME_LEASE: the turn takes a pool lease and gives it back, even when the runtime fails (adapter 1.2.1, #843)")
        # Until 1.2.1 neither half worked and nothing here could see it. The
        # acquire sent `duration_seconds`/`purpose` — LeaseRequest is a StrictModel,
        # so the plane answered 422 on every turn and the adapter logged "refused …
        # running without it", which reads like a transient. The release sent
        # DELETE /resources/<r>/leases/<id>, where {resource_id:path} swallows the
        # tail and the plane answers "Resource …/leases/<id> not found" — it never
        # addressed a lease. Both confirmed against the live plane on 2026-09-27;
        # the stub above now refuses exactly what the plane refuses, which is what
        # makes this cell able to fail.
        rt_leased = dict(rt_env, RAIL_RUNTIME_LEASE="tester/pool")
        rig.reg.tickets[846] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(486, "ticket:846", title="leased")
        rig.row(846, "ticket.created", "answer under a lease", actor="alice", assignee="tester")
        rig.dispatch(**rt_leased)
        time.sleep(3.0)
        check("the plane refused nothing — the body is the one LeaseRequest accepts",
              rig.reg.lease_rejects == [], json.dumps(rig.reg.lease_rejects)[:300])
        check("a lease was taken on the resource the seat declared",
              len(rig.reg.leases) == 1
              and list(rig.reg.leases.values())[0]["resource"] == "tester/pool",
              json.dumps(rig.reg.leases)[:300])
        lid = next(iter(rig.reg.leases), "")
        check("exclusive, with a TTL, because that is what a turn on a single-slot pool needs",
              bool(lid) and rig.reg.leases[lid]["body"].get("exclusive") is True
              and rig.reg.leases[lid]["body"].get("ttl_seconds"),
              json.dumps(rig.reg.leases)[:300])
        check("and it was released, by lease id",
              bool(lid) and rig.reg.leases[lid]["status"] == "released"
              and ("DELETE", f"/api/v1/leases/{lid}", {}) in rig.reg.calls,
              json.dumps([c for c in rig.reg.calls if c[0] == "DELETE"])[:300])

        # The release lives in a `finally`, and this is the run that proves it: a
        # runtime that fails must not leave the pool leased behind it. That is the
        # shape of #827 — a lease outliving its holder — and the whole reason a
        # turn lease is worth having at all.
        (rig.t / "runtime-exit").write_text("1")
        rig.reg.tickets[847] = {"status": "open", "assignee": "tester"}
        rig.reg.declare_thread(487, "ticket:847", title="leased and failing")
        rig.row(847, "ticket.created", "fail under a lease", actor="alice", assignee="tester")
        rig.dispatch(**rt_leased)
        time.sleep(3.0)
        check("a failing turn still took its lease",
              len(rig.reg.leases) == 2, json.dumps(rig.reg.leases)[:300])
        check("and still gave it back — nothing is left leased by a turn that failed",
              all(v["status"] == "released" for v in rig.reg.leases.values()),
              json.dumps(rig.reg.leases)[:300])
        (rig.t / "runtime-exit").write_text("0")

    finally:
        rig.cleanup()

    print()
    if failed:
        print(f"  FAILED: {failed} failed, {passed} passed")
        sys.exit(1)
    print(f"  ALL PASSED: rail lifecycle {passed} passed, 0 failed")


if __name__ == "__main__":
    main()
