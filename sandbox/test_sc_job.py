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
import json
import os
import signal
import socket
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
        self.posts = {}
        self.next_grant = 20


S = State()


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
        if method == "GET" and p == "/api/v1/services/dev%2Fplain":
            return self.send(200, {"id": "dev/plain", "external_url": "http://example.invalid"})
        if method == "POST" and p == "/api/v1/grants":
            with S.lock:
                gid = S.next_grant
                S.next_grant += 1
                S.grants[gid] = body["group_scope"]
            if body["group_scope"] == "nogrant":     # a mint answer missing its secret
                return self.send(200, {"id": gid, "expires_at": "later"})
            return self.send(200, {"id": gid, "grant": f"sc_grant_{gid}", "expires_at": "later"})
        if method == "DELETE" and p.startswith("/api/v1/grants/"):
            gid = int(p.rsplit("/", 1)[1])
            if S.grants.get(gid) == "flaky":
                return self.send(503, {"detail": "down for a moment"})
            if S.grants.get(gid) == "slowrevoke":
                time.sleep(1.5)
            S.revoked.add(gid)
            return self.send(200, {"id": gid, "revoked_at": "now"})
        if method == "GET" and p.startswith("/api/v1/files/"):
            fid = p.rsplit("/", 1)[1]
            if fid == "999":
                return self.send(404, {"detail": "no such file"})
            return self.send(200, {"id": int(fid), "content_type": "image/png" if fid == "101" else "application/json"})
        return self.send(404, {"detail": f"plane has no {method} {p}"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_DELETE(self):
        self.handle_any("DELETE")


SCENARIOS = {"ok": "J1", "fail": "J2", "escape": "J3", "redirect": "J4", "cancel": "J5", "link": "J6",
             "blips": "J7", "slow": "J8", "odd": "J9", "canceled": "J10", "flaky": "J11", "apiredir": "J13",
             "rate": "J14", "truncated": "J15", "lost404": "J16", "lost": "J17", "nostatus": "J18", "unicode": "J19"}
REPLIES = {"badjob": {"job": "x/evil1"}, "badpoll": {"job": "J12", "poll": "/jobs/../evilpoll"}}
FILES = {"/jobs/J1/files/sheet.png": b"PNGDATA", "/jobs/J1/files/stills/a-01.jpg": b"JPGDATA",
         "/jobs/J6/files/link/x.txt": b"LINKED", "/jobs/J7/files/out.txt": b"AFTERBLIPS",
         "/jobs/J11/files/out.txt": b"FLAKYOK", "/jobs/J14/files/out.txt": b"AFTER429",
         "/jobs/J19/files/my sheet é.png": b"SPACED", "/jobs/J19/files/stills/café 01.jpg": b"ACCENTED"}


def job_doc(job, n):
    if job == "J1":
        if n < 2:
            return {"job": job, "status": ["queued", "running"][n], "stage": "waiting" if n == 0 else None}
        return {"job": job, "status": "done", "waited_s": 3, "render_s": 4,
                "results": {"sheet": f"/jobs/{job}/files/sheet.png", "note": "plain text, not a file",
                            "cameras": [{"shots": [{"still": f"/jobs/{job}/files/stills/a-01.jpg"}]}],
                            "elsewhere": "/jobs/OTHER/files/x.png"},
                "plane_files": {"missing": 999, "sheet": 101, "stills": {"a-01.jpg": 102}, "odd": "²"}}
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
    if job == "J15":
        return {"job": job, "status": "done", "results": [f"/jobs/{job}/files/big.bin"]}
    if job == "J16" and n >= 2:
        return {"job": job, "status": "done", "results": []}
    if job == "J18":
        return {"job": job}
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
        if scenario == "submit500":
            return self.send(500, {"error": "can't take it"})
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
            if parts[2] in ("J16", "J17") and (parts[2] == "J17" or n < 2):
                return self.send(404, {"error": "no such job (yet)"})
            d = job_doc(parts[2], n)
            return self.send(200, d) if d else self.send(404, {"error": "no such job"})
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
    hint = m.Client(f"{PLANE_URL}/api/v1", TOKEN, HDR).revoke_hint("1; rm -rf ~")
    check("the printed revoke line quotes the grant id", "'" in hint and "/grants/1; rm" in hint and " rm -rf ~'" in hint, hint)


def main():
    global HDR
    os.umask(0o022)
    signal.signal(signal.SIGHUP, signal.SIG_DFL)   # the helpers we start must not inherit an ignored SIGHUP (nohup)
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
    check("the embed line names the first image, past a 404", "embed: ![](/api/v1/files/101/raw)" in r.stdout, r.stdout)
    g = minted_since(mark)
    check("the grant rode in the body, top-level", len(g) == 1 and S.bodies.get("J1", {}).get("grant") == f"sc_grant_{g[0]}",
          S.bodies.get("J1"))
    posted = [b for m_, p, _a, b in S.plane if m_ == "POST" and p == "/api/v1/grants"]
    check("the grant named the service, the group and the default ttl",
          posted[-1:] == [{"service_id": "dev/render", "group_scope": "projects", "ttl_s": 7200}], posted)
    check("revoked when the job is done", g and g[0] in S.revoked, (g, S.revoked))
    check("progress goes to stderr, not stdout", "waiting" in r.stderr and "waiting" not in r.stdout, r.stderr)
    check("the bearer reached the plane and the service", all(a == TOKEN for _m, _p, a, _b in S.plane + S.service))
    check("the bearer is never printed", "sc-job-test-token" not in r.stdout + r.stderr)

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
    r = run(["call", "dev/render", "/render", '{"scenario":"lost"}', "--store", "projects"], tmp)
    g = minted_since(mark)
    check("a job that keeps answering 404 stops the wait (exit 2), keeps the grant and prints the fetch line",
          r.returncode == 2 and g and g[0] not in S.revoked and f"sc-job fetch dev/render J17 --grant {g[0]}" in r.stderr,
          (r.returncode, g, r.stderr))
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
    r = run(["call", "dev/render", "/render", '{"scenario":"odd"}'], tmp)
    check("an unknown status is named once, then expired exits 1",
          r.returncode == 1 and r.stderr.count("doesn't know") == 1 and "job J9: expired" in r.stdout, r.stdout + r.stderr)
    r = run(["call", "dev/render", "/render", '{"scenario":"canceled"}'], tmp)
    check("'canceled' ends the wait, exit 1", r.returncode == 1 and "job J10: canceled" in r.stdout, r.stdout + r.stderr)

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
