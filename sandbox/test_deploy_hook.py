#!/usr/bin/env python3
"""The zero-touch deploy gate: only a signed, merged PR into main, by an allowed merger.

Drives deploy/deploy-hook/receiver.py over real HTTP on a random port with the
deploy step replaced, so nothing is fetched or merged.

Run: python3 sandbox/test_deploy_hook.py
"""
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"  [{detail}]"))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="deploy-hook-test-"))
    secret = b"test-secret-not-real"
    (tmp / "secret").write_bytes(secret)
    os.environ.update({"DEPLOY_HOOK_SECRET_FILE": str(tmp / "secret"), "DEPLOY_HOOK_REPO": "o/r",
                       "DEPLOY_HOOK_MERGERS": "alice", "DEPLOY_HOOK_LOG": str(tmp / "log"),
                       "DEPLOY_HOOK_GITHUB_TOKEN_FILE": str(tmp / "no-such-token")})
    spec = importlib.util.spec_from_file_location("receiver", ROOT / "deploy/deploy-hook/receiver.py")
    rx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rx)
    deployed = []
    rx.deploy = lambda sha, why, pr=None: deployed.append((sha, (pr or {}).get("number")))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), rx.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/github"

    def post(event, payload, sign=True, key=secret):
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
        if sign:
            headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    sha = "a" * 40
    def merged(**over):
        pr = {"number": 7, "merged": True, "base": {"ref": "main"},
              "merged_by": {"login": "alice"}, "merge_commit_sha": sha}
        pr.update(over)
        return {"action": "closed", "repository": {"full_name": "o/r"}, "pull_request": pr}

    code, body = post("pull_request", merged())
    time.sleep(0.2)
    check("a signed merged PR into main by an allowed merger deploys, and the hook learns which PR",
          code == 202 and body.get("deploy") is True and deployed == [(sha, 7)], (code, body, deployed))

    deployed.clear()
    cases = [
        ("unsigned", ("pull_request", merged()), {"sign": False}, 403),
        ("wrong secret", ("pull_request", merged()), {"key": b"other"}, 403),
        ("a push event (a direct push to main)", ("push", {"ref": "refs/heads/main"}), {}, 200),
        ("closed without merging", ("pull_request", merged(merged=False)), {}, 200),
        ("merged into another branch", ("pull_request", merged(base={"ref": "staging"})), {}, 200),
        ("merged by someone not allowed", ("pull_request", merged(merged_by={"login": "mallory"})), {}, 200),
        ("another repository", ("pull_request", {**merged(), "repository": {"full_name": "x/y"}}), {}, 200),
        ("a malformed merge sha", ("pull_request", merged(merge_commit_sha="main; rm -rf /")), {}, 200),
    ]
    for name, (event, payload), kw, want in cases:
        code, body = post(event, payload, **kw)
        check(f"{name} does not deploy", code == want and not body.get("deploy"), (code, body))
    time.sleep(0.2)
    check("nothing was deployed by any refused event", deployed == [], deployed)

    code, body = post("ping", {"zen": "hi"})
    check("GitHub's ping is answered without deploying", code == 200 and body.get("deploy") is False)
    with urllib.request.urlopen(url.replace("/github", "/health"), timeout=5) as r:
        health = json.loads(r.read())
        check("health answers, names the receiver and says it is not announcing",
              r.status == 200 and health.get("ok") is True and health.get("receiver") == rx.RECEIVER_VERSION
              and health.get("announces") is False, health)
    srv.shutdown()

    # 0.12.96: the release is read from post-merge's last line and told to the PR.
    rel = rx.parse_result("  building... ok\npost-merge: result=ok version=0.1.1 commit=abc1234 prs=#7,#8\n")
    check("post-merge's result line is parsed",
          rel == {"result": "ok", "version": "0.1.1", "commit": "abc1234", "prs": [7, 8]}, rel)
    check("no result line claims no release", rx.parse_result("Already up to date.\n") is None)
    text = rx.announcement(rel, 7, "alice")
    check("a release is announced with its version, its merger and what it carried",
          "v0.1.1" in text and "@alice" in text and "#8" in text and "#7" not in text, text)
    bad = rx.announcement({**rel, "result": "build-failed"}, 7, "alice")
    check("a failed deploy is never worded as a release",
          "Not released" in bad and "build-failed" in bad and "Released as" not in bad, bad)
    check("without a token nothing is posted or pushed",
          rx.announce(7, "x").startswith("no token") and rx.push_tag("0.1.1").startswith("no token"))

    # No instance in the defaults (#1010): the repo is required, the plane and the
    # receiver's public URL are optional and named only when set.
    check("with no plane set, a release names no plane", "GET " not in text and "https://" not in text, text)
    check("with no public URL set, a failure points at the receiver's own /health",
          "the receiver's /health" in bad and "https://" not in bad, bad)
    def load(env):
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            sp = importlib.util.spec_from_file_location("receiver_env", ROOT / "deploy/deploy-hook/receiver.py")
            m = importlib.util.module_from_spec(sp)
            sp.loader.exec_module(m)
            return m
        finally:
            for k, v in saved.items():
                os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    named = load({"DEPLOY_HOOK_PLANE": "https://cloud.example.org/", "DEPLOY_HOOK_PUBLIC_URL": "https://hooks.example.org"})
    check("a set plane and public URL are named",
          "`GET https://cloud.example.org/api/v1/` names `version` 0.1.1" in named.announcement(rel, 7, "alice")
          and "https://hooks.example.org/health" in named.announcement({**rel, "result": "build-failed"}, 7, "alice"))
    try:
        load({"DEPLOY_HOOK_REPO": ""}).main()
        refused = None
    except SystemExit as e:
        refused = str(e)
    check("the receiver refuses to start without DEPLOY_HOOK_REPO", refused and "DEPLOY_HOOK_REPO" in refused, refused)

    if all(results):
        print(f"ALL PASSED: deploy hook gate ({len(results)} checks)")
        return 0
    print(f"FAILED: {results.count(False)} of {len(results)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
