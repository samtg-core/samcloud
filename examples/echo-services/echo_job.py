#!/usr/bin/env python3
"""test-echo-job — the reference compute job for the plane's service standard (doc 793).

For testing and copying, not for real work. It does what a compute job on the plane does, and
nothing else: it checks the caller's bearer, takes a small lease as its own service id, waits,
writes one file, gives it to the caller (and to the caller's grant, if one was sent), and
gives the lease back on every path.

    POST   /jobs                 {"text": "...", "wait_s": 3, "grant": "sc_grant_…"}  → 202 {job, status, poll}
    GET    /jobs/<job>           {job, status, error, results, plane_files, plane_error, expires}
    GET    /jobs/<job>/files/echo.json
    DELETE /jobs/<job>           withdraw a queued or running job; delete a finished one
    GET    /service-docs         the caller's guide (no token)
    GET    /health               {"status": "ok", "version"} (no token)

Standard library only. Settings come from the environment (compose.yaml sets them on lagoon):

    SAMCLOUD_API      the plane (https://cloud.samtg.xyz/api/v1)
    SERVICE_ID        this service's id, e.g. lagoon/test-echo-job
    SERVICE_HDR       a 0600 file holding "Authorization: Bearer <the service token>"
    LEASE_RESOURCE    what it leases (lagoon/test-slot), LEASE_MB (64)
    HOST, PORT        where it listens (127.0.0.1, 8811)
    HEARTBEAT_S       how often it reports health (300; 0 turns it off)
    MAX_JOBS          jobs waiting or running at once (4), MAX_JOBS_PER_CALLER (2); beyond is 429

Jobs run one at a time, in order, from one worker: however many are submitted, the service
holds at most one lease and one long-poll on the plane. On SIGTERM it gives its lease back
before it exits.
"""
import datetime
import hashlib
import http.server
import json
import os
import pathlib
import queue
import re
import secrets
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"
API = os.environ.get("SAMCLOUD_API", "https://cloud.samtg.xyz/api/v1").rstrip("/")
SERVICE_ID = os.environ.get("SERVICE_ID", "lagoon/test-echo-job")
SERVICE_HDR = pathlib.Path(os.path.expanduser(os.environ.get("SERVICE_HDR", "~/.config/samcloud/test-echo-job/token.hdr")))
RESOURCE = os.environ.get("LEASE_RESOURCE", "lagoon/test-slot")
LEASE_MB = int(os.environ.get("LEASE_MB", "64"))
HOST, PORT = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8811"))
HEARTBEAT_S = int(os.environ.get("HEARTBEAT_S", "300"))
RELEASE_BUDGET_S = float(os.environ.get("RELEASE_BUDGET_S", "300"))   # queue.md: retry a release through a restart
BACKOFF_S = float(os.environ.get("BACKOFF_S", "2"))     # the first wait after a failed plane call; it doubles to 60
SHUTDOWN_S = float(os.environ.get("SHUTDOWN_S", "10"))  # how long SIGTERM spends giving the lease back
MAX_JOBS = int(os.environ.get("MAX_JOBS", "4"))
MAX_PER_CALLER = int(os.environ.get("MAX_JOBS_PER_CALLER", "2"))
WHO_CACHE_S = float(os.environ.get("WHO_CACHE_S", "60"))
KEEP_S, KEEP_MAX = 3600, 500     # a finished job and its file are kept an hour; at most 500 of them
GUIDE = pathlib.Path(__file__).with_name("guide-echo-job.md")
GRANT = re.compile(r"sc_grant_[A-Za-z0-9_-]{43}")

jobs, lock = {}, threading.Lock()
who_cache = {}
work = queue.Queue()             # the one worker takes jobs from here, in order
held = set()                     # lease ids the plane hasn't yet confirmed released
stopping = threading.Event()


class Refused(Exception):
    """A request the service turns down, with its status and the reason the caller reads."""

    def __init__(self, status, error, headers=()):
        super().__init__(error)
        self.status, self.error, self.headers = status, error, headers


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def service_auth():
    return SERVICE_HDR.read_text().split(":", 1)[1].strip()


def plane(method, path, auth, body=None, timeout=60):
    """One call to the plane. Returns (status, parsed JSON or None)."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, method=method, headers={"Authorization": auth})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read()
    try:
        return code, json.loads(raw) if raw.strip() else None
    except ValueError:
        return code, None


def caller(auth):
    """Who a bearer is, from the plane's /auth/verify, cached WHO_CACHE_S by the token's hash.
    None for a token the plane refuses. The bearer itself is never kept."""
    if not auth or not auth.startswith("Bearer "):
        return None
    k, t = hashlib.sha256(auth.encode()).hexdigest(), time.time()
    with lock:
        hit = who_cache.get(k)
    if hit and t - hit[1] < WHO_CACHE_S:
        return hit[0]
    try:
        code, v = plane("GET", "/auth/verify", auth, timeout=15)
    except (urllib.error.URLError, OSError):
        raise Refused(503, "the plane can't be reached to check your token; try again shortly") from None
    if code >= 500:
        raise Refused(503, f"the plane answered {code} checking your token; try again shortly")
    name = (v.get("username") or v.get("owner_id")) if code == 200 and isinstance(v, dict) and v.get("valid") else None
    with lock:
        if len(who_cache) > 1000:
            who_cache.clear()
        who_cache[k] = (name, t)
    return name


def scrub(text):
    return GRANT.sub("sc_grant_[redacted]", text)


def redact(value):
    """The input as it may be shown back: no grant key, and no grant secret anywhere in it,
    keys included."""
    if isinstance(value, dict):
        return {scrub(k): redact(v) for k, v in value.items() if k != "grant"}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return scrub(value)
    return value


def parse(body):
    """The submit body, checked before any work. Raises Refused(422) with what to fix."""
    if not isinstance(body, dict):
        raise Refused(422, "the body is a JSON object: {\"text\": \"...\"}")
    extra = set(body) - {"text", "wait_s", "grant"}
    if extra:
        names = ", ".join(sorted(scrub(k)[:40] for k in extra))[:200]
        raise Refused(422, f"unknown field(s): {names}; the fields are text, wait_s and grant")
    text = body.get("text")
    if not isinstance(text, str) or not 1 <= len(text) <= 1000:
        raise Refused(422, "text is required: a string of 1 to 1000 characters")
    wait_s = body.get("wait_s", 3)
    if isinstance(wait_s, bool) or not isinstance(wait_s, int) or not 0 <= wait_s <= 30:
        raise Refused(422, "wait_s is a whole number of seconds from 0 to 30 (default 3)")
    grant = body.get("grant")
    if grant is not None and not (isinstance(grant, str) and GRANT.fullmatch(grant)):
        raise Refused(422, "grant is the secret POST /grants returned (sc_grant_…), or left out")
    return text, wait_s, grant


def check_grant(grant, who):
    """Before any work: would this grant upload, and did this caller make it? (tools.md)"""
    try:
        code, chk = plane("POST", "/grants/check", service_auth(), {"grant": grant}, timeout=15)
    except (urllib.error.URLError, OSError):
        raise Refused(503, "the plane can't be reached to check your grant; try again shortly") from None
    if code != 200 or not isinstance(chk, dict):
        raise Refused(503, f"the plane answered {code} checking your grant; try again shortly")
    if not chk.get("valid"):
        raise Refused(422, f"grant: {chk.get('reason') or 'not valid for ' + SERVICE_ID}")
    if chk.get("creator") != who:
        raise Refused(403, "grant: it was made by someone else; send a grant you made with POST /grants")


def view(job):
    out = {k: job[k] for k in ("job", "status", "submitted", "expires") if job.get(k)}
    if job.get("error"):
        out["error"] = job["error"]
    if job["status"] == "done":
        out["results"] = [f"/jobs/{job['job']}/files/echo.json"]
    for k in ("plane_files", "plane_error", "lease"):
        if job.get(k):
            out[k] = job[k]
    if "released" in job:
        out["lease_released"] = job["released"]
    return out


# ---- the lease: taken as this service, given back on every path ----------------------------

def take_lease(job):
    code, r = plane("POST", f"/resources/{urllib.parse.quote(RESOURCE, safe='/')}/leases", service_auth(),
                    {"memory_mb": LEASE_MB, "ttl_seconds": 120, "wait_s": 600})
    if code != 200 or not isinstance(r, dict) or not r.get("lease_id"):
        raise RuntimeError(f"the plane refused the lease ({code}): {(r or {}).get('detail', r)}")
    lease, status = r["lease_id"], r.get("status")
    with lock:
        held.add(lease)
    job["lease"] = lease
    delay = BACKOFF_S
    while status == "queued":
        if job["status"] == "cancelled" or stopping.is_set():
            return lease
        t = time.monotonic()
        try:
            code, r = plane("GET", f"/leases/{lease}/wait?timeout_s=10", service_auth(), timeout=40)
        except (urllib.error.URLError, OSError):
            code, r = 0, None
        if code == 404:
            raise RuntimeError(f"the plane no longer has lease {lease}")
        if code == 200 and isinstance(r, dict):
            status, delay = r.get("status"), BACKOFF_S
            pause = 1 - (time.monotonic() - t)            # never more than one wait a second
        else:                                               # no answer, or not one to read: back off
            pause, delay = delay, min(delay * 2, 60)
        if pause > 0 and status == "queued":
            stopping.wait(pause)
    if status != "active":
        raise RuntimeError(f"the lease ended as {status} before it was granted")
    return lease


def release(lease, budget=None):
    """DELETE until the plane says it's gone: a 2xx, or the plane's own 404 for this lease.
    No answer, a 5xx or a 429 is retried with back-off for RELEASE_BUDGET_S (queue.md). The
    id is kept, in the log, if the budget runs out, so it can be released by hand."""
    budget = RELEASE_BUDGET_S if budget is None else budget
    delay, start = BACKOFF_S, time.monotonic()
    while True:
        try:
            code, r = plane("DELETE", f"/leases/{lease}", service_auth(), timeout=min(60, budget))
        except (urllib.error.URLError, OSError):
            code, r = 0, None
        if 200 <= code < 300 or (code == 404 and f"Lease {lease} not found" in json.dumps(r)):
            with lock:
                held.discard(lease)
            return True
        if code not in (0, 429) and code < 500:
            print(f"release {lease}: {code} {r}; not released, keep the id", flush=True)
            return False
        if time.monotonic() - start + delay > budget:
            print(f"release {lease}: still {code or 'no answer'} after {budget:g} s; keep the id", flush=True)
            return False
        time.sleep(delay)
        delay = min(delay * 2, 60)


def upload(name, data, grant):
    """The result as a plane file the caller owns, in the grant's group (tools.md)."""
    boundary = secrets.token_hex(16)
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"grant\"\r\n\r\n{grant}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/json\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{API}/files", data=body, method="POST",
                                 headers={"Authorization": service_auth(),
                                          "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["id"]


def move(job, status, **fields):
    """Set the job's status unless it was withdrawn: a DELETE always wins over the worker."""
    with lock:
        if job["status"] == "cancelled":
            return False
        job.update(status=status, t=time.time(), **fields)       # t: the sweep counts from here
        return True


def run(job, text, wait_s, grant):
    try:
        lease = take_lease(job)
        if not move(job, "running"):
            return
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and job["status"] != "cancelled":
            time.sleep(0.2)
        if job["status"] == "cancelled":
            return
        job["file"] = json.dumps({"service": SERVICE_ID, "version": VERSION, "job": job["job"],
                                  "caller": job["owner"], "lease": lease, "at": now(),
                                  "input": redact({"text": text, "wait_s": wait_s})}, indent=1).encode()
    except Exception as e:
        move(job, "failed", error=f"{e.__class__.__name__}: {e}")
        return
    finally:
        if job.get("lease"):                      # set as soon as the plane gave an id, queued or not
            job["released"] = release(job["lease"])
    if grant:
        try:
            job["plane_files"] = {"echo.json": upload("echo.json", job["file"], grant)}
        except urllib.error.HTTPError as e:
            job["plane_error"] = f"the plane refused the upload ({e.code}): {e.read()[:200].decode('utf-8', 'replace')}"
        except (urllib.error.URLError, OSError) as e:
            job["plane_error"] = f"the plane couldn't be reached for the upload: {e}"
    move(job, "done", expires=(datetime.datetime.now(datetime.timezone.utc)
                               + datetime.timedelta(seconds=KEEP_S)).strftime("%Y-%m-%dT%H:%M:%SZ"))


def worker():
    """The one worker: jobs run one at a time, in the order they came."""
    while True:
        job, text, wait_s, grant = work.get()
        if job["status"] != "cancelled" and not stopping.is_set():
            run(job, text, wait_s, grant)


def shutdown(*_):
    """SIGTERM (docker stop, a redeploy): withdraw what's waiting and give the lease back."""
    stopping.set()
    with lock:
        for j in jobs.values():
            if j["status"] in ("queued", "running"):
                j.update(status="cancelled", error="the service stopped", t=time.time())
        leases = sorted(held)
    for lease in leases:
        print(f"stopping: release {lease}: {'done' if release(lease, SHUTDOWN_S) else 'NOT released'}", flush=True)
    os._exit(0)


def sweep():
    """Finished jobs, and their files, go after KEEP_S."""
    while True:
        time.sleep(60)
        cut = time.time() - KEEP_S
        with lock:
            ended = [k for t, k in sorted((j["t"], k) for k, j in jobs.items()
                                          if j["status"] in ("done", "failed", "cancelled"))]
            old = sum(jobs[k]["t"] < cut for k in ended)
            for k in ended[:max(old, len(ended) - KEEP_MAX)]:        # oldest first
                del jobs[k]


def heartbeat():
    while HEARTBEAT_S > 0:
        try:
            plane("POST", f"/services/{SERVICE_ID}/health", service_auth(), {}, timeout=30)
        except Exception as e:            # a missed beat is logged and tried again; serving goes on
            print(f"heartbeat: {e}", flush=True)
        time.sleep(HEARTBEAT_S)


# ---- HTTP ------------------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, obj=None, raw=None, ctype="application/json", headers=()):
        data = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if code == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="samcloud"')
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def route(self, method, body):
        path = urllib.parse.urlsplit(self.path).path
        if method == "GET" and path == "/health":
            return self.send(200, {"status": "ok", "service": SERVICE_ID, "version": VERSION})
        if method == "GET" and path == "/service-docs":
            return self.send(200, raw=GUIDE.read_bytes(), ctype="text/markdown; charset=utf-8")
        who = caller(self.headers.get("Authorization", ""))        # every other route needs a bearer
        if not who:
            raise Refused(401, "a bearer is required: Authorization: Bearer <your samcloud token>")
        if method == "POST" and path == "/jobs":
            return self.submit(who, body)
        m = re.fullmatch(r"/jobs/([A-Za-z0-9_-]{1,64})(/files/echo\.json)?", path)
        with lock:
            job = jobs.get(m.group(1)) if m else None
        if not job or job["owner"] != who:                           # someone else's job doesn't exist
            raise Refused(404, "no such job of yours")
        if method == "GET" and not m.group(2):
            return self.send(200, view(job))
        if method == "GET" and job["status"] == "done":
            return self.send(200, raw=job["file"])
        if method == "GET":
            raise Refused(409, f"the job is {job['status']}; its file comes when it's done")
        if method == "DELETE" and not m.group(2):
            with lock:
                live = job["status"] in ("queued", "running")
                if live:
                    job.update(status="cancelled", t=time.time())
                else:
                    jobs.pop(job["job"], None)
            return self.send(200, view(job) if live else {"job": job["job"], "deleted": True})
        raise Refused(405, f"{method} isn't used on {path}")

    def submit(self, who, raw):
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            raise Refused(400, "the body isn't JSON") from None
        text, wait_s, grant = parse(body)
        if grant:
            check_grant(grant, who)
        job = {"job": secrets.token_hex(6), "status": "queued", "owner": who, "submitted": now(), "t": time.time()}
        with lock:
            live = [j for j in jobs.values() if j["status"] in ("queued", "running")]
            mine = sum(j["owner"] == who for j in live)
            if mine >= MAX_PER_CALLER or len(live) >= MAX_JOBS:
                raise Refused(429, f"{mine} of your jobs and {len(live)} in all are waiting or running; the limits are "
                                   f"{MAX_PER_CALLER} and {MAX_JOBS}. Try again when one ends.", [("Retry-After", "30")])
            jobs[job["job"]] = job
        work.put((job, text, wait_s, grant))
        return self.send(202, {"job": job["job"], "status": "queued", "poll": f"/jobs/{job['job']}"})

    def body(self):
        """Every body is read, so keep-alive stays in step; one that can't be is refused and the
        connection closed."""
        n = self.headers.get("Content-Length") or "0"
        if not n.isdigit() or int(n) > 65536:
            self.close_connection = True
            raise Refused(413 if n.isdigit() else 400, "the body is at most 64 KB, with a Content-Length")
        return self.rfile.read(int(n))

    def handle_method(self, method):
        try:
            self.route(method, self.body())
        except Refused as e:
            self.send(e.status, {"error": e.error}, headers=e.headers)

    def do_GET(self):
        self.handle_method("GET")

    def do_POST(self):
        self.handle_method("POST")

    def do_DELETE(self):
        self.handle_method("DELETE")


def main():
    signal.signal(signal.SIGTERM, shutdown)
    for target in (worker, sweep, heartbeat):
        threading.Thread(target=target, daemon=True).start()
    print(f"{SERVICE_ID} {VERSION} on {HOST}:{PORT}, leasing {LEASE_MB} MB on {RESOURCE}", flush=True)
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
