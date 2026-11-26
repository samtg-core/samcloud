#!/usr/bin/env python3
"""test-echo-api — the reference no-compute service for the plane's service standard (doc 793).

For testing and copying, not for real work. One synchronous call: it checks the caller's bearer
and answers with the JSON body it was sent, minus any grant. No lease, no files, no headers
echoed.

    POST /echo            any JSON object (at most 16 KB)  → 200 {"echo": {...}, "caller": "<you>"}
    GET  /service-docs    the caller's guide (no token)
    GET  /health          {"status": "ok", "version"} (no token)

Standard library only. Settings come from the environment (compose.yaml sets them on lagoon):

    SAMCLOUD_API   the plane (https://cloud.samtg.xyz/api/v1)
    SERVICE_ID     this service's id, e.g. lagoon/test-echo-api
    SERVICE_HDR    a 0600 file holding "Authorization: Bearer <the service token>" (for health)
    HOST, PORT     where it listens (127.0.0.1, 8812)
    HEARTBEAT_S    how often it reports health (300; 0 turns it off)
"""
import hashlib
import http.server
import json
import os
import pathlib
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"
API = os.environ.get("SAMCLOUD_API", "https://cloud.samtg.xyz/api/v1").rstrip("/")
SERVICE_ID = os.environ.get("SERVICE_ID", "lagoon/test-echo-api")
SERVICE_HDR = pathlib.Path(os.path.expanduser(os.environ.get("SERVICE_HDR", "~/.config/samcloud/test-echo-api/token.hdr")))
HOST, PORT = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8812"))
HEARTBEAT_S = int(os.environ.get("HEARTBEAT_S", "300"))
MAX_BODY = 16384
WHO_CACHE_S = float(os.environ.get("WHO_CACHE_S", "60"))
GUIDE = pathlib.Path(__file__).with_name("guide-echo-api.md")
GRANT = re.compile(r"sc_grant_[A-Za-z0-9_-]{43}")

lock = threading.Lock()
who_cache = {}


class Refused(Exception):
    """A request the service turns down, with its status and the reason the caller reads."""

    def __init__(self, status, error):
        super().__init__(error)
        self.status, self.error = status, error


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
    """The body as it may be shown back: no grant key, and no grant secret anywhere in it,
    keys included."""
    if isinstance(value, dict):
        return {scrub(k): redact(v) for k, v in value.items() if k != "grant"}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return scrub(value)
    return value


def heartbeat():
    while HEARTBEAT_S > 0:
        try:
            auth = SERVICE_HDR.read_text().split(":", 1)[1].strip()
            plane("POST", f"/services/{SERVICE_ID}/health", auth, {}, timeout=30)
        except Exception as e:            # a missed beat is logged and tried again; serving goes on
            print(f"heartbeat: {e}", flush=True)
        time.sleep(HEARTBEAT_S)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, obj=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if code == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="samcloud"')
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        """Every body is read, so keep-alive stays in step; one that can't be is refused and the
        connection closed."""
        n = self.headers.get("Content-Length") or "0"
        if not n.isdigit() or int(n) > MAX_BODY:
            self.close_connection = True
            raise Refused(413 if n.isdigit() else 400, f"the body is at most {MAX_BODY // 1024} KB, with a Content-Length")
        return self.rfile.read(int(n))

    def route(self, method, raw):
        path = urllib.parse.urlsplit(self.path).path
        if method == "GET" and path == "/health":
            return self.send(200, {"status": "ok", "service": SERVICE_ID, "version": VERSION})
        if method == "GET" and path == "/service-docs":
            return self.send(200, raw=GUIDE.read_bytes(), ctype="text/markdown; charset=utf-8")
        who = caller(self.headers.get("Authorization", ""))        # every other route needs a bearer
        if not who:
            raise Refused(401, "a bearer is required: Authorization: Bearer <your samcloud token>")
        if path != "/echo":
            raise Refused(404, "the one call is POST /echo")
        if method != "POST":
            raise Refused(405, "the one call is POST /echo")
        try:
            body = json.loads(raw)
        except ValueError:
            raise Refused(400, "the body isn't JSON") from None
        if not isinstance(body, dict):
            raise Refused(422, "the body is a JSON object, e.g. {\"text\": \"hello\"}")
        return self.send(200, {"echo": redact(body), "caller": who, "service": SERVICE_ID, "version": VERSION})

    def handle_method(self, method):
        try:
            self.route(method, self.body())
        except Refused as e:
            self.send(e.status, {"error": e.error})

    def do_GET(self):
        self.handle_method("GET")

    def do_POST(self):
        self.handle_method("POST")

    def do_DELETE(self):
        self.handle_method("DELETE")


def main():
    threading.Thread(target=heartbeat, daemon=True).start()
    print(f"{SERVICE_ID} {VERSION} on {HOST}:{PORT}", flush=True)
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
