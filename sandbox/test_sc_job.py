#!/usr/bin/env python3
"""sc-job against a fake plane and a fake service on loopback (ticket #1157).

The caller helper submits, polls through blips, downloads, and with --store
mints a grant and revokes it on every way out except the two that leave the job
running (--no-wait after a good submit, Ctrl-C while waiting). It must refuse a
results path that leaves the job's files (and a link inside --out), never send
the bearer to another origin, and exit 1 on a failed, cancelled or expired job.
Run: python3 sandbox/test_sc_job.py
"""
import importlib.machinery
import importlib.util
import hashlib
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = Path(os.environ.get("SC_JOB_TEST_HELPER") or ROOT / "registry/helpers/samcloud-job")   # a copy, for mutation runs
TOKEN = "Bearer sc-job-test-token"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"  [{str(detail)[:400]}]"))


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.plane, self.service, self.other = [], [], []
        self.polls, self.bodies = {}, {}
        self.grants, self.revoked = {}, set()
        self.posts, self.gets = {}, {}
        self.secrets = []
        self.next_grant = 20


S = State()


def grant_secret(gid):
    """Shaped like the plane's own ("sc_grant_" + token_urlsafe(32): 43 of [A-Za-z0-9_-]), with a
    '-' and a '_' in it, so a pattern missing either can't hide it."""
    return "sc_grant_" + (f"{gid:04d}-Ab_" + hashlib.sha256(str(gid).encode()).hexdigest())[:43]


def leaked(text):
    """Any part of a grant this run minted, in text sc-job printed."""
    return any(s[9:21] in text or s[-14:] in text for s in S.secrets)


def server(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Base(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def send(self, code, obj=None, raw=None, headers=()):
        data = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Plane(Base):
    def handle_any(self, method):
        body = self.body() if method == "POST" else None
        with S.lock:
            S.plane.append((method, self.path, self.headers.get("Authorization"), body))
        if self.headers.get("Authorization") != TOKEN:
            return self.send(401, {"detail": "no"})
        p = self.path
        if method == "GET" and p == "/api/v1/services":
            return self.send(200, [
                {"id": "dev/render", "last_health_status": 200, "external_url": SVC_URL,
                 "description": "Renders \x1b[31mthings\x1b[0m. Second sentence."},
                {"id": "dev/plain", "external_url": "http://example.invalid", "description": "Plain."},
                {"id": "dev/none", "external_url": None, "description": "Nowhere."}])
        if method == "GET" and p == "/api/v1/services/dev%2Frender":
            return self.send(200, {"id": "dev/render", "external_url": SVC_URL})
        if method == "GET" and p == "/api/v1/services/dev%2Fclosed":
            return self.send(200, {"id": "dev/closed", "external_url": f"http://127.0.0.1:{CLOSED_PORT}"})
        if method == "GET" and p == "/api/v1/services/dev%2Fplain":
            return self.send(200, {"id": "dev/plain", "external_url": "http://example.invalid"})
        if method == "POST" and p == "/api/v1/grants":
            with S.lock:
                gid = S.next_grant
                S.next_grant += 1
                S.grants[gid] = body["group_scope"]
            if body["group_scope"] == "nogrant":     # a mint answer missing its secret
                return self.send(200, {"id": gid, "expires_at": "later"})
            secret = grant_secret(gid)
            with S.lock:
                S.secrets.append(secret)
            if body["group_scope"] == "oddid":       # an id that isn't a plain number
                return self.send(200, {"id": "7/../x", "grant": secret, "expires_at": "later"})
            return self.send(200, {"id": gid, "grant": secret, "expires_at": "later"})
        if method == "DELETE" and p.startswith("/api/v1/grants/"):
            tail = p[len("/api/v1/grants/"):]
            if not tail.isdigit():
                return self.send(404, {"detail": "no such grant"})
            gid = int(tail)
            if S.grants.get(gid) == "flaky":
                return self.send(503, {"detail": "down for a moment"})
            if S.grants.get(gid) == "flakyonce":
                with S.lock:
                    S.gets[("revoke", gid)] = S.gets.get(("revoke", gid), 0) + 1
                    first = S.gets[("revoke", gid)] == 1
                if first:
                    return self.send(503, {"detail": "down for a moment"})
            if S.grants.get(gid) == "slowrevoke":
                time.sleep(1.5)
            S.revoked.add(gid)
            return self.send(200, {"id": gid, "revoked_at": "now"})
        if method == "GET" and p.startswith("/api/v1/files/"):
            fid = p.rsplit("/", 1)[1]
            if fid == "999":
                return self.send(404, {"detail": "no such file"})
            if fid == "101":
                return self.send(200, {"id": 101, "name": "sheet.png", "content_type": "image/png",
                                       "markdown": "![sheet.png](/api/v1/files/101/raw)"})
            if fid == "102":
                return self.send(200, {"id": 102, "name": "a-01.jpg", "content_type": "image/jpeg"})
            if fid == "103":
                return self.send(200, {"id": 103, "name": "x.png", "content_type": "image/png",
                                       "markdown": "![x](https://elsewhere.example/x.png)"})
            plane_md = {"104": "![\x1b[31mred](/api/v1/files/104/raw)",      # its own, but not printable
                        "105": "![" + "a" * 400 + "](/api/v1/files/105/raw)",  # its own, but too long
                        "106": "![Contact sheet](/api/v1/files/106/raw)"}       # its own, and not what sc-job builds
            names = {"104": "c.png", "105": "l.png", "106": "s.png", "107": "a\x1b[31mb\x07c.png", "108": "x[1].png"}
            if fid in names:
                f = {"id": int(fid), "name": names[fid], "content_type": "text/plain" if fid == "108" else "image/png"}
                if fid in plane_md:
                    f["markdown"] = plane_md[fid]
                return self.send(200, f)
            if 200 <= int(fid) < 300:
                return self.send(200, {"id": int(fid), "name": f"n{fid}.json", "content_type": "application/json"})
            return self.send(200, {"id": int(fid), "content_type": "application/json"})
        return self.send(404, {"detail": f"plane has no {method} {p}"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_DELETE(self):
        self.handle_any("DELETE")


SCENARIOS = {"ok": "J1", "fail": "J2", "escape": "J3", "redirect": "J4", "cancel": "J5", "link": "J6",
             "blips": "J7", "slow": "J8", "odd": "J9", "canceled": "J10", "flaky": "J11", "apiredir": "J13",
             "rate": "J14", "truncated": "J15", "lost404": "J16", "lost": "J17", "nostatus": "J18", "unicode": "J19",
             "echo": "J20", "leaky": "J38", "leakyfail": "J39", "embeds": "J40", "many": "J41",
             "percent": "J42", "flap": "J43", "heartbeat": "J44", "queue": "J45", "gaps": "J46",
             "absdup": "J47", "failres": "J48", "busy": "J21", "stuck": "J22", "longrun": "J23", "blank": "J24", "error": "J25",
             "big": "J27", "chunked": "J28", "shortbig": "J29", "dropdl": "J30", "truncdl": "J31",
             "resetdl": "J32", "slowdl": "J33", "patchy": "J34", "mixed": "J35", "list": "J36",
             "inching": "J37"}
REPLIES = {"badjob": {"job": "x/evil1"}, "badpoll": {"job": "J12", "poll": "/jobs/../evilpoll"}}
FILES = {"/jobs/J1/files/sheet.png": b"PNGDATA", "/jobs/J1/files/stills/a-01.jpg": b"JPGDATA",
         "/jobs/J6/files/link/x.txt": b"LINKED", "/jobs/J7/files/out.txt": b"AFTERBLIPS",
         "/jobs/J11/files/out.txt": b"FLAKYOK", "/jobs/J14/files/out.txt": b"AFTER429",
         "/jobs/J47/files/abs.txt": b"ABS", "/jobs/J47/files/dup.txt": b"DUP", "/jobs/J48/files/x.txt": b"NO",
         "/jobs/J19/files/my sheet é.png": b"SPACED", "/jobs/J19/files/stills/café 01.jpg": b"ACCENTED"}


BIG = bytes(range(256)) * 800                  # 204800 bytes, not all one value


def job_doc(job, n):
    if job == "J1":
        if n < 2:
            return {"job": job, "status": ["queued", "running"][n], "stage": "waiting" if n == 0 else None}
        return {"job": job, "status": "done", "waited_s": 3, "render_s": 4,
                "results": {"sheet": f"/jobs/{job}/files/sheet.png", "note": "plain text, not a file",
                            "cameras": [{"shots": [{"still": f"/jobs/{job}/files/stills/a-01.jpg"}]}],
                            "elsewhere": "/jobs/OTHER/files/x.png"},
                "plane_files": {"missing": 999, "sheet": 101, "stills": {"a-01.jpg": 102}, "odd": "²", "lure": 103}}
    if job == "J2":
        return {"job": job, "status": "failed", "error": "boom \x1b]0;pwned\x07here"}
    if job == "J3":
        return {"job": job, "status": "done", "results": {"x": f"/jobs/{job}/files/../../../escape.txt"}}
    if job == "J4":
        return {"job": job, "status": "done", "results": [f"/jobs/{job}/files/r.bin"]}
    if job == "J5":
        return {"job": job, "status": "cancelled"}
    if job == "J6":
        return {"job": job, "status": "done", "results": [f"/jobs/{job}/files/link/x.txt"]}
    if job == "J7":
        return {"job": job, "status": "done", "results": [f"/jobs/{job}/files/out.txt"]}
    if job == "J8":
        return {"job": job, "status": "running", "stage": "rendering"}
    if job == "J9":
        return {"job": job, "status": "rendering" if n < 4 else "expired"}
    if job == "J10":
        return {"job": job, "status": "canceled"}
    if job in ("J11", "J14", "J7"):
        return {"job": job, "status": "done", "results": [f"/jobs/{job}/files/out.txt"]}
    if job in ("J30", "J31", "J32", "J33"):
        return {"job": job, "status": "done", "results": [f"/jobs/{job}/files/f.bin"]}
    if job == "J34":                              # 404s in fours, a good poll between: never five in a row
        return {"job": job, "status": "running"} if n == 4 else {"job": job, "status": "done", "results": []}
    if job == "J35":                              # two unfamiliar, running, one unfamiliar: never three in a row
        return {"job": job, "status": ["rendering", "rendering", "running", "rendering"][n]} if n < 4 else \
               {"job": job, "status": "done", "results": []}
    if job == "J36":                              # JSON, but not an object, twice
        return [] if n < 2 else {"job": job, "status": "done", "results": []}
    if job in ("J27", "J28", "J29"):
        return {"job": job, "status": "done", "results": [f"/jobs/{job}/files/big.bin"]}
    if job == "J15":
        return {"job": job, "status": "done", "results": [f"/jobs/{job}/files/big.bin"]}
    if job == "J16" and n >= 2:
        return {"job": job, "status": "done", "results": []}
    if job == "J18":
        return {"job": job}
    if job in ("J21", "J26"):                     # a status sc-job doesn't know, but moving
        return {"job": job, "status": "processing", "stage": f"frame {n}"} if n < 30 else \
               {"job": job, "status": "done", "results": []}
    if job == "J37":                              # the same unfamiliar status and stage; only progress moves
        return {"job": job, "status": "processing", "stage": "frames", "progress": n / 30} if n < 30 else \
               {"job": job, "status": "done", "results": []}
    if job == "J22":                              # the same unfamiliar answer, for ever
        return {"job": job, "status": "processing", "stage": "frames"}
    if job == "J23":                              # running, unchanged, for longer than the cap
        return {"job": job, "status": "running"} if n < 30 else {"job": job, "status": "done", "results": []}
    if job == "J24":                              # no status, though something changes
        return {"job": job, "stage": f"frame {n}"}
    if job == "J25":
        return {"job": job, "status": "error", "error": "out of memory"}
    if job == "J47":                              # one file named by absolute URL, one named twice
        return {"job": job, "status": "done",
                "results": [f"{SVC_URL}/jobs/{job}/files/abs.txt", f"/jobs/{job}/files/dup.txt", f"/jobs/{job}/files/dup.txt"]}
    if job == "J48":                              # failed, though it names a file
        return {"job": job, "status": "failed", "error": "no", "results": [f"/jobs/{job}/files/x.txt"]}
    if job == "J46":                              # 15 with no status, a running, 15 more: never 20 in a row
        if n == 15:
            return {"job": job, "status": "running"}
        return {"job": job} if n < 31 else {"job": job, "status": "done", "results": []}
    if job == "J42":                              # moving only in a field the wait never names
        return {"job": job, "status": "processing", "stage": "frames", "percent": n} if n < 30 else \
               {"job": job, "status": "done", "results": []}
    if job == "J43":                              # two unfamiliar statuses, alternating for ever
        return {"job": job, "status": "ab"[n % 2]}
    if job == "J44":                              # stuck, but its clock fields tick
        return {"job": job, "status": "processing", "updated_at": f"T{n}", "run_s": n, "heartbeat": n,
                "detail": {"checked_at": n}}
    if job == "J45":                              # an unfamiliar status; only the queue position moves
        return {"job": job, "status": "waiting", "position": 30 - n} if n < 30 else \
               {"job": job, "status": "done", "results": []}
    if job == "J40":
        return {"job": job, "status": "done", "results": [],
                "plane_files": {"ctl": 104, "long": 105, "own": 106, "dirty": 107, "brackets": 108}}
    if job == "J41":
        return {"job": job, "status": "done", "results": [], "plane_files": {f"f{i:02d}": 200 + i for i in range(26)}}
    if job in ("J38", "J39"):                     # the grant it was given, echoed wherever sc-job prints
        grant = (S.bodies.get(job) or {}).get("grant", "")
        if job == "J39":
            return {"job": job, "status": "failed", "error": f"refused grant {grant}"}
        if n == 0:
            return {"job": job, "status": "running", "stage": f"checking {grant}"}
        return {"job": job, "status": "done", "plane_error": f"upload refused for {grant}",
                "results": [f"/jobs/{job}/files/{grant}.txt", f"/jobs/{job}/files/sc_grant_report-2026.txt"]}
    if job == "J20":                              # echoes the request it was given, grant and all
        return {"job": job, "status": "done", "results": [], "request": S.bodies.get(job)}
    if job == "J19":
        return {"job": job, "status": "done",
                "results": [f"/jobs/{job}/files/my%20sheet%20%C3%A9.png", f"/jobs/{job}/files/stills/café 01.jpg"]}
    return None


class Service(Base):
    def do_POST(self):
        body = self.body()
        with S.lock:
            S.service.append(("POST", self.path, self.headers.get("Authorization"), body))
        scenario = (body or {}).get("scenario")
        with S.lock:
            S.posts[scenario] = S.posts.get(scenario, 0) + 1
        if scenario == "strict422":              # a model that forbids extra fields echoes the grant
            return self.send(422, {"detail": [{"type": "extra_forbidden", "loc": ["body", "grant"],
                                               "msg": "Extra inputs are not permitted", "input": body.get("grant")},
                                              {"type": "also", "input": body.get("grant")}]})
        if scenario == "submit500":
            return self.send(500, {"error": "can't take it"})
        if scenario == "submit503":              # counted on its own: the 5xx-is-not-re-sent check
            return self.send(503, {"error": "busy"})
        if scenario == "dropsubmit":             # took the body, then the line went dead
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if scenario in REPLIES:
            return self.send(202, REPLIES[scenario])
        job = SCENARIOS.get(scenario)
        if self.path != "/render" or not job:
            return self.send(404, {"error": "no"})
        S.bodies[job] = body
        reply = {"job": job, "status": "queued"}
        if job != "J5":
            reply["poll"] = f"/jobs/{job}"
        return self.send(202, reply)

    def do_GET(self):
        with S.lock:
            S.service.append(("GET", self.path, self.headers.get("Authorization"), None))
        if "evil" in self.path:                   # answers anything, so only sc-job's own check refuses
            return self.send(200, {"job": "evil", "status": "done", "results": []})
        parts = self.path.split("/")
        if len(parts) == 3 and parts[1] == "jobs":
            with S.lock:
                n = S.polls.get(parts[2], 0)
                S.polls[parts[2]] = n + 1
            if parts[2] == "J7" and n < 6:
                return self.send(502, {"error": "bad gateway"})
            if parts[2] == "J7" and n == 6:
                self.close_connection = True       # drop it: no status line at all
                return
            if parts[2] == "J14" and n < 6:
                return self.send(429, {"error": "slow down"}, headers=[("Retry-After", "0")])
            if parts[2] == "J13":
                return self.send(302, raw=b"", headers=[("Location", f"{OTHER_URL}/evilredir")])
            if parts[2] == "J34" and n != 4 and n < 9:
                return self.send(404, {"error": "no such job (yet)"})
            if parts[2] in ("J16", "J17") and (parts[2] == "J17" or n < 2):
                return self.send(404, {"error": "no such job (yet)"})
            d = job_doc(parts[2], n)
            return self.send(200, d) if d is not None else self.send(404, {"error": "no such job"})
        if self.path.startswith("/jobs/J38/files/"):
            return self.send(200, raw=b"LEAKY")
        if self.path.startswith("/jobs/J3") and self.path.endswith("/files/f.bin"):
            with S.lock:
                n = S.gets.get(self.path, 0)
                S.gets[self.path] = n + 1
            if n == 0 and "J30" in self.path:         # first try: no status line at all
                self.close_connection = True
                return
            if n == 0 and "J31" in self.path:         # first try: short of its Content-Length
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                self.wfile.write(b"x" * 1000)
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if n == 0 and "J32" in self.path:         # first try: reset mid-body (a bare ConnectionResetError)
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                self.wfile.write(b"x" * 1000)
                self.wfile.flush()
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self.close_connection = True
                self.connection.close()
                return
            if "J33" in self.path:                    # a slow body: 1000 bytes, then nothing for 20 s
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                self.wfile.write(b"x" * 1000)
                self.wfile.flush()
                time.sleep(20)
                self.close_connection = True
                return
            return self.send(200, raw=b"SECONDTRY")
        if "escape" in self.path:
            return self.send(200, raw=b"ESCAPED")
        if self.path == "/jobs/J15/files/big.bin":   # promise 100000 bytes, send 1000, cut the line
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b"x" * 1000)
            self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if self.path == "/jobs/J27/files/big.bin":   # well past one 64 KiB read
            return self.send(200, raw=BIG)
        if self.path == "/jobs/J28/files/big.bin":   # chunked, with a Content-Length that chunking overrides
            self.send_response(200)
            self.send_header("Content-Length", "999999")    # more than the body: a check on it would fail
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(BIG), 70000):
                part = BIG[i:i + 70000]
                self.wfile.write(b"%x\r\n" % len(part) + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        if self.path == "/jobs/J29/files/big.bin":   # promise 300000, send 200000 (past the first read), cut
            self.send_response(200)
            self.send_header("Content-Length", "300000")
            self.end_headers()
            self.wfile.write(BIG[:200000])
            self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        path = urllib.parse.unquote(self.path)
        if path in FILES:
            return self.send(200, raw=FILES[path])
        if self.path == "/jobs/J4/files/r.bin":
            return self.send(302, raw=b"", headers=[("Location", f"{OTHER_URL}/r.bin")])
        return self.send(404, {"error": "no file"})

    def do_DELETE(self):
        with S.lock:
            S.service.append(("DELETE", self.path, self.headers.get("Authorization"), None))
        if "evil" in self.path:
            return self.send(200, {"job": "evil", "status": "cancelled"})
        return self.send(200, {"job": self.path.rsplit("/", 1)[1], "status": "cancelled"})


class Other(Base):
    def do_GET(self):
        with S.lock:
            S.other.append((self.path, self.headers.get("Authorization")))
        return self.send(200, raw=b"REDIRECTED")


plane_srv, svc_srv, other_srv = server(Plane), server(Service), server(Other)
PLANE_URL = f"http://127.0.0.1:{plane_srv.server_port}"
SVC_URL = f"http://127.0.0.1:{svc_srv.server_port}"
OTHER_URL = f"http://localhost:{other_srv.server_port}"   # another host, and plain http
_closed = socket.socket()
_closed.bind(("127.0.0.1", 0))
CLOSED_PORT = _closed.getsockname()[1]                     # nothing listens: a connection is refused
_closed.close()


def env_for(cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SAMCLOUD_")}
    env.update({"HOME": cwd, "SAMCLOUD_API": f"{PLANE_URL}/api/v1", "SAMCLOUD_AUTH_HDR": HDR,
                "SC_JOB_POLL_S": "0.02,0.05", "SC_JOB_RETRY_S": "0.02,0.05"})
    return env


def run(args, cwd, stdin=None, timeout=30):
    try:
        return subprocess.run([sys.executable, str(HELPER), *args], cwd=cwd, env=env_for(cwd), input=stdin,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(e.cmd, None, e.stdout or "", f"(timed out after {timeout} s)")


def wait_polls(before, n=2):
    deadline = time.time() + 20
    while S.polls.get("J8", 0) < before + n and time.time() < deadline:
        time.sleep(0.02)


def interrupted(args, cwd, sig, then=None, preexec=None):
    """Start a call on the never-ending job J8, signal it once it is polling, return (proc, out, err).
    `then` is (pause, signal): a second signal after a pause."""
    before = S.polls.get("J8", 0)
    p = subprocess.Popen([sys.executable, str(HELPER), *args], cwd=cwd, env=env_for(cwd),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, preexec_fn=preexec)
    wait_polls(before)
    p.send_signal(sig)
    if then:
        time.sleep(then[0])
        p.send_signal(then[1])
    try:
        out, err = p.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
    return p, out, err


def content(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def mode(path):
    try:
        return Path(path).stat().st_mode & 0o777
    except OSError:
        return None


def minted_since(mark):
    return [g for g in S.grants if g >= mark]


def requests_to(fragment):
    return [p for _m, p, _a, _b in S.service if fragment in p]


def load():
    loader = importlib.machinery.SourceFileLoader("scjob", str(HELPER))
    spec = importlib.util.spec_from_loader("scjob", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def raises(fn, exc=SystemExit):
    try:
        fn()
    except exc:
        return True
    except BaseException:
        return False
    return False


def unit(m, tmp):
    print("sc-job — guards, one by one")
    check("a job id passes", m.job_id("1125-003713-32180d") == "1125-003713-32180d")
    check("a job id with '..' or a slash is refused", raises(lambda: m.job_id("../x")) and raises(lambda: m.job_id("a/b")))
    check("a job id with a trailing newline is refused (fullmatch)", raises(lambda: m.job_id("J1\n")))
    rf = lambda path: m.result_files({"results": [path]}, "https://s", "/jobs/J")
    check("a results path with a trailing newline is refused", raises(lambda: rf("/jobs/J/files/a.png\n")))
    check("a percent-encoded '..' segment is refused", raises(lambda: rf("/jobs/J/files/%2e%2e/escape.txt")))
    check("a '.' or empty segment is refused", raises(lambda: rf("/jobs/J/files/a/./b")) and raises(lambda: rf("/jobs/J/files/a//b")))
    check("a backslash is refused", raises(lambda: rf("/jobs/J/files/a%5C..%5Cb")))
    check("a NUL is refused", raises(lambda: rf("/jobs/J/files/a%00b")))
    check("spaces and non-ASCII letters are allowed", rf("/jobs/J/files/my%20sheet%20%C3%A9.png") == ["my sheet é.png"],
          rf("/jobs/J/files/my%20sheet%20%C3%A9.png") if not raises(lambda: rf("/jobs/J/files/my%20sheet%20%C3%A9.png")) else "refused")
    try:
        flat = list(m.flatten({"x": "²", "y": "7"}))
    except Exception as e:
        flat = e
    check("flatten skips a non-ASCII digit instead of crashing", flat == [("y", 7)], flat)
    check("clean strips escape sequences and control characters",
          m.clean("a\x1b[31mred\x1b[0m\x1b]0;t\x07\x00b") == "ared b", repr(m.clean("a\x1b[31mred\x1b[0m\x1b]0;t\x07\x00b")))

    c = m.Client(f"{PLANE_URL}/api/v1", TOKEN, HDR)
    before = len(S.service)
    check("Client refuses an origin it wasn't given, before sending anything",
          raises(lambda: c.call("GET", f"{SVC_URL}/jobs/J1"), m.CallError) and len(S.service) == before)

    def redirect(handler, old, new):
        req = urllib.request.Request(old, headers={"Authorization": TOKEN})
        return handler.redirect_request(req, None, 302, "Found", {}, new)

    d = m.DownloadRedirects()
    new = redirect(d, "https://svc.example/jobs/J/files/a", "https://cdn.example/a")
    check("a download redirect to another https host drops the bearer",
          new is not None and "Authorization" not in new.headers and "Authorization" not in new.unredirected_hdrs)
    new = redirect(d, "https://svc.example/jobs/J/files/a", "https://svc.example/b")
    check("a download redirect on the same origin keeps the bearer", new is not None and new.get_header("Authorization") == TOKEN)
    check("a download redirect to plain http is refused",
          raises(lambda: redirect(d, "https://svc.example/a", "http://cdn.example/a"), m.RedirectRefused))
    a = m.ApiRedirects()
    check("an API redirect to another origin is refused",
          raises(lambda: redirect(a, "https://svc.example/jobs/J", "https://other.example/jobs/J"), m.RedirectRefused))
    check("an API redirect on the same origin is followed",
          redirect(a, "https://svc.example/jobs/J", "https://svc.example/jobs/J/") is not None)

    saved = os.environ.get("SAMCLOUD_API")
    os.environ["SAMCLOUD_API"] = "http://plane.example/api/v1"
    check("a plane on plain http off loopback is refused", raises(lambda: m.plane_api(None)))
    os.environ["SAMCLOUD_API"] = "https://plane.example"
    check("an https plane is used, with /api/v1", m.plane_api(None) == "https://plane.example/api/v1")
    if saved is None:
        os.environ.pop("SAMCLOUD_API")
    else:
        os.environ["SAMCLOUD_API"] = saved
    check("--hdr naming a missing file dies, with no fallback",
          raises(lambda: m.bearer(os.path.join(tmp, "nope.hdr"))))
    for bad, what in (("render", "no leading /"), ("//evil.example/x", "a leading //"), ("/a://b", "a ://"),
                      ("/a\\b", "a backslash"), ("/a?b", "a ?"), ("/a#b", "a #"), ("/a\nb", "a control character")):
        check(f"a service path with {what} is refused", raises(lambda: m.safe_path(bad, "path")), bad)
    hint = m.Client(f"{PLANE_URL}/api/v1", TOKEN, HDR).revoke_hint("1; rm -rf ~")
    check("the printed revoke line quotes the grant id", "'" in hint and "/grants/1; rm" in hint and " rm -rf ~'" in hint, hint)


def main():
    global HDR
    os.umask(0o022)
    # The helpers we start must not inherit an ignored SIGHUP (nohup) or SIGINT (`&` in a
    # non-interactive shell): the signal checks below would hang or crash.
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    tmp = tempfile.mkdtemp(prefix="sc-job-")
    HDR = os.path.join(tmp, "token.hdr")
    Path(HDR).write_text(f"Authorization: {TOKEN}\n")
    os.chmod(HDR, 0o600)
    unit(load(), tmp)

    print("sc-job — submit, poll, download")
    mark = S.next_grant
    r = run(["call", "dev/render", "/render", '{"scenario":"ok"}', "--store", "projects"], tmp)
    out = Path(tmp) / "J1"
    check("a done job exits 0", r.returncode == 0, r.stderr)
    check("files land in ./<job>/ by their path under the job's files",
          out.exists() and content(out / "sheet.png") == b"PNGDATA"
          and content(out / "stills/a-01.jpg") == b"JPGDATA", r.stdout + r.stderr)
    check("no temp file is left beside the downloads", not list(out.rglob(".sc-job-*")))
    check("downloads get the umask's mode (0644), not mkstemp's 0600",
          mode(out / "sheet.png") == 0o644, mode(out / "sheet.png"))
    check("a path of another job's files is not fetched", not requests_to("/jobs/OTHER"))
    check("the summary names the job, its timings and the files",
          "job J1: done · waited 3 s, render 4 s" in r.stdout and "sheet.png" in r.stdout, r.stdout)
    check("plane files are listed by role", "  sheet 101" in r.stdout and "  stills/a-01.jpg 102" in r.stdout, r.stdout)
    check("the summary says the plane files are already yours, not to re-upload",
          "already on the plane" in r.stdout and "don't upload the downloads again" in r.stdout, r.stdout)
    check("each plane file is printed with the plane's markdown",
          "  sheet 101  ![sheet.png](/api/v1/files/101/raw)" in r.stdout, r.stdout)
    check("a file with no markdown gets one built from its name and type",
          "  stills/a-01.jpg 102  ![a-01.jpg](/api/v1/files/102/raw)" in r.stdout, r.stdout)
    check("a file the plane can't read still gets a link to it",
          "  missing 999  [missing](/api/v1/files/999/raw)" in r.stdout, r.stdout)
    check("markdown pointing anywhere but that file is not printed",
          "elsewhere.example" not in r.stdout and "  lure 103  ![x.png](/api/v1/files/103/raw)" in r.stdout, r.stdout)
    g = minted_since(mark)
    check("the grant rode in the body, top-level", len(g) == 1 and S.bodies.get("J1", {}).get("grant") == grant_secret(g[0]),
          S.bodies.get("J1"))
    posted = [b for m_, p, _a, b in S.plane if m_ == "POST" and p == "/api/v1/grants"]
    check("the grant named the service, the group and the default ttl",
          posted[-1:] == [{"service_id": "dev/render", "group_scope": "projects", "ttl_s": 7200}], posted)
    check("revoked when the job is done", g and g[0] in S.revoked, (g, S.revoked))
    check("progress goes to stderr, not stdout", "waiting" in r.stderr and "waiting" not in r.stdout, r.stderr)
    check("the bearer reached the plane and the service", all(a == TOKEN for _m, _p, a, _b in S.plane + S.service))
    check("the bearer is never printed", "sc-job-test-token" not in r.stdout + r.stderr)

    r = run(["call", "dev/render", "/render", '{"scenario":"embeds"}', "--out", "o-embeds"], tmp)
    check("the plane's own markdown is used when it differs from what sc-job would build",
          "  own 106  ![Contact sheet](/api/v1/files/106/raw)" in r.stdout, r.stdout)
    check("the plane's markdown is not used when it holds an escape sequence",
          "  ctl 104  ![c.png](/api/v1/files/104/raw)" in r.stdout and "\x1b" not in r.stdout, repr(r.stdout))
    check("... nor when it is longer than 300 characters",
          "  long 105  ![l.png](/api/v1/files/105/raw)" in r.stdout and "aaaa" not in r.stdout, r.stdout)
    check("a file name is cleaned of escape sequences and control characters",
          "  dirty 107  ![ab c.png](/api/v1/files/107/raw)" in r.stdout, repr(r.stdout))
    check("brackets in a name become parentheses, and a non-image gets a plain link",
          "  brackets 108  [x(1).png](/api/v1/files/108/raw)" in r.stdout, r.stdout)
    before = len(S.plane)
    r = run(["call", "dev/render", "/render", '{"scenario":"many"}', "--out", "o-many"], tmp)
    looked = [p_ for m_, p_, _a, _b in S.plane[before:] if m_ == "GET" and p_.startswith("/api/v1/files/2")]
    check("of 26 plane files, the first 24 are looked up and the rest get a link built from their role",
          len(looked) == 24 and "  f23 223  [n223.json](/api/v1/files/223/raw)" in r.stdout
          and "  f24 224  [f24](/api/v1/files/224/raw)" in r.stdout and "  f25 225  [f25](/api/v1/files/225/raw)" in r.stdout,
          (len(looked), r.stdout[-300:]))

    print("sc-job — the grant on every way out")
    for scenario, code, what in (("fail", 1, "failed"), ("cancel", 1, "cancelled"), ("submit500", 2, "the submit failed")):
        mark = S.next_grant
        r = run(["call", "dev/render", "/render", "-", "--store", "projects", "--out", f"o-{scenario}"], tmp,
                stdin=json.dumps({"scenario": scenario}))
        g = minted_since(mark)
        check(f"revoked when {what} (exit {code})", r.returncode == code and len(g) == 1 and g[0] in S.revoked,
              (r.returncode, g, r.stdout + r.stderr))
    r = run(["call", "dev/render", "/render", '{"scenario":"fail"}', "--out", "o-f2"], tmp)
    check("the error is printed clean", "· boom here" in r.stdout and "\x1b" not in r.stdout, repr(r.stdout))

    mark = S.next_grant
    r = run(["call", "dev/render", "/render", '{"scenario":"ok"}', "--store", "nogrant", "--out", "o-nogrant"], tmp)
    g = minted_since(mark)
    check("a mint answer that can't go in the body is revoked, with no traceback (exit 2)",
          r.returncode == 2 and g and g[0] in S.revoked and "Traceback" not in r.stderr, (r.returncode, g, r.stderr))
    mark = S.next_grant
    r = run(["call", "dev/render", "/render", '{"scenario":"ok"}', "--store", "projects", "--no-wait"], tmp)
    g = minted_since(mark)
    check("--no-wait after a good submit keeps the grant and says how to resume and revoke",
          r.returncode == 0 and r.stdout.strip() == "J1" and g and g[0] not in S.revoked
          and f"sc-job fetch dev/render J1 --grant {g[0]}" in r.stderr and f"/grants/{g[0]}" in r.stderr,
          (r.stdout, r.stderr))
    if g:
        r = run(["fetch", "dev/render", "J1", "--grant", str(g[0]), "--out", "o-fetchgrant"], tmp)
        check("fetch --grant downloads, then revokes that grant",
              r.returncode == 0 and (Path(tmp) / "o-fetchgrant/sheet.png").exists() and g[0] in S.revoked,
              (r.returncode, g, r.stderr))
    mark = S.next_grant
    r = run(["call", "dev/render", "/render", '{"scenario":"submit500"}', "--store", "projects", "--no-wait"], tmp)
    g = minted_since(mark)
    check("--no-wait with a failed submit revokes the grant", r.returncode == 2 and g and g[0] in S.revoked,
          (r.returncode, g, r.stderr))

    mark = S.next_grant
    p, o, e = interrupted(["call", "dev/render", "/render", '{"scenario":"slow"}', "--store", "projects", "--out", "o-term"],
                          tmp, signal.SIGTERM)
    g = minted_since(mark)
    check("SIGTERM while waiting revokes the grant (exit 2)", p.returncode == 2 and g and g[0] in S.revoked,
          (p.returncode, g, e))
    check("... and says the job carries on, its uploads will fail, and how to fetch",
          "stopped by SIGTERM" in e and "Job J8" in e and "uploads to the plane will fail" in e
          and "sc-job fetch dev/render J8" in e and "Traceback" not in e, e)
    mark = S.next_grant
    p, o, e = interrupted(["call", "dev/render", "/render", '{"scenario":"slow"}', "--store", "projects", "--out", "o-hup"],
                          tmp, signal.SIGHUP)
    g = minted_since(mark)
    check("SIGHUP while waiting revokes the grant (exit 2)", p.returncode == 2 and g and g[0] in S.revoked
          and "stopped by SIGHUP" in e, (p.returncode, g, e))
    mark = S.next_grant
    before = S.polls.get("J8", 0)
    p = subprocess.Popen([sys.executable, str(HELPER), "call", "dev/render", "/render", '{"scenario":"slow"}',
                          "--store", "projects", "--out", "o-nohup"], cwd=tmp, env=env_for(tmp),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN))
    wait_polls(before)
    p.send_signal(signal.SIGHUP)
    time.sleep(0.5)
    alive, polled = p.poll() is None, S.polls.get("J8", 0)
    time.sleep(0.3)
    still_polling = S.polls.get("J8", 0) > polled
    p.send_signal(signal.SIGINT)
    o, e = p.communicate(timeout=30)
    g = minted_since(mark)
    check("under nohup (SIGHUP ignored) a SIGHUP changes nothing: it keeps waiting, the grant stays",
          alive and still_polling and g and g[0] not in S.revoked and "SIGHUP" not in e, (alive, still_polling, g, e))
    mark = S.next_grant
    p, o, e = interrupted(["call", "dev/render", "/render", '{"scenario":"slow"}', "--store", "slowrevoke", "--out", "o-twice"],
                          tmp, signal.SIGTERM, then=(0.4, signal.SIGTERM))
    g = minted_since(mark)
    check("a second SIGTERM during the revoke is ignored: the revoke finishes and the hint is printed",
          p.returncode == 2 and g and g[0] in S.revoked and "resume:  sc-job fetch dev/render J8" in e, (p.returncode, g, e))
    mark = S.next_grant
    mark = S.next_grant
    before = S.polls.get("J8", 0)
    p = subprocess.Popen([sys.executable, str(HELPER), "call", "dev/render", "/render", '{"scenario":"slow"}',
                          "--store", "projects", "--out", "o-amp"], cwd=tmp, env=env_for(tmp),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
    wait_polls(before)
    p.send_signal(signal.SIGINT)
    time.sleep(0.5)
    alive, polled = p.poll() is None, S.polls.get("J8", 0)
    time.sleep(0.3)
    still_polling = S.polls.get("J8", 0) > polled
    p.send_signal(signal.SIGTERM)
    o, e = p.communicate(timeout=30)
    g = minted_since(mark)
    check("started with SIGINT ignored (`&`), a SIGINT changes nothing: it keeps waiting, the grant stays",
          alive and still_polling and "stopped waiting" not in e and g and "stopped by SIGTERM" in e,
          (alive, still_polling, g, e[-300:]))
    for sig in (signal.SIGINT, signal.SIGTERM):
        mark = S.next_grant
        p = subprocess.Popen([sys.executable, str(HELPER), "call", "dev/render", "/render", '{"scenario":"ok"}',
                              "--store", "slowrevoke", "--out", f"o-done-{sig.name}"], cwd=tmp, env=env_for(tmp),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 20
        while time.time() < deadline and not [x for x in S.plane if x[0] == "DELETE" and x[1].startswith("/api/v1/grants/")
                                              and int(x[1].rsplit("/", 1)[1]) >= mark]:
            time.sleep(0.02)
        time.sleep(0.3)
        p.send_signal(sig)
        o, e = p.communicate(timeout=30)
        check(f"a {sig.name} during the revoke after a done job is ignored: the files still land (exit 0)",
              p.returncode == 0 and content(Path(tmp) / f"o-done-{sig.name}/sheet.png") == b"PNGDATA",
              (p.returncode, e[-300:]))
    def both(first, second, pause=0.0, preexec=None):
        mark = S.next_grant
        before = S.polls.get("J8", 0)
        p = subprocess.Popen([sys.executable, str(HELPER), "call", "dev/render", "/render", '{"scenario":"slow"}',
                              "--store", "projects", "--out", "o-both"], cwd=tmp, env=env_for(tmp),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, preexec_fn=preexec)
        wait_polls(before)
        p.send_signal(first)
        if pause:
            time.sleep(pause)
        p.send_signal(second)
        o, e = p.communicate(timeout=30)
        g = minted_since(mark)
        return p.returncode, bool(g) and g[0] in S.revoked, e

    def stop_won(code, revoked, e):
        return (code == 2 and revoked and "stopped by SIGTERM" in e and "stays live" not in e
                and "resume:  sc-job fetch dev/render J8" in e and "Traceback" not in e)

    for trial in range(2):
        code, revoked, e = both(signal.SIGTERM, signal.SIGINT)
        check(f"SIGTERM then SIGINT (try {trial + 1}): the stop wins: revoked, the fetch line, no 'stays live' (exit 2)",
              stop_won(code, revoked, e), (code, revoked, e[-400:]))
    code, revoked, e = both(signal.SIGTERM, signal.SIGINT,
                            preexec=lambda: signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM}))
    check("a SIGTERM already pending when Ctrl-C is handled wins: revoked, no 'stays live' (exit 2)",
          stop_won(code, revoked, e), (code, revoked, e[-400:]))
    for pause in (0.0, 0.0, 0.02, 0.1, 0.4):
        code, revoked, e = both(signal.SIGINT, signal.SIGTERM, pause)
        kept = (code == 2 and not revoked and "stays live" in e and "stopped by" not in e
                and "resume:  sc-job fetch dev/render J8 --grant" in e and "Traceback" not in e)
        check(f"SIGINT then SIGTERM {pause} s later: what is printed holds, either way (exit 2)",
              stop_won(code, revoked, e) or kept, (code, revoked, e[-400:]))
    mark = S.next_grant
    p, o, e = interrupted(["call", "dev/render", "/render", '{"scenario":"slow"}', "--store", "slowrevoke", "--out", "o-tint"],
                          tmp, signal.SIGTERM, then=(0.4, signal.SIGINT))
    g = minted_since(mark)
    check("a Ctrl-C during the revoke is ignored: the revoke finishes and the hint is printed",
          p.returncode == 2 and g and g[0] in S.revoked and "resume:  sc-job fetch dev/render J8" in e
          and "interrupted" not in e, (p.returncode, g, e))
    p, o, e = interrupted(["fetch", "dev/render", "J8", "--grant", "903", "--out", "o-fterm"], tmp, signal.SIGTERM)
    check("SIGTERM during fetch --grant revokes that grant and prints the fetch line (exit 2)",
          p.returncode == 2 and 903 in S.revoked and "stopped by SIGTERM" in e
          and "resume:  sc-job fetch dev/render J8" in e, (p.returncode, e))
    mark = S.next_grant
    p, o, e = interrupted(["call", "dev/render", "/render", '{"scenario":"slow"}', "--store", "projects", "--out", "o-int"],
                          tmp, signal.SIGINT)
    g = minted_since(mark)
    check("Ctrl-C while waiting keeps the grant (exit 2)", p.returncode == 2 and g and g[0] not in S.revoked,
          (p.returncode, g, e))
    check("... and prints the job, the grant, how to resume and how to revoke",
          "J8" in e and f"grant {g[0] if g else '?'}" in e and f"sc-job fetch dev/render J8 --grant {g[0] if g else '?'}" in e
          and f"-X DELETE {PLANE_URL}/api/v1/grants/" in e and "Traceback" not in e, e)
    p, o, e = interrupted(["fetch", "dev/render", "J8", "--grant", "901", "--out", "o-fint"], tmp, signal.SIGINT)
    check("Ctrl-C during fetch --grant keeps that grant (exit 2) and prints the fetch line with it",
          p.returncode == 2 and 901 not in S.revoked and "sc-job fetch dev/render J8 --grant 901" in e, (p.returncode, e))
    p, o, e = interrupted(["call", "dev/render", "/render", '{"scenario":"slow"}', "--out", "o-int2"], tmp, signal.SIGINT)
    check("Ctrl-C without --store says how to fetch", p.returncode == 2 and "sc-job fetch dev/render J8" in e, e)

    mark = S.next_grant
    r = run(["call", "dev/render", "/render", '{"scenario":"flaky"}', "--store", "flaky", "--out", "o-flaky"], tmp)
    g = minted_since(mark)
    check("a revoke that 503s still downloads and summarises (exit 0)",
          r.returncode == 0 and content(Path(tmp) / "o-flaky/out.txt") == b"FLAKYOK" and "job J11: done" in r.stdout,
          r.stdout + r.stderr)
    check("... and prints the grant id and the revoke command",
          g and f"could not revoke grant {g[0]}" in r.stderr and f"-X DELETE {PLANE_URL}/api/v1/grants/{g[0]}" in r.stderr,
          r.stderr)

    print("sc-job — polling through blips, and odd statuses")
    r = run(["call", "dev/render", "/render", '{"scenario":"blips"}', "--out", "o-blips"], tmp)
    check("a 502 and then a dropped connection are waited out",
          r.returncode == 0 and content(Path(tmp) / "o-blips/out.txt") == b"AFTERBLIPS", r.stdout + r.stderr)
    check("... with one stderr line for the streak", r.stderr.count("retrying") == 1, r.stderr)
    r = run(["call", "dev/render", "/render", '{"scenario":"rate"}', "--out", "o-rate"], tmp)
    check("six 429s in a row are waited out", r.returncode == 0 and content(Path(tmp) / "o-rate/out.txt") == b"AFTER429",
          r.stdout + r.stderr)
    r = run(["call", "dev/render", "/render", '{"scenario":"lost404"}', "--out", "o-l404"], tmp)
    check("two 404s while polling don't end the wait", r.returncode == 0 and "job J16: done" in r.stdout, r.stdout + r.stderr)
    mark = S.next_grant
    before = S.polls.get("J17", 0)
    r = run(["call", "dev/render", "/render", '{"scenario":"lost"}', "--store", "projects"], tmp)
    g = minted_since(mark)
    check("a job that keeps answering 404 stops the wait (exit 2), keeps the grant and prints the fetch line",
          r.returncode == 2 and g and g[0] not in S.revoked and f"sc-job fetch dev/render J17 --grant {g[0]}" in r.stderr,
          (r.returncode, g, r.stderr))
    check("... after exactly 5 failed polls", S.polls.get("J17", 0) - before == 5, S.polls.get("J17", 0) - before)
    r = run(["call", "dev/render", "/render", '{"scenario":"gaps"}'], tmp)
    check("a 'running' between two runs of 15 polls with no status resets the count: waited out (exit 0)",
          r.returncode == 0 and "job J46: done" in r.stdout, (r.returncode, S.polls.get("J46"), r.stderr[-300:]))
    r = run(["fetch", "dev/render", "J17", "--grant", "902"], tmp)
    check("fetch --grant on a job that keeps answering 404 stops (exit 2) and keeps that grant",
          r.returncode == 2 and 902 not in S.revoked and "sc-job fetch dev/render J17 --grant 902" in r.stderr,
          (r.returncode, r.stderr))
    mark = S.next_grant
    r = run(["call", "dev/render", "/render", '{"scenario":"nostatus"}', "--store", "projects"], tmp, timeout=15)
    g = minted_since(mark)
    check("a job with no status stops the wait after 20 polls (exit 2), keeping the grant",
          r.returncode == 2 and g and g[0] not in S.revoked and f"sc-job fetch dev/render J18 --grant {g[0]}" in r.stderr
          and S.polls.get("J18") == 20, (r.returncode, g, S.polls.get("J18"), r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"busy"}'], tmp)
    check("an unfamiliar status whose stage keeps changing is waited out past 20 polls (exit 0)",
          r.returncode == 0 and "job J21: done" in r.stdout and S.polls.get("J21", 0) > 20,
          (r.returncode, S.polls.get("J21"), r.stderr[-300:]))
    r = run(["fetch", "dev/render", "J26"], tmp)
    check("... and so is it by fetch", r.returncode == 0 and "job J26: done" in r.stdout and S.polls.get("J26", 0) > 20,
          (r.returncode, S.polls.get("J26"), r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"inching"}'], tmp)
    check("an unfamiliar status whose progress alone moves is waited out past 20 polls (exit 0)",
          r.returncode == 0 and "job J37: done" in r.stdout and S.polls.get("J37", 0) > 20,
          (r.returncode, S.polls.get("J37"), r.stderr[-300:]))
    for scenario, job, what in (("percent", "J42", "only `percent`, a field the wait never names"),
                                ("queue", "J45", "only its queue position")):
        r = run(["call", "dev/render", "/render", json.dumps({"scenario": scenario})], tmp)
        check(f"an unfamiliar status where {what} moves is waited out past 20 polls (exit 0)",
              r.returncode == 0 and f"job {job}: done" in r.stdout and S.polls.get(job, 0) > 20,
              (r.returncode, S.polls.get(job), r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"flap"}'], tmp)
    check("two unfamiliar statuses alternating stop the wait 20 polls after both were seen (exit 2)",
          r.returncode == 2 and S.polls.get("J43") == 21 and "sc-job fetch dev/render J43" in r.stderr,
          (r.returncode, S.polls.get("J43"), r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"heartbeat"}'], tmp)
    check("a stuck job whose clock fields tick (`*_at`, `*_s`, heartbeat) stops the wait after 20 polls (exit 2)",
          r.returncode == 2 and S.polls.get("J44") == 20, (r.returncode, S.polls.get("J44"), r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"stuck"}'], tmp)
    check("an unfamiliar status that doesn't change stops the wait after 20 polls (exit 2)",
          r.returncode == 2 and S.polls.get("J22") == 20 and "sc-job fetch dev/render J22" in r.stderr,
          (r.returncode, S.polls.get("J22"), r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"longrun"}'], tmp)
    check("'running', unchanged for 30 polls, is waited out (exit 0)",
          r.returncode == 0 and "job J23: done" in r.stdout, (r.returncode, S.polls.get("J23"), r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"blank"}'], tmp)
    check("no status stops the wait after 20 polls even while the stage changes (exit 2)",
          r.returncode == 2 and S.polls.get("J24") == 20, (r.returncode, S.polls.get("J24"), r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"error"}'], tmp)
    check("'error' ends the wait at once, exit 1, with the error",
          r.returncode == 1 and S.polls.get("J25") == 1 and "job J25: error · out of memory" in r.stdout,
          (r.returncode, S.polls.get("J25"), r.stdout + r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"patchy"}'], tmp)
    check("eight failed polls, never five in a row, are waited out: a good poll resets the count (exit 0)",
          r.returncode == 0 and "job J34: done" in r.stdout and S.polls.get("J34") == 10,
          (r.returncode, S.polls.get("J34"), r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"mixed"}'], tmp)
    check("an unfamiliar status is not named when 'running' breaks the run of three",
          r.returncode == 0 and "doesn't know" not in r.stderr, (r.returncode, r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"list"}'], tmp)
    check("a poll that answers JSON but not an object is refused, retried, and waited out (exit 0)",
          r.returncode == 0 and "job J36: done" in r.stdout and "did not answer a JSON object" in r.stderr,
          (r.returncode, r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"odd"}'], tmp)
    check("an unknown status is named once, then expired exits 1",
          r.returncode == 1 and r.stderr.count("doesn't know") == 1 and "job J9: expired" in r.stdout, r.stdout + r.stderr)
    r = run(["call", "dev/render", "/render", '{"scenario":"canceled"}'], tmp)
    check("'canceled' ends the wait, exit 1", r.returncode == 1 and "job J10: canceled" in r.stdout, r.stdout + r.stderr)

    print("sc-job — a grant is never printed")
    mark = S.next_grant
    r = run(["call", "dev/render", "/render", '{"scenario":"strict422"}', "--store", "projects"], tmp)
    g = minted_since(mark)
    check("a 422 that echoes the grant twice prints both redacted (the error line), and the grant is revoked (exit 2)",
          r.returncode == 2 and not leaked(r.stdout + r.stderr) and r.stderr.count("sc_grant_[redacted]") == 2
          and g and g[0] in S.revoked, (r.returncode, g, r.stderr))
    r = run(["call", "dev/render", "/render", '{"scenario":"echo"}', "--store", "projects", "--json", "--out", "o-echo"], tmp)
    check("--json of a job that echoes the request prints the grant redacted",
          r.returncode == 0 and not leaked(r.stdout + r.stderr)
          and json.loads(r.stdout)["request"]["grant"] == "sc_grant_[redacted]", (r.returncode, r.stdout, r.stderr))
    r = run(["status", "dev/render", "J20"], tmp)
    check("status of that job prints the grant redacted",
          r.returncode == 0 and not leaked(r.stdout) and "sc_grant_[redacted]" in r.stdout, r.stdout)
    r = run(["call", "dev/render", "/render", '{"scenario":"leaky"}', "--store", "projects", "--out", "o-leaky"], tmp)
    out = r.stdout + r.stderr
    check("a grant echoed in a progress note is redacted", "checking sc_grant_[redacted]" in r.stderr and not leaked(r.stderr),
          r.stderr[-400:])
    check("... in the files line", "sc_grant_[redacted].txt" in r.stdout and not leaked(r.stdout), r.stdout)
    check("... in plane_error", "plane upload: upload refused for sc_grant_[redacted]" in r.stdout, r.stdout)
    check("a file named like sc_grant_report-2026.txt prints as it is", "sc_grant_report-2026.txt" in r.stdout
          and r.returncode == 0, (r.returncode, out[-400:]))
    r = run(["fetch", "dev/render", "J38", "--out", "o-leaky2"], tmp)
    check("... and fetch redacts the same", r.returncode == 0 and "sc_grant_[redacted].txt" in r.stdout
          and "upload refused for sc_grant_[redacted]" in r.stdout and not leaked(r.stdout + r.stderr), r.stdout + r.stderr)
    r = run(["call", "dev/render", "/render", '{"scenario":"leakyfail"}', "--store", "projects"], tmp)
    check("a failed job's error that echoes the grant is redacted in the summary (exit 1)",
          r.returncode == 1 and "· refused grant sc_grant_[redacted]" in r.stdout and not leaked(r.stdout + r.stderr),
          (r.returncode, r.stdout))

    print("sc-job — downloads retried, and stopped")
    for scenario, job, what in (("dropdl", "J30", "no status line (RemoteDisconnected)"),
                                ("truncdl", "J31", "a body cut short (IncompleteRead)"),
                                ("resetdl", "J32", "a reset mid-body (ConnectionResetError)")):
        r = run(["call", "dev/render", "/render", json.dumps({"scenario": scenario}), "--out", f"o-{scenario}"], tmp)
        check(f"a download that first fails with {what} is retried and lands (exit 0)",
              r.returncode == 0 and content(Path(tmp) / f"o-{scenario}/f.bin") == b"SECONDTRY"
              and S.gets.get(f"/jobs/{job}/files/f.bin") == 2, (r.returncode, S.gets.get(f"/jobs/{job}/files/f.bin"), r.stderr[-300:]))
    mark = S.next_grant
    p = subprocess.Popen([sys.executable, str(HELPER), "call", "dev/render", "/render", '{"scenario":"slowdl"}',
                          "--store", "projects", "--out", "o-slowdl"], cwd=tmp, env=env_for(tmp),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 20
    while not S.gets.get("/jobs/J33/files/f.bin") and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.3)
    g = minted_since(mark)
    revoked_first = bool(g) and g[0] in S.revoked
    p.send_signal(signal.SIGTERM)
    try:
        o, e = p.communicate(timeout=8)
        ended = True
    except subprocess.TimeoutExpired:
        p.kill()
        o, e = p.communicate()
        ended = False
    check("SIGTERM during a download after the revoke still stops sc-job (handlers restored after a revoke)",
          revoked_first and ended and p.returncode == 2, (revoked_first, ended, p.returncode, e[-300:]))
    check("... and leaves no temp file (cleanup on any exception, not only Exception)",
          (Path(tmp) / "o-slowdl").exists() and not list((Path(tmp) / "o-slowdl").glob(".sc-job-*"))
          and not (Path(tmp) / "o-slowdl/f.bin").exists(), sorted(q.name for q in (Path(tmp) / "o-slowdl").iterdir())
          if (Path(tmp) / "o-slowdl").exists() else "no dir")

    print("sc-job — inputs, and odd answers")
    r = run(["call", "dev/render", "/render", '{"scenario":"absdup"}', "--out", "o-absdup"], tmp)
    check("a result named by its absolute URL on the service downloads",
          r.returncode == 0 and content(Path(tmp) / "o-absdup/abs.txt") == b"ABS", (r.returncode, r.stderr[-300:]))
    check("a result named twice is fetched once", content(Path(tmp) / "o-absdup/dup.txt") == b"DUP"
          and len(requests_to("/jobs/J47/files/dup.txt")) == 1, requests_to("/jobs/J47/files/dup.txt"))
    r = run(["call", "dev/render", "/render", '{"scenario":"failres"}', "--out", "o-failres"], tmp)
    check("a failed job's results are not downloaded (exit 1)",
          r.returncode == 1 and not requests_to("/jobs/J48/files/"), (r.returncode, requests_to("/jobs/J48/files/")))
    body_file = Path(tmp) / "body.json"
    body_file.write_text('{"scenario":"ok"}')
    r = run(["call", "dev/render", "/render", f"@{body_file}", "--out", "o-atfile"], tmp)
    check("BODY @file is read from the file", r.returncode == 0 and "job J1: done" in r.stdout, r.stdout + r.stderr[-300:])
    mark = S.next_grant
    r = run(["call", "dev/render", "/render", "[1]", "--store", "projects"], tmp)
    check("--store with a BODY that isn't an object is refused before a grant is minted (exit 2)",
          r.returncode == 2 and "JSON object" in r.stderr and not minted_since(mark), (r.returncode, r.stderr))
    mark = S.next_grant
    r = run(["call", "dev/render", "/render", "{}", "--store", "projects", "--ttl", "0"], tmp)
    check("--ttl 0 is refused before a grant is minted (exit 2)",
          r.returncode == 2 and "--ttl" in r.stderr and not minted_since(mark), (r.returncode, r.stderr))
    nohdr = Path(tmp) / "noauth.hdr"
    nohdr.write_text("X-Other: 1\n")
    r = run(["--hdr", str(nohdr), "list"], tmp)
    check("--hdr naming a file with no Authorization line exits 2", r.returncode == 2
          and "no Authorization line" in r.stderr, (r.returncode, r.stderr))
    r = run(["--plane", "nowhere", "list"], tmp)
    check("an unknown --plane is refused (exit 2)", r.returncode == 2 and "unknown plane" in r.stderr, (r.returncode, r.stderr))
    r = run(["call", "dev/closed", "/render", "{}"], tmp)
    check("a submit refused before anything was sent is retried (5 tries), not called 'may have reached'",
          r.returncode == 2 and "after 5 tries" in r.stderr and "may have" not in r.stderr, (r.returncode, r.stderr))
    mark = S.next_grant
    r = run(["call", "dev/render", "/render", '{"scenario":"ok"}', "--store", "flakyonce", "--out", "o-flakyonce"], tmp)
    g = minted_since(mark)
    check("a revoke that 503s once is retried and lands, with no 'could not revoke'",
          r.returncode == 0 and g and g[0] in S.revoked and "could not revoke" not in r.stderr, (r.returncode, g, r.stderr[-300:]))
    before = len(S.plane)
    r = run(["call", "dev/render", "/render", '{"scenario":"ok"}', "--store", "oddid", "--out", "o-oddid"], tmp)
    deletes = [p_ for m_, p_, _a, _b in S.plane[before:] if m_ == "DELETE"]
    check("a grant id that isn't a plain number is quoted in the revoke's path",
          deletes == ["/api/v1/grants/7%2F..%2Fx"] * len(deletes) and deletes, deletes)

    print("sc-job — refusals")
    r = run(["call", "dev/render", "/render", '{"scenario":"escape"}', "--out", "j3"], tmp)
    written = [str(q) for q in Path(tmp).rglob("escape.txt")] + [str(q) for q in Path(tmp).parent.glob("escape.txt")]
    check("a results path that leaves the job's files is refused (exit 2)",
          r.returncode == 2 and "outside the job's files" in r.stderr, r.stdout + r.stderr)
    check("... nothing was written", not written, written)
    check("... and no request went to that path, though the service would have served it", not requests_to("escape"))
    outside = Path(tmp) / "outside"
    outside.mkdir()
    (Path(tmp) / "j6").mkdir()
    (Path(tmp) / "j6/link").symlink_to(outside)
    r = run(["call", "dev/render", "/render", '{"scenario":"link"}', "--out", "j6"], tmp)
    check("a link inside --out that leads outside is refused (exit 2)",
          r.returncode == 2 and "outside" in r.stderr, r.stdout + r.stderr)
    check("... nothing landed through it, and nothing was fetched", not list(outside.iterdir())
          and not requests_to("/jobs/J6/files/"), (list(outside.iterdir()), requests_to("/jobs/J6/files/")))
    r = run(["call", "dev/render", "/render", '{"scenario":"redirect"}', "--out", "j4"], tmp)
    check("a download redirect to plain http on another host is refused (exit 2)",
          r.returncode == 2 and "not https" in r.stderr, r.stdout + r.stderr)
    check("... and the other host got nothing", not S.other, S.other)
    r = run(["call", "dev/render", "/render", '{"scenario":"truncated"}', "--out", "o-trunc"], tmp)
    check("a download cut short of its Content-Length fails (non-zero)", r.returncode not in (0, None), r.stdout + r.stderr)
    check("... leaving no file at the target and no temp file",
          not (Path(tmp) / "o-trunc/big.bin").exists() and not list((Path(tmp) / "o-trunc").glob(".sc-job-*")),
          list((Path(tmp) / "o-trunc").iterdir()) if (Path(tmp) / "o-trunc").exists() else "no dir")
    (Path(tmp) / "o-trunc2").mkdir()
    (Path(tmp) / "o-trunc2/big.bin").write_bytes(b"OLD")
    r = run(["call", "dev/render", "/render", '{"scenario":"truncated"}', "--out", "o-trunc2"], tmp)
    check("... and a file already there is left as it was (written via a temp file)",
          content(Path(tmp) / "o-trunc2/big.bin") == b"OLD" and not list((Path(tmp) / "o-trunc2").glob(".sc-job-*")),
          sorted(q.name for q in (Path(tmp) / "o-trunc2").iterdir()))
    r = run(["call", "dev/render", "/render", '{"scenario":"big"}', "--out", "o-big"], tmp)
    check("a 200 KiB download lands whole", r.returncode == 0 and content(Path(tmp) / "o-big/big.bin") == BIG,
          (r.returncode, len(content(Path(tmp) / "o-big/big.bin") or b""), r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"chunked"}', "--out", "o-chunk"], tmp)
    check("a chunked download with a stray Content-Length lands whole (exit 0)",
          r.returncode == 0 and content(Path(tmp) / "o-chunk/big.bin") == BIG,
          (r.returncode, len(content(Path(tmp) / "o-chunk/big.bin") or b""), r.stderr[-300:]))
    r = run(["call", "dev/render", "/render", '{"scenario":"shortbig"}', "--out", "o-shortbig"], tmp)
    check("a download cut short after 200 KB of its 300 KB fails, leaving no file and no temp file",
          r.returncode not in (0, None) and not (Path(tmp) / "o-shortbig/big.bin").exists()
          and not list((Path(tmp) / "o-shortbig").glob(".sc-job-*")),
          (r.returncode, sorted(q.name for q in (Path(tmp) / "o-shortbig").iterdir()) if (Path(tmp) / "o-shortbig").exists() else "no dir"))
    r = run(["call", "dev/render", "/render", '{"scenario":"unicode"}', "--out", "o-uni"], tmp)
    check("result names with spaces and non-ASCII letters download",
          r.returncode == 0 and content(Path(tmp) / "o-uni/my sheet é.png") == b"SPACED"
          and content(Path(tmp) / "o-uni/stills/café 01.jpg") == b"ACCENTED", r.stdout + r.stderr)
    S.other.clear()
    r = run(["call", "dev/render", "/render", '{"scenario":"apiredir"}'], tmp)
    check("an API call redirected to another host carries no bearer there",
          not [a for p_, a in S.other if a], S.other)
    for scenario, frag, what in (("badjob", "evil1", "a submit reply naming a malformed job id"),
                                 ("badpoll", "evilpoll", "a submit reply naming a poll path with '..'")):
        r = run(["call", "dev/render", "/render", json.dumps({"scenario": scenario}), "--out", f"o-{scenario}"], tmp)
        check(f"{what} is refused (exit 2), and nothing is sent to it",
              r.returncode == 2 and not requests_to(frag), (r.returncode, requests_to(frag), r.stderr))
    for cmd, frag in (("fetch", "evil2"), ("status", "evil3"), ("cancel", "evil4")):
        r = run([cmd, "dev/render", f"x/{frag}"], tmp)
        check(f"{cmd} with a malformed job id is refused (exit 2), and nothing is sent",
              r.returncode == 2 and not requests_to(frag), (r.returncode, requests_to(frag), r.stderr))
    r = run(["call", "dev/render", "/render", '{"scenario":"dropsubmit"}', "--store", "projects"], tmp)
    check("a submit that fails after sending is not re-sent (one POST), and says it may have landed",
          r.returncode == 2 and S.posts.get("dropsubmit") == 1 and "may have" in r.stderr,
          (r.returncode, S.posts.get("dropsubmit"), r.stderr))
    r = run(["call", "dev/render", "/render", '{"scenario":"submit503"}', "--store", "projects"], tmp)
    check("a submit answered 503 is not re-sent (one POST), exit 2",
          r.returncode == 2 and S.posts.get("submit503") == 1, (r.returncode, S.posts.get("submit503"), r.stderr))
    r = run(["call", "dev/plain", "/render", "{}"], tmp)
    check("a service on plain http off loopback is refused", r.returncode == 2 and "refusing" in r.stderr, r.stderr)
    r = run(["call", "dev/render", "/../admin", "{}"], tmp)
    check("a submit path with '..' is refused", r.returncode == 2 and "refusing" in r.stderr, r.stderr)
    r = run(["--hdr", os.path.join(tmp, "missing.hdr"), "list"], tmp)
    check("--hdr to a missing file exits 2 and calls nothing", r.returncode == 2 and "no readable header" in r.stderr, r.stderr)

    print("sc-job — list, fetch, status, cancel")
    r = run(["list"], tmp)
    lines = r.stdout.strip().splitlines()
    check("list shows id, health, address and a clean first sentence",
          lines and lines[0] == f"dev/render  200  {SVC_URL}  Renders things.", r.stdout)
    check("list marks services sc-job can't call",
          any(l.startswith("dev/plain  -  (not callable by sc-job: no https address)") for l in lines)
          and any(l.startswith("dev/none  -  (not callable by sc-job: no address)") for l in lines), r.stdout)
    r = run(["fetch", "dev/render", "J1", "--out", "o-fetch"], tmp)
    check("fetch waits, downloads and summarises", r.returncode == 0 and (Path(tmp) / "o-fetch/sheet.png").exists()
          and "job J1: done" in r.stdout, r.stdout + r.stderr)
    r = run(["fetch", "dev/render", "J2"], tmp)
    check("fetch of a failed job exits 1", r.returncode == 1, r.stdout + r.stderr)
    r = run(["status", "dev/render", "J2"], tmp)
    check("status prints the job as raw JSON", r.returncode == 0 and json.loads(r.stdout)["status"] == "failed", r.stdout)
    r = run(["cancel", "dev/render", "J1"], tmp)
    check("cancel sends DELETE on the job", r.returncode == 0 and ("DELETE", "/jobs/J1") in
          [(m_, p) for m_, p, _a, _b in S.service], r.stdout + r.stderr)
    r = run(["status", "dev/render", "../x"], tmp)
    check("a malformed job id on the command line is refused", r.returncode == 2, r.stdout + r.stderr)

    passed = sum(results)
    print(f"\n{'ALL PASSED' if passed == len(results) else 'FAILED'}: {passed} passed, {len(results) - passed} failed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
