#!/usr/bin/env python3
"""The reference services in examples/echo-services against a fake plane (doc 793).

Both run as compose.yaml runs them (a subprocess, `python3 -I`). test-echo-job must
check the bearer on every call but its guide and health, refuse `{}` with a 4xx JSON error,
check the grant before queueing, lease with its own service token, give the lease back on
every path (through a 5xx, and only on the plane's own 404), and work end to end with
`sc-job call --store`. Neither may reflect a credential: no header, no grant field, no
grant secret, in an answer or in the file.
Run: python3 sandbox/test_echo_services.py
"""
import hashlib
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples/echo-services"
SC_JOB = ROOT / "registry/helpers/samcloud-job"
ALICE, BOB, SVC_JOB, SVC_API = "Bearer alice-token", "Bearer bob-token", "Bearer svc-job-token", "Bearer svc-api-token"
CAROL, DAVE = "Bearer carol-token", "Bearer dave-token"
LONG_A, LONG_B = "Bearer " + "p" * 60 + "A", "Bearer " + "p" * 60 + "B"     # one valid, one not: same long prefix
WHO = {ALICE: "alice", BOB: "bob", CAROL: "carol", LONG_A: "erin", SVC_JOB: "dev/test-echo-job", SVC_API: "dev/test-echo-api"}
RESOURCE = "dev/gpu-0"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"  [{str(detail)[:500]}]"))


def secret(n):
    """Shaped like the plane's own: "sc_grant_" and 43 of [A-Za-z0-9_-]."""
    return "sc_grant_" + (f"{n:04d}-Ab_" + hashlib.sha256(str(n).encode()).hexdigest())[:43]


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []                 # (method, path, auth, body)
        self.grants = {}                # secret → {id, creator, group, revoked}
        self.leases = {}                # id → {status, auth}
        self.files = []                 # (auth, grant, name, data)
        self.next = 100
        self.mode = {}                  # what the plane does next: lease, wait, release, verify, upload
        self.waiting = self.most_waiting = 0      # concurrent GET /leases/{id}/wait


S = State()


class Plane(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def drop(self):
        """No answer at all: the connection closes without a status line."""
        self.close_connection = True

    def handle_any(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        auth = self.headers.get("Authorization")
        p = self.path[len("/api/v1"):]
        body = None
        if raw and "json" in (self.headers.get("Content-Type") or ""):
            body = json.loads(raw)
        with S.lock:
            S.calls.append((method, p, auth, body, time.monotonic()))
            S.next += 1
            nid = S.next
        if p == "/auth/verify":
            if S.mode.get("verify") == "down":
                return self.send(502, {"detail": "bad gateway"})
            if S.mode.get("verify") == "drop":
                return self.drop()
            if auth not in WHO:
                return self.send(401, {"detail": "Invalid token"})
            svc = auth in (SVC_JOB, SVC_API)
            return self.send(200, {"valid": True, "owner_id": WHO[auth], "username": None if svc else WHO[auth],
                                   "token_type": "service" if svc else "agent"})
        if auth not in WHO:
            return self.send(401, {"detail": "Invalid token"})
        if method == "GET" and p.startswith("/services/"):
            sid = urllib.parse.unquote(p[len("/services/"):])
            return self.send(200, {"id": sid, "external_url": URLS[sid]})
        if method == "POST" and p == "/grants":
            s = secret(nid)
            with S.lock:
                S.grants[s] = {"id": nid, "creator": WHO[auth], "group": body["group_scope"], "revoked": False,
                               "service": body["service_id"]}
            return self.send(200, {"id": nid, "grant": s, "expires_at": "later"})
        if method == "DELETE" and p.startswith("/grants/"):
            for g in S.grants.values():
                if str(g["id"]) == p.rsplit("/", 1)[1]:
                    g["revoked"] = True
            return self.send(200, {"revoked_at": "now"})
        if method == "POST" and p == "/grants/check":
            if auth not in (SVC_JOB, SVC_API):
                return self.send(403, {"detail": "service token only"})
            g = S.grants.get(body.get("grant"))
            if not g or g["revoked"]:
                return self.send(200, {"valid": False, "status": 403, "reason": "grant revoked or unknown"})
            return self.send(200, {"valid": True, "id": g["id"], "creator": g["creator"], "group_scope": g["group"],
                                   "expires_at": "later", "uses": 0})
        if method == "POST" and p == f"/resources/{RESOURCE}/leases":
            how = S.mode.get("lease", "active")
            if how == "refuse":
                return self.send(409, {"detail": {"code": "exceeds_capacity"}})
            lid = f"L{nid}"
            with S.lock:
                S.leases[lid] = {"status": "queued" if how == "queued" else "active", "auth": auth, "body": body}
            return self.send(200, {"lease_id": lid, "status": S.leases[lid]["status"]})
        m = re.fullmatch(r"/leases/(L\d+)(/wait\?timeout_s=\d+)?", p)
        if m and method == "GET" and m.group(2):
            if S.mode.get("wait") == "502":
                return self.send(502, {"detail": "bad gateway"})
            with S.lock:
                S.waiting += 1
                S.most_waiting = max(S.most_waiting, S.waiting)
            time.sleep(0.3)
            with S.lock:
                S.waiting -= 1
            lease = S.leases.get(m.group(1))
            if S.mode.get("lease") == "queued-then-active" or (lease and S.mode.get("lease") != "queued"):
                lease["status"] = "active"
            return self.send(200, {"id": m.group(1), "status": lease["status"]})
        if m and method == "DELETE":
            plan = S.mode.get("release", [])
            how = plan.pop(0) if plan else "ok"
            if how == "502":
                return self.send(502, {"detail": "bad gateway"})
            if how == "404route":
                return self.send(404, {"detail": "Not Found"})
            if how == "drop":
                return self.drop()
            if m.group(1) not in S.leases or how == "gone":
                return self.send(404, {"detail": f"Lease {m.group(1)} not found"})
            S.leases[m.group(1)]["status"] = "released"
            return self.send(200, {"id": m.group(1), "status": "released"})
        if method == "POST" and p == "/files":
            fields = dict(re.findall(rb'name="([a-z]+)"(?:; filename="[^"]*")?\r\n(?:Content-Type: [^\r]*\r\n)?\r\n(.*?)\r\n--',
                                     raw, re.S))
            grant = fields.get(b"grant", b"").decode()
            g = S.grants.get(grant)
            if S.mode.get("upload") == "slow":
                time.sleep(1)
            if not g or g["revoked"] or S.mode.get("upload") == "refuse":
                return self.send(403, {"detail": "grant revoked"})
            with S.lock:
                S.files.append((auth, grant, "echo.json", fields.get(b"file", b"")))
            return self.send(200, {"id": nid, "name": "echo.json"})
        if method == "GET" and p.startswith("/files/"):
            return self.send(200, {"id": int(p.rsplit("/", 1)[1]), "name": "echo.json", "content_type": "application/json"})
        if method == "POST" and p.startswith("/services/") and p.endswith("/health"):
            return self.send(200, {"status": "ok"})
        return self.send(404, {"detail": f"plane has no {method} {p}"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_DELETE(self):
        self.handle_any("DELETE")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


plane_srv = ThreadingHTTPServer(("127.0.0.1", 0), Plane)
threading.Thread(target=plane_srv.serve_forever, daemon=True).start()
PLANE = f"http://127.0.0.1:{plane_srv.server_port}/api/v1"
PORTS = {"dev/test-echo-job": free_port(), "dev/test-echo-api": free_port()}
URLS = {k: f"http://127.0.0.1:{v}" for k, v in PORTS.items()}


def start(script, sid, token, tmp):
    hdr = Path(tmp) / f"{script}.hdr"
    hdr.write_text(f"Authorization: {token}\n")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SAMCLOUD_API": PLANE, "SERVICE_ID": sid,
           "SERVICE_HDR": str(hdr), "LEASE_RESOURCE": RESOURCE, "PORT": str(PORTS[sid]), "HEARTBEAT_S": "1",
           "RELEASE_BUDGET_S": "3", "BACKOFF_S": "0.2", "SHUTDOWN_S": "5", "WHO_CACHE_S": "1"}
    proc = subprocess.Popen([sys.executable, "-I", str(EXAMPLES / script)], env=env, cwd=tmp,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if call(sid, "GET", "/health")[0] == 200:
                return proc
        except OSError:
            time.sleep(0.05)
    raise SystemExit(f"{script} didn't start: {proc.stdout.read() if proc.poll() is not None else ''}")


def call(sid, method, path, body=None, auth=None, raw=None, conn=None):
    """(status, parsed JSON or text, headers)."""
    c = conn or http.client.HTTPConnection("127.0.0.1", PORTS[sid], timeout=20)
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if auth:
        headers["Authorization"] = auth
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    text = r.read().decode()
    try:
        out = json.loads(text)
    except ValueError:
        out = text
    if not conn:
        c.close()
    return r.status, out, dict(r.getheaders())


def wait_job(job, auth=ALICE, until=("done", "failed", "cancelled"), timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, j, _ = call("dev/test-echo-job", "GET", f"/jobs/{job}", auth=auth)
        if code == 200 and j.get("status") in until and ("lease_released" in j or not j.get("lease")):
            return j
        time.sleep(0.1)
    return j


def calls_since(n, method=None, prefix=""):
    with S.lock:
        return [c for c in S.calls[n:] if (method is None or c[0] == method) and c[1].startswith(prefix)]


def mint(auth=ALICE, group="grp", sid="dev/test-echo-job"):
    c = http.client.HTTPConnection("127.0.0.1", plane_srv.server_port, timeout=10)
    c.request("POST", "/api/v1/grants", body=json.dumps({"service_id": sid, "group_scope": group, "ttl_s": 600}),
              headers={"Authorization": auth, "Content-Type": "application/json"})
    return json.loads(c.getresponse().read())["grant"]


def credentials_in(text):
    """Any token or grant secret this run knows, in text a service gave back."""
    words = [t.split()[1] for t in WHO] + list(S.grants)
    return [w for w in words if w in text] + (["Authorization"] if "Authorization" in text else [])


def test_job(tmp):
    J = "dev/test-echo-job"
    code, j, h = call(J, "GET", "/health")
    check("job: /health answers without a token", code == 200 and j.get("status") == "ok", (code, j))
    code, g, h = call(J, "GET", "/service-docs")
    check("job: the guide opens without a token, and says it's a test fixture",
          code == 200 and "For testing and copying, not for real work." in g and h.get("Content-Type", "").startswith("text/markdown"),
          (code, g[:200]))
    for method, path, body in (("GET", "/jobs/sc-job-check", None), ("POST", "/jobs", {}), ("DELETE", "/jobs/x", None),
                               ("GET", "/jobs/x/files/echo.json", None), ("GET", "/elsewhere", None)):
        code, j, h = call(J, method, path, body)
        check(f"job: {method} {path} without a token is 401 with a JSON error and WWW-Authenticate",
              code == 401 and isinstance(j, dict) and j.get("error") and h.get("WWW-Authenticate", "").startswith("Bearer"),
              (code, j, h))
    code, j, _ = call(J, "POST", "/jobs", {"text": "hi"}, auth="Bearer not-a-token")
    check("job: a token the plane refuses is 401", code == 401, (code, j))

    n = len(S.calls)
    code, j, _ = call(J, "POST", "/jobs", {}, auth=ALICE)
    check("job: {} is refused with 422 and a JSON error", code == 422 and "text is required" in j.get("error", ""), (code, j))
    for body, why in (({"text": "hi", "colour": "red"}, "unknown field"), ({"text": "x" * 1001}, "text is required"),
                      ({"text": "hi", "wait_s": True}, "wait_s"), ({"text": "hi", "wait_s": 31}, "wait_s"),
                      ({"text": "hi", "grant": "not-a-grant"}, "grant is the secret"), (["hi"], "JSON object")):
        code, j, _ = call(J, "POST", "/jobs", body, auth=ALICE)
        check(f"job: {json.dumps(body)[:40]} is 422 ({why})", code == 422 and why in j.get("error", ""), (code, j))
    code, j, _ = call(J, "POST", "/jobs", raw=b"{not json", auth=ALICE)
    check("job: a body that isn't JSON is 400", code == 400 and j.get("error"), (code, j))
    check("job: nothing refused took a lease", not calls_since(n, "POST", "/resources"), calls_since(n, "POST", "/resources"))

    bobs = mint(BOB)
    n = len(S.calls)
    code, j, _ = call(J, "POST", "/jobs", {"text": "hi", "grant": bobs}, auth=ALICE)
    check("job: a grant someone else made is 403 at submit, before any lease",
          code == 403 and "someone else" in j.get("error", "") and not calls_since(n, "POST", "/resources"), (code, j))
    check("job: the grant is checked with the service token",
          [c[2] for c in calls_since(n, "POST", "/grants/check")] == [SVC_JOB], calls_since(n))
    revoked = mint(ALICE)
    S.grants[revoked]["revoked"] = True
    code, j, _ = call(J, "POST", "/jobs", {"text": "hi", "grant": revoked}, auth=ALICE)
    check("job: a grant the plane says isn't valid is 422 with its reason", code == 422 and "revoked" in j.get("error", ""),
          (code, j))

    # sc-job call --store, end to end
    hdr = Path(tmp) / "alice.hdr"
    hdr.write_text(f"Authorization: {ALICE}\n")
    os.chmod(hdr, 0o600)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SAMCLOUD_")}
    env.update({"HOME": tmp, "SAMCLOUD_API": PLANE, "SAMCLOUD_AUTH_HDR": str(hdr), "SC_JOB_POLL_S": "0.1,0.2"})
    leak = secret(7)
    n = len(S.calls)
    t0 = time.time()
    r = subprocess.run([sys.executable, str(SC_JOB), "call", J, "/jobs",
                        json.dumps({"text": f"hello {leak}", "wait_s": 1}), "--store", "grp", "--out", "out"],
                       cwd=tmp, env=env, capture_output=True, text=True, timeout=60)
    took = time.time() - t0
    f = Path(tmp) / "out/echo.json"
    data = json.loads(f.read_text()) if f.exists() else {}
    check("job: sc-job call --store finishes done (exit 0), downloads echo.json and names the plane file",
          r.returncode == 0 and data.get("input", {}).get("text", "").startswith("hello")
          and "plane files you own" in r.stdout, r.stdout + r.stderr)
    check("job: it held the lease for wait_s", 1 <= took < 30, took)
    leases = calls_since(n, "POST", f"/resources/{RESOURCE}/leases")
    check("job: one lease, taken with the service's own token, sized LEASE_MB with a TTL",
          len(leases) == 1 and leases[0][2] == SVC_JOB and leases[0][3].get("memory_mb") == 64
          and leases[0][3].get("ttl_seconds") == 120 and "service_id" not in leases[0][3], leases)
    lid = data.get("lease")
    check("job: the lease is released with the service token, and the poll says so",
          [(c[0], c[2]) for c in calls_since(n, "DELETE", "/leases/")] == [("DELETE", SVC_JOB)]
          and S.leases.get(lid, {}).get("status") == "released", (lid, calls_since(n, "DELETE")))
    up = [x for x in S.files if x[0] == SVC_JOB]
    check("job: the file went to the caller's grant with the service token, before the grant was revoked",
          len(up) == 1 and S.grants[up[0][1]]["creator"] == "alice" and S.grants[up[0][1]]["revoked"], up)
    stored = up[0][3].decode() if up else ""
    check("job: the file holds the input without the grant, and a grant secret in the text is redacted",
          data.get("input") == {"text": "hello sc_grant_[redacted]", "wait_s": 1} and "grant" not in data["input"]
          and json.loads(stored or "{}") == data, data)
    check("job: no token or grant secret in the file, the download or sc-job's output",
          not credentials_in(stored + f.read_text() if f.exists() else stored) and leak not in r.stdout + r.stderr,
          credentials_in(stored))
    check("job: it reports health with its own token",
          any(c[1] == f"/services/{J}/health" and c[2] == SVC_JOB for c in S.calls), [c for c in S.calls if "health" in c[1]])

    # the job's routes are the submitter's alone
    job = data.get("job")
    code, j, _ = call(J, "GET", f"/jobs/{job}", auth=ALICE)
    check("job: the poll shows results, plane_files, the lease and lease_released",
          code == 200 and j.get("status") == "done" and j.get("results") == [f"/jobs/{job}/files/echo.json"]
          and isinstance(j.get("plane_files", {}).get("echo.json"), int) and j.get("lease") == lid
          and j.get("lease_released") is True and j.get("expires"), j)
    for path in (f"/jobs/{job}", f"/jobs/{job}/files/echo.json"):
        code, j, _ = call(J, "GET", path, auth=BOB)
        check(f"job: someone else's GET {path.replace(job, '<job>')} is 404", code == 404, (code, j))
    code, j, _ = call(J, "DELETE", f"/jobs/{job}", auth=BOB)
    check("job: someone else's DELETE is 404, and the job stays", code == 404
          and call(J, "GET", f"/jobs/{job}", auth=ALICE)[0] == 200, (code, j))
    code, j, _ = call(J, "DELETE", f"/jobs/{job}", auth=ALICE)
    check("job: DELETE on a finished job deletes it", code == 200 and j.get("deleted") is True
          and call(J, "GET", f"/jobs/{job}", auth=ALICE)[0] == 404, (code, j))

    # the lease comes back on every path
    S.mode["release"] = ["502"]
    n = len(S.calls)
    code, j, _ = call(J, "POST", "/jobs", {"text": "through a 502", "wait_s": 0}, auth=ALICE)
    j = wait_job(j["job"])
    dels = calls_since(n, "DELETE", "/leases/")
    check("job: a 502 on release is retried, and the release counted once the plane says 2xx",
          j.get("status") == "done" and j.get("lease_released") is True and len(dels) == 2, (j, dels))
    S.mode["release"] = ["gone"]
    code, j, _ = call(J, "POST", "/jobs", {"text": "already gone", "wait_s": 0}, auth=ALICE)
    j = wait_job(j["job"])
    check("job: the plane's own 404 for the lease counts as released", j.get("lease_released") is True, j)
    S.mode["release"] = ["404route"]
    n = len(S.calls)
    code, j, _ = call(J, "POST", "/jobs", {"text": "wrong route", "wait_s": 0}, auth=ALICE)
    j = wait_job(j["job"])
    check("job: a 404 that isn't the plane's word on this lease is not counted as released, and isn't retried",
          j.get("lease_released") is False and len(calls_since(n, "DELETE", "/leases/")) == 1, j)
    S.mode["release"] = []

    S.mode["lease"] = "refuse"
    n = len(S.calls)
    code, j, _ = call(J, "POST", "/jobs", {"text": "no room"}, auth=ALICE)
    j = wait_job(j["job"])
    check("job: a refused lease fails the job with the plane's reason, and nothing is released",
          j.get("status") == "failed" and "409" in j.get("error", "") and not calls_since(n, "DELETE", "/leases/"), j)
    S.mode["lease"] = "queued"
    n = len(S.calls)
    code, j, _ = call(J, "POST", "/jobs", {"text": "queued then withdrawn"}, auth=ALICE)
    job = j["job"]
    time.sleep(0.5)
    code, j, _ = call(J, "DELETE", f"/jobs/{job}", auth=ALICE)
    j = wait_job(job)
    dels = calls_since(n, "DELETE", "/leases/")
    check("job: withdrawn while the lease is queued, the job is cancelled and the queued lease released",
          j.get("status") == "cancelled" and j.get("lease_released") is True and len(dels) == 1, (j, dels))
    S.mode["lease"] = "queued-then-active"
    code, j, _ = call(J, "POST", "/jobs", {"text": "waits its turn", "wait_s": 0}, auth=ALICE)
    j = wait_job(j["job"])
    check("job: a queued lease is waited for with /leases/{id}/wait, then the job runs",
          j.get("status") == "done" and j.get("lease_released") is True, j)
    S.mode["lease"] = "active"

    grant = mint(ALICE)
    S.mode["upload"] = "refuse"
    code, j, _ = call(J, "POST", "/jobs", {"text": "upload refused", "wait_s": 0, "grant": grant}, auth=ALICE)
    job = j["job"]
    j = wait_job(job)
    check("job: an upload the plane refuses is plane_error on a done job, and the download still works",
          j.get("status") == "done" and "403" in j.get("plane_error", "") and not j.get("plane_files")
          and call(J, "GET", f"/jobs/{job}/files/echo.json", auth=ALICE)[0] == 200
          and grant not in json.dumps(j), j)
    S.mode.pop("upload")

    S.mode["verify"] = "down"
    code, j, _ = call(J, "GET", "/jobs/x", auth="Bearer fresh-token")
    check("job: a plane that can't check the token is 503, never a pass", code == 503 and j.get("error"), (code, j))
    S.mode.pop("verify")

    c = http.client.HTTPConnection("127.0.0.1", PORTS[J], timeout=10)
    first = call(J, "POST", "/jobs", {"text": "no token"}, conn=c)[0]
    second = call(J, "GET", "/health", conn=c)[0]
    c.close()
    check("job: a refused body is still read, so the next request on the connection works", (first, second) == (401, 200),
          (first, second))


def test_bearer():
    """The bearer check can't be passed by the plane being away, a stale cache or a shared prefix."""
    J = "dev/test-echo-job"
    S.mode["verify"] = "drop"
    code, j, _ = call(J, "GET", "/jobs/x", auth="Bearer never-seen")
    check("bearer: a plane that drops the connection is 503, never a pass", code == 503 and j.get("error"), (code, j))
    S.mode.pop("verify")
    WHO[DAVE] = "dave"
    first = call(J, "GET", "/jobs/x", auth=DAVE)[0]
    del WHO[DAVE]
    cached = call(J, "GET", "/jobs/x", auth=DAVE)[0]
    time.sleep(1.3)
    later = call(J, "GET", "/jobs/x", auth=DAVE)[0]
    check("bearer: a token the plane has since refused is 401 once the cache (WHO_CACHE_S) runs out",
          (first, cached, later) == (404, 404, 401), (first, cached, later))
    a = call(J, "GET", "/jobs/x", auth=LONG_A)[0]
    b = call(J, "GET", "/jobs/x", auth=LONG_B)[0]
    check("bearer: the cache is keyed by the whole token: one sharing a valid token's prefix is 401", (a, b) == (404, 401),
          (a, b))


def test_release():
    J = "dev/test-echo-job"
    S.mode["release"] = ["drop"]
    n = len(S.calls)
    code, j, _ = call(J, "POST", "/jobs", {"text": "no answer on release", "wait_s": 0}, auth=ALICE)
    j = wait_job(j["job"])
    check("release: no answer at all is retried, and the lease released",
          j.get("lease_released") is True and len(calls_since(n, "DELETE", "/leases/")) == 2, (j, calls_since(n, "DELETE")))
    S.mode["release"] = ["502"] * 100
    n = len(S.calls)
    t0 = time.monotonic()
    code, j, _ = call(J, "POST", "/jobs", {"text": "never released", "wait_s": 0}, auth=ALICE)
    j = wait_job(j["job"])
    took = time.monotonic() - t0
    check("release: it stops at RELEASE_BUDGET_S (3 s), says it wasn't released, and keeps the id",
          j.get("lease_released") is False and j.get("lease") and 1 < took < 6
          and len(calls_since(n, "DELETE", "/leases/")) <= 6, (j, took, len(calls_since(n, "DELETE", "/leases/"))))
    S.mode["release"] = []


def test_release_clock(tmp):
    """release() itself, on a clock that only moves when it sleeps: the delays it asks for are
    exact, at the shipped defaults (2 s doubling to 60, a 300 s budget)."""
    import importlib.util
    import types
    hdr = Path(tmp) / "unit.hdr"
    hdr.write_text(f"Authorization: {SVC_JOB}\n")
    saved = dict(os.environ)
    os.environ.update({"SAMCLOUD_API": PLANE, "SERVICE_HDR": str(hdr)})
    for k in ("BACKOFF_S", "RELEASE_BUDGET_S"):
        os.environ.pop(k, None)
    try:
        spec = importlib.util.spec_from_file_location("echo_job_unit", EXAMPLES / "echo_job.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    clock = types.SimpleNamespace(now=0.0, delays=[])
    clock.monotonic = lambda: clock.now
    clock.sleep = lambda d: (clock.delays.append(d), setattr(clock, "now", clock.now + d))
    clock.time = time.time
    mod.time = clock
    S.leases["L9001"] = {"status": "active", "auth": SVC_JOB, "body": {}}
    S.mode["release"] = ["502", "502", "502"]
    ok = mod.release("L9001")
    check("release: the back-off doubles from 2 s", ok is True and clock.delays == [2, 4, 8], (ok, clock.delays))
    S.leases["L9002"] = {"status": "active", "auth": SVC_JOB, "body": {}}
    S.mode["release"] = ["502"] * 100
    clock.delays.clear()
    n = len(S.calls)
    ok = mod.release("L9002")
    check("release: it doubles to 60 s and stops inside the 300 s budget, not released",
          ok is False and clock.delays == [2, 4, 8, 16, 32, 60, 60, 60] and len(calls_since(n, "DELETE")) == 9,
          (ok, clock.delays, len(calls_since(n, "DELETE"))))
    S.mode["release"] = ["drop", "drop"]
    clock.delays.clear()
    ok = mod.release("L9002")
    check("release: no answer is retried like a 5xx", ok is True and clock.delays == [2, 4], (ok, clock.delays))
    S.mode["release"] = []


def test_limits():
    """However many jobs come in, the plane sees one lease and one long-poll from the service."""
    J = "dev/test-echo-job"
    S.mode["lease"] = "queued"
    S.most_waiting = 0
    n = len(S.calls)
    before = set(S.leases)
    took = [call(J, "POST", "/jobs", {"text": f"a{i}"}, auth=ALICE) for i in range(3)]
    check("limits: a caller's third live job is 429 with Retry-After",
          [c for c, _, _ in took] == [202, 202, 429] and took[2][2].get("Retry-After") == "30"
          and "limits are 2 and 4" in took[2][1].get("error", ""), [(c, j) for c, j, _ in took])
    took += [call(J, "POST", "/jobs", {"text": f"b{i}"}, auth=BOB) for i in range(2)]
    carol = call(J, "POST", "/jobs", {"text": "c"}, auth=CAROL)
    check("limits: a fifth live job from anyone is 429", [c for c, _, _ in took[3:]] == [202, 202] and carol[0] == 429,
          ([c for c, _, _ in took[3:]], carol[:2]))
    time.sleep(2.5)
    leases = calls_since(n, "POST", "/resources")
    check("limits: four jobs waiting, one lease asked for and one long-poll at a time",
          len(leases) == 1 and S.most_waiting == 1, (len(leases), S.most_waiting))
    for c, j, _ in took:
        if c == 202:
            call(J, "DELETE", f"/jobs/{j['job']}", auth=ALICE if j["job"] in (took[0][1]["job"], took[1][1]["job"]) else BOB)
    first = wait_job(took[0][1]["job"])
    check("limits: withdrawn, the waiting lease is released and the slots free up",
          first.get("status") == "cancelled" and first.get("lease_released") is True
          and call(J, "POST", "/jobs", {"text": "c", "wait_s": 0}, auth=CAROL)[0] == 202, first)
    S.mode["lease"] = "active"
    S.mode["wait"] = "502"
    S.mode["lease"] = "queued"
    time.sleep(1.5)                           # carol's job, waiting on a plane that answers 502
    n = len(S.calls)
    time.sleep(2)
    waits = len(calls_since(n, "GET", "/leases/"))
    check("limits: a wait the plane answers 502 backs off (a handful of calls in 2 s, not thousands)", 1 <= waits <= 6,
          waits)
    S.mode.pop("wait")
    S.mode["lease"] = "active"
    with S.lock:
        for lease in S.leases.values():
            if lease["status"] == "queued":
                lease["status"] = "active"
    deadline = time.time() + 10
    while time.time() < deadline and any(v["status"] != "released" for k, v in S.leases.items() if k not in before):
        time.sleep(0.1)
    check("limits: every lease the service took here has been released",
          all(v["status"] == "released" for k, v in S.leases.items() if k not in before),
          {k: v["status"] for k, v in S.leases.items() if k not in before})


def test_order_and_echo():
    J = "dev/test-echo-job"
    grant = mint(ALICE)
    S.mode["upload"] = "slow"
    code, j, _ = call(J, "POST", "/jobs", {"text": "slow upload", "wait_s": 0, "grant": grant}, auth=ALICE)
    job, first_done = j["job"], None
    deadline = time.time() + 15
    while time.time() < deadline and first_done is None:
        c, j, _ = call(J, "GET", f"/jobs/{job}", auth=ALICE)
        if j.get("status") == "done":
            first_done = j
        time.sleep(0.05)
    S.mode.pop("upload")
    check("order: the upload lands before `done`, so sc-job revoking the grant at `done` can't race it",
          first_done and isinstance(first_done.get("plane_files", {}).get("echo.json"), int), first_done)
    s9 = secret(9)
    code, j, _ = call(J, "POST", "/jobs", {"text": "hi", s9: 1}, auth=ALICE)
    check("echo: a grant sent as an unknown field's name isn't in the 422", code == 422 and s9 not in json.dumps(j)
          and "sc_grant_[redacted]" in j.get("error", ""), (code, j))


def test_shutdown(proc):
    """SIGTERM (docker stop) gives the lease back before the process exits."""
    J = "dev/test-echo-job"
    code, j, _ = call(J, "POST", "/jobs", {"text": "stopped mid-job", "wait_s": 30}, auth=ALICE)
    deadline = time.time() + 10
    while time.time() < deadline and call(J, "GET", f"/jobs/{j['job']}", auth=ALICE)[1].get("status") != "running":
        time.sleep(0.1)
    lease = call(J, "GET", f"/jobs/{j['job']}", auth=ALICE)[1].get("lease")
    n = len(S.calls)
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    dels = [c for c in calls_since(n, "DELETE", "/leases/") if c[2] == SVC_JOB]
    check("shutdown: SIGTERM releases the running job's lease and the process exits",
          lease and proc.poll() == 0 and dels and S.leases[lease]["status"] == "released", (lease, proc.poll(), dels))


def test_api():
    A = "dev/test-echo-api"
    code, j, _ = call(A, "GET", "/health")
    code2, g, _ = call(A, "GET", "/service-docs")
    check("api: /health and the guide answer without a token, and the guide says it's a test fixture",
          code == 200 and code2 == 200 and "For testing and copying, not for real work." in g, (code, code2))
    for method, path in (("POST", "/echo"), ("GET", "/jobs/sc-job-check"), ("GET", "/anything")):
        code, j, h = call(A, method, path, {"text": "hi"} if method == "POST" else None)
        check(f"api: {method} {path} without a token is 401", code == 401 and j.get("error")
              and "WWW-Authenticate" in h, (code, j))
    grant = mint(ALICE, sid=A)
    body = {"text": "hello", "grant": grant, "nested": {"grant": "x", "note": f"see {grant}"},
            "list": [f"{grant}", 1, None], "Authorization": "pretend"}
    code, j, h = call(A, "POST", "/echo", body, auth=ALICE)
    check("api: POST /echo answers 200 with the body, the caller and the service",
          code == 200 and j.get("echo", {}).get("text") == "hello" and j.get("caller") == "alice"
          and j.get("service") == A, (code, j))
    check("api: no grant field, at any depth, and no grant secret anywhere in the answer",
          "grant" not in j["echo"] and "grant" not in j["echo"]["nested"]
          and j["echo"]["nested"]["note"] == "see sc_grant_[redacted]" and j["echo"]["list"][0] == "sc_grant_[redacted]"
          and grant not in json.dumps(j), j)
    s8 = secret(8)
    code, j, _ = call(A, "POST", "/echo", {s8: "a grant as a key", "deep": {s8: [s8]}}, auth=ALICE)
    check("api: a grant sent as a key, at any depth, comes back redacted", code == 200 and s8 not in json.dumps(j)
          and j["echo"].get("sc_grant_[redacted]") == "a grant as a key", (code, j))
    check("api: no header comes back: the caller's token is nowhere in the answer",
          ALICE.split()[1] not in json.dumps(j) and not any(ALICE.split()[1] in v for v in h.values()), (j, h))
    for raw, want in ((b"{oops", 400), (b"[1, 2]", 422), (b'"a string"', 422), (b"x" * 17000, 413)):
        code, j, _ = call(A, "POST", "/echo", raw=raw, auth=ALICE)
        check(f"api: {raw[:12]!r} is {want} with a JSON error", code == want and isinstance(j, dict) and j.get("error"),
              (code, j))
    check("api: another path is 404, another method 405",
          call(A, "POST", "/other", {}, auth=ALICE)[0] == 404 and call(A, "GET", "/echo", auth=ALICE)[0] == 405)
    check("api: it reports health with its own token",
          any(c[1] == f"/services/{A}/health" and c[2] == SVC_API for c in S.calls))


COMPOSE_SERVICES = """  test-echo-job:
    <<: *fixture
    command: ["python3", "-I", "/srv/echo_job.py"]
    environment:
      <<: *env
      SERVICE_ID: lagoon/test-echo-job
      LEASE_RESOURCE: lagoon/test-slot
      PORT: "8811"
    ports: ["${LAN_IP:-192.168.1.31}:8811:8811"]
    volumes:
      - ./echo_job.py:/srv/echo_job.py:ro
      - ./guide-echo-job.md:/srv/guide-echo-job.md:ro
      - ${TOKENS:-/etc/samcloud-test-echo}/test-echo-job.hdr:/run/token.hdr:ro

  test-echo-api:
    <<: *fixture
    command: ["python3", "-I", "/srv/echo_api.py"]
    environment:
      <<: *env
      SERVICE_ID: lagoon/test-echo-api
      PORT: "8812"
    ports: ["${LAN_IP:-192.168.1.31}:8812:8812"]
    volumes:
      - ./echo_api.py:/srv/echo_api.py:ro
      - ./guide-echo-api.md:/srv/guide-echo-api.md:ro
      - ${TOKENS:-/etc/samcloud-test-echo}/test-echo-api.hdr:/run/token.hdr:ro

networks:
  fixtures: {}
"""


def test_compose():
    """compose.yaml keeps the fixtures off the plane they sit beside (claude-producer, task 1472)."""
    text = (EXAMPLES / "compose.yaml").read_text()
    anchor = text.split("services:", 1)[0]
    for want in ('user: "64100:64100"', "read_only: true", "cap_drop: [ALL]", '"no-new-privileges:true"',
                 "mem_limit: 128m", "memswap_limit: 128m", "cpus: 0.25", "pids_limit: 128", "networks: [fixtures]",
                 "max-size: \"1m\"", "stop_grace_period: 20s"):
        check(f"compose: every fixture has {want}", want in anchor, anchor[:300])
    check("compose: the image is pinned by digest", re.search(r"^  image: python:3\.13-alpine@sha256:[0-9a-f]{64}$", anchor, re.M),
          anchor[:300])
    check("compose: the services are exactly these: the shared limits, their own script, guide, header and LAN port",
          text.split("\nservices:\n", 1)[-1] == COMPOSE_SERVICES, text.split("\nservices:\n", 1)[-1])
    check("compose: user is set once, in the shared block", text.count("user:") == 1)
    mounts = re.findall(r"^\s+- (\S+:\S+)$", text, re.M)
    mounts = [m for m in mounts if ":/" in m and not m.startswith('"')]
    check("compose: the only mounts are each service's script, guide and token header, read-only",
          len(mounts) == 6 and all(m.endswith(":ro") for m in mounts), mounts)
    for key in ("privileged", "network_mode", "pid", "ipc", "cap_add", "devices", "secrets", "env_file"):
        check(f"compose: no {key}:", not re.search(rf"^\s*{key}:", text, re.M))
    for bad in ("docker.sock", "samcloud-prod", "/var/lib/docker", "Bearer"):
        check(f"compose: no {bad}", bad not in text)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        procs = [start("echo_job.py", "dev/test-echo-job", SVC_JOB, tmp),
                 start("echo_api.py", "dev/test-echo-api", SVC_API, tmp)]
        try:
            time.sleep(1.2)                         # one heartbeat each
            print("test-echo-job")
            test_job(tmp)
            test_bearer()
            test_release()
            test_release_clock(tmp)
            test_limits()
            test_order_and_echo()
            test_shutdown(procs[0])
            print("test-echo-api")
            test_api()
            print("compose.yaml")
            test_compose()
        finally:
            for p in procs:
                p.terminate()
            logs = "".join(p.communicate(timeout=10)[0] for p in procs)
        check("neither service logged a token or a grant secret", not credentials_in(logs), logs[-500:])
    fails = len(results) - sum(results)
    print(f"\nALL PASSED: {len(results)} passed" if not fails else f"\nFAILED: {fails} of {len(results)}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
