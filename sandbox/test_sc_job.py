#!/usr/bin/env python3
"""sc-job against a fake plane and a fake service on loopback (ticket #1157).

The caller helper submits, polls, downloads, and with --store mints a grant and
revokes it however the job ends. It must refuse a results path that leaves the
job's files, never send the bearer on a redirect to another host, and exit 1 on
a cancelled job. Run: python3 sandbox/test_sc_job.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "registry/helpers/samcloud-job"
TOKEN = "Bearer sc-job-test-token"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"  [{detail}]"))


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.plane = []       # (method, path, auth, body)
        self.service = []
        self.other = []
        self.polls = {}
        self.bodies = {}


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
        body = self.body() if method in ("POST", "PATCH") else None
        with S.lock:
            S.plane.append((method, self.path, self.headers.get("Authorization"), body))
        if self.headers.get("Authorization") != TOKEN:
            return self.send(401, {"detail": "no"})
        p = self.path
        if method == "GET" and p == "/api/v1/services":
            return self.send(200, [{"id": "dev/render", "last_health_status": 200, "external_url": SVC_URL,
                                    "description": "Renders things. Second sentence."}])
        if method == "GET" and p == "/api/v1/services/dev%2Frender":
            return self.send(200, {"id": "dev/render", "external_url": SVC_URL})
        if method == "GET" and p == "/api/v1/services/dev%2Fplain":
            return self.send(200, {"id": "dev/plain", "external_url": "http://example.invalid"})
        if method == "POST" and p == "/api/v1/grants":
            return self.send(200, {"id": 12, "grant": "sc_grant_test", "expires_at": "later"})
        if method == "DELETE" and p == "/api/v1/grants/12":
            return self.send(200, {"id": 12, "revoked_at": "now"})
        if method == "GET" and p.startswith("/api/v1/files/"):
            fid = p.rsplit("/", 1)[1]
            return self.send(200, {"id": int(fid), "content_type": "image/png" if fid == "101" else "application/json"})
        return self.send(404, {"detail": f"plane has no {method} {p}"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_DELETE(self):
        self.handle_any("DELETE")


JOBS = {
    "ok": "J1", "fail": "J2", "escape": "J3", "redirect": "J4", "cancel": "J5",
}


def job_doc(job, n):
    if job == "J1":
        if n < 2:
            return {"job": job, "status": ["queued", "running"][n], "stage": "waiting" if n == 0 else None}
        return {"job": job, "status": "done", "waited_s": 3, "render_s": 4,
                "results": {"sheet": f"/jobs/{job}/files/sheet.png", "note": "plain text, not a file",
                            "cameras": [{"shots": [{"still": f"/jobs/{job}/files/stills/a-01.jpg"}]}],
                            "elsewhere": "/jobs/OTHER/files/x.png"},
                "plane_files": {"sheet": 101, "stills": {"a-01.jpg": 102}}}
    if job == "J2":
        return {"job": job, "status": "failed", "error": "boom"}
    if job == "J3":
        return {"job": job, "status": "done", "results": {"x": f"/jobs/{job}/files/../../../escape.txt"}}
    if job == "J4":
        return {"job": job, "status": "done", "results": [f"/jobs/{job}/files/r.bin"]}
    if job == "J5":
        return {"job": job, "status": "cancelled"}
    return None


class Service(Base):
    def do_POST(self):
        body = self.body()
        with S.lock:
            S.service.append(("POST", self.path, self.headers.get("Authorization"), body))
        job = JOBS.get((body or {}).get("scenario"))
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
        parts = self.path.split("/")
        if len(parts) == 3 and parts[1] == "jobs":
            n = S.polls.get(parts[2], 0)
            S.polls[parts[2]] = n + 1
            d = job_doc(parts[2], n)
            return self.send(200, d) if d else self.send(404, {"error": "no job"})
        if self.path == "/jobs/J1/files/sheet.png":
            return self.send(200, raw=b"PNGDATA")
        if self.path == "/jobs/J1/files/stills/a-01.jpg":
            return self.send(200, raw=b"JPGDATA")
        if self.path == "/jobs/J4/files/r.bin":
            return self.send(302, raw=b"", headers=[("Location", f"{OTHER_URL}/r.bin")])
        return self.send(404, {"error": "no file"})

    def do_DELETE(self):
        with S.lock:
            S.service.append(("DELETE", self.path, self.headers.get("Authorization"), None))
        return self.send(200, {"job": self.path.rsplit("/", 1)[1], "status": "cancelled"})


class Other(Base):
    def do_GET(self):
        with S.lock:
            S.other.append((self.path, self.headers.get("Authorization")))
        return self.send(200, raw=b"REDIRECTED")


plane_srv, svc_srv, other_srv = server(Plane), server(Service), server(Other)
PLANE_URL = f"http://127.0.0.1:{plane_srv.server_port}"
SVC_URL = f"http://127.0.0.1:{svc_srv.server_port}"
OTHER_URL = f"http://localhost:{other_srv.server_port}"   # another host name: a cross-host redirect


def run(args, cwd, stdin=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SAMCLOUD_")}
    env.update({"HOME": cwd, "SAMCLOUD_API": f"{PLANE_URL}/api/v1", "SAMCLOUD_AUTH_HDR": HDR,
                "SC_JOB_POLL_S": "0.02,0.05"})
    return subprocess.run([sys.executable, str(HELPER), *args], cwd=cwd, env=env, input=stdin,
                          capture_output=True, text=True, timeout=60)


def grant_calls():
    return [(m, p) for m, p, _a, _b in S.plane if "/grants" in p]


def main():
    global HDR
    tmp = tempfile.mkdtemp(prefix="sc-job-")
    HDR = os.path.join(tmp, "token.hdr")
    Path(HDR).write_text(f"Authorization: {TOKEN}\n")
    os.chmod(HDR, 0o600)

    print("sc-job — submit, poll, download")
    r = run(["call", "dev/render", "/render", '{"scenario":"ok"}', "--store", "projects"], tmp)
    out = Path(tmp) / "J1"
    check("a done job exits 0", r.returncode == 0, r.stderr)
    check("files land in ./<job>/ by their path under the job's files",
          (out / "sheet.png").read_bytes() == b"PNGDATA" and (out / "stills/a-01.jpg").read_bytes() == b"JPGDATA"
          if out.exists() else False, r.stdout + r.stderr)
    check("a path of another job's files is not fetched",
          not any("/jobs/OTHER" in p for _m, p, _a, _b in S.service))
    check("the summary names the job, its timings and the files",
          "job J1: done · waited 3 s, render 4 s" in r.stdout and "sheet.png" in r.stdout, r.stdout)
    check("plane files are listed by role", "  sheet 101" in r.stdout and "  stills/a-01.jpg 102" in r.stdout,
          r.stdout)
    check("the embed line names the first image", "embed: ![](/api/v1/files/101/raw)" in r.stdout, r.stdout)
    check("the grant rode in the body, top-level", S.bodies.get("J1", {}).get("grant") == "sc_grant_test",
          S.bodies.get("J1"))
    posted = [b for m, p, _a, b in S.plane if m == "POST" and p == "/api/v1/grants"]
    check("the grant named the service, the group and the default ttl",
          posted == [{"service_id": "dev/render", "group_scope": "projects", "ttl_s": 7200}], posted)
    check("--store revokes the grant once the job is done", ("DELETE", "/api/v1/grants/12") in grant_calls(),
          grant_calls())
    check("progress goes to stderr, not stdout", "waiting" in r.stderr and "waiting" not in r.stdout, r.stderr)
    check("the bearer reached the plane and the service",
          all(a == TOKEN for _m, _p, a, _b in S.plane + S.service))
    check("the bearer is never printed", "sc-job-test-token" not in r.stdout + r.stderr)

    print("sc-job — failure and cancel")
    before = len(grant_calls())
    r = run(["call", "dev/render", "/render", "-", "--store", "projects", "--out", "j2"], tmp,
            stdin='{"scenario":"fail"}')
    check("a failed job exits 1 and says why", r.returncode == 1 and "boom" in r.stdout, r.stdout + r.stderr)
    check("--store revokes the grant when the job fails",
          grant_calls()[before:] == [("POST", "/api/v1/grants"), ("DELETE", "/api/v1/grants/12")],
          grant_calls()[before:])
    r = run(["call", "dev/render", "/render", '{"scenario":"cancel"}'], tmp)
    check("a cancelled job exits 1", r.returncode == 1 and "job J5: cancelled" in r.stdout, r.stdout + r.stderr)
    check("with no poll in the reply, it polls /jobs/<job>", any(p == "/jobs/J5" for _m, p, _a, _b in S.service))

    print("sc-job — refusals")
    r = run(["call", "dev/render", "/render", '{"scenario":"escape"}', "--out", "j3"], tmp)
    check("a results path that leaves the job's files is refused (exit 2)",
          r.returncode == 2 and "outside the job's files" in r.stderr, r.stdout + r.stderr)
    check("nothing is written outside the out dir",
          not (Path(tmp) / "escape.txt").exists() and not Path(tmp).parent.joinpath("escape.txt").exists())
    r = run(["call", "dev/render", "/render", '{"scenario":"redirect"}', "--out", "j4"], tmp)
    check("a file behind a cross-host redirect still downloads",
          r.returncode == 0 and (Path(tmp) / "j4/r.bin").read_bytes() == b"REDIRECTED", r.stdout + r.stderr)
    check("the bearer is not sent on a redirect to another host",
          S.other and all(a is None for _p, a in S.other), S.other)
    r = run(["call", "dev/plain", "/render", "{}"], tmp)
    check("a service on plain http off loopback is refused", r.returncode == 2 and "refusing" in r.stderr,
          r.stderr)
    r = run(["call", "dev/render", "/../admin", "{}"], tmp)
    check("a submit path with '..' is refused", r.returncode == 2 and "refusing" in r.stderr, r.stderr)

    print("sc-job — list, status, cancel")
    r = run(["list"], tmp)
    check("list shows id, health, address and the first sentence",
          r.stdout.strip() == f"dev/render  200  {SVC_URL}  Renders things.", r.stdout)
    r = run(["status", "dev/render", "J2"], tmp)
    check("status prints the job", r.returncode == 0 and json.loads(r.stdout)["status"] == "failed", r.stdout)
    r = run(["cancel", "dev/render", "J1"], tmp)
    check("cancel sends DELETE on the job", r.returncode == 0 and ("DELETE", "/jobs/J1") in
          [(m, p) for m, p, _a, _b in S.service], r.stdout + r.stderr)
    r = run(["status", "dev/render", "../x"], tmp)
    check("a malformed job id is refused", r.returncode == 2, r.stdout + r.stderr)

    passed = sum(results)
    print(f"\n{'ALL PASSED' if passed == len(results) else 'FAILED'}: {passed} passed, "
          f"{len(results) - passed} failed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
