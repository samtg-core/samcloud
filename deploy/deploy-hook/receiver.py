#!/usr/bin/env python3
"""Zero-touch deploy for the plane: a merged PR into main is the release (ticket 960; 0.12.96).

GitHub sends a signed `pull_request` webhook here. Only a PR that was merged into
`main` of the configured repository, by an allowed merger, deploys: the receiver
fetches from GitHub (read-only deploy key), fast-forwards the deploy host's checkout
to the merge commit, and `.githooks/post-merge` releases it — the next version from
this host's release tags, images built as that version, the services changed since
the previous release recreated, the tag written last. The hook's final line
(`post-merge: result=… version=…`) is what this process reports: in `/health`, in its
log and, when the host holds a GitHub token, as a comment on every pull request the
release carried, with the tag pushed to GitHub. A direct push to main sends no
pull_request event, so it never deploys. Runs on the host under systemd, outside the
compose stack, so a deploy cannot restart it.

Config (environment, normally from ~/.config/samcloud/deploy-hook.env):
  DEPLOY_HOOK_SECRET_FILE        file holding the webhook secret (0600)
  DEPLOY_HOOK_REPO_DIR           the deploy host's checkout (hooksPath = .githooks)
  DEPLOY_HOOK_REPO               owner/name the event must name (required)
  DEPLOY_HOOK_MERGERS            comma-separated GitHub logins allowed to merge-to-deploy
  DEPLOY_HOOK_PORT               listen port (all interfaces; Caddy proxies to it)
  DEPLOY_HOOK_LOG                log file
  DEPLOY_HOOK_GITHUB_TOKEN_FILE  optional, 0600: a GitHub token with `contents` and
                                 `pull requests` write on the repository. Present, the
                                 receiver pushes each release tag and comments the result
                                 on the pull requests. Absent, it only logs. Git reads it
                                 through askpass.sh; it is never on a command line.
  DEPLOY_HOOK_PLANE              optional: the plane URL the release comment names
  DEPLOY_HOOK_PUBLIC_URL         optional: where this receiver is reachable (Caddy's route
                                 to it); a failure comment points at its /health

Stdlib only.
"""
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RECEIVER_VERSION = "2.1.0"

HOME = Path.home()
SECRET_FILE = Path(os.environ.get("DEPLOY_HOOK_SECRET_FILE", HOME / ".config/samcloud/deploy-hook.secret"))
REPO_DIR = os.environ.get("DEPLOY_HOOK_REPO_DIR", str(HOME / "samcloud"))
REPO = os.environ.get("DEPLOY_HOOK_REPO", "").strip()
MERGERS = {m.strip() for m in os.environ.get("DEPLOY_HOOK_MERGERS", "").split(",") if m.strip()}
PORT = int(os.environ.get("DEPLOY_HOOK_PORT", "9911"))
LOG = Path(os.environ.get("DEPLOY_HOOK_LOG", HOME / "logs/deploy-hook.log"))
TOKEN_FILE = Path(os.environ.get("DEPLOY_HOOK_GITHUB_TOKEN_FILE",
                                 HOME / ".config/samcloud/deploy-hook.github-token"))
PLANE = os.environ.get("DEPLOY_HOOK_PLANE", "").strip().rstrip("/")
PUBLIC_URL = os.environ.get("DEPLOY_HOOK_PUBLIC_URL", "").strip().rstrip("/")
HEALTH = f"{PUBLIC_URL}/health" if PUBLIC_URL else "the receiver's /health"
ASKPASS = Path(__file__).resolve().parent / "askpass.sh"
MAX_BODY = 5 * 1024 * 1024
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# post-merge's contract line. Anything else the hook prints is narrative.
RESULT_LINE = re.compile(r"^post-merge: result=(\S+) version=(\S+) commit=(\S+) prs=(\S+)", re.M)

deploy_lock = threading.Lock()
state = {"started": time.time(), "last": None}


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + msg.rstrip() + "\n")


def git(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", REPO_DIR, *args], capture_output=True, text=True,
                          env={**os.environ, **env} if env else None)


def verdict(event: str, payload: dict) -> tuple:
    """(deploy?, reason, merge_sha). Pure, so the gate is testable without GitHub."""
    if event == "ping":
        return False, "ping", None
    if event != "pull_request":
        return False, f"ignored event {event!r}", None
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name")
    if repo != REPO:
        return False, f"wrong repository {repo!r}", None
    if payload.get("action") != "closed" or not pr.get("merged"):
        return False, f"not a merge (action {payload.get('action')!r})", None
    if (pr.get("base") or {}).get("ref") != "main":
        return False, f"merged into {(pr.get('base') or {}).get('ref')!r}, not main", None
    merger = (pr.get("merged_by") or {}).get("login")
    if merger not in MERGERS:
        return False, f"merged by {merger!r}, not an allowed merger", None
    sha = pr.get("merge_commit_sha") or ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return False, "no merge commit sha", None
    return True, f"PR #{pr.get('number')} merged by {merger}", sha


def parse_result(hook_output: str) -> dict | None:
    """post-merge's last line -> {result, version, commit, prs}; None when the hook
    said nothing (nothing merged, or a hook that predates the contract). Pure."""
    m = RESULT_LINE.search(hook_output)
    if not m:
        return None
    return {"result": m.group(1), "version": m.group(2), "commit": m.group(3),
            "prs": [int(n) for n in re.findall(r"\d+", m.group(4))]}


def announcement(rel: dict, pr_number: int, merger: str) -> str:
    """What a pull request is told. A failure is never worded as a release. Pure."""
    v, head = rel["version"], rel["commit"]
    if rel["result"] == "ok":
        others = [f"#{n}" for n in rel["prs"] if n != pr_number]
        carried = f" Carried with it: {', '.join(others)}." if others else ""
        check = f" `GET {PLANE}/api/v1/` names `version` {v}." if PLANE else ""
        return f"Released as **v{v}** ({head}) on the deploy host, merged by @{merger}.{carried}{check}"
    if rel["result"] == "already-released":
        return f"Already released as v{v} ({head}); nothing was rebuilt."
    return (f"**Not released.** The deploy of this merge stopped at `{rel['result']}` "
            f"(it would have been v{v}, {head}). Nothing new is running: see "
            f"{HEALTH} and the deploy host's deploy-hook log.")


def token() -> str | None:
    try:
        return TOKEN_FILE.read_text().strip() or None
    except OSError:
        return None


def github(method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    """One GitHub API call with the host's token in a header; (status, error text)."""
    tok = token()
    if not tok:
        return 0, "no token"
    req = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": f"samcloud-deploy-hook/{RECEIVER_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"")[:300].decode(errors="replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def announce(pr_number: int, text: str) -> str:
    """Comment on the pull request; without a token, nothing leaves the host."""
    if not token():
        return "no token: not announced on the pull request"
    code, err = github("POST", f"/repos/{REPO}/issues/{pr_number}/comments", {"body": text})
    return f"commented on #{pr_number}" if code == 201 else f"comment on #{pr_number} failed ({code}: {err})"


def push_tag(version: str) -> str:
    """Push the release tag over HTTPS, the token answered to git by askpass.sh (never
    argv, never a stored credential helper). Without a token the tag stays on the host."""
    if not token():
        return "no token: tag kept on the host"
    if not ASKPASS.exists():
        return f"askpass helper missing at {ASKPASS}: tag kept on the host"
    r = git("-c", "credential.helper=", "push", "-q",
            f"https://x-access-token@github.com/{REPO}.git", f"refs/tags/v{version}",
            env={"GIT_ASKPASS": str(ASKPASS), "DEPLOY_HOOK_GITHUB_TOKEN_FILE": str(TOKEN_FILE),
                 "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode == 0:
        return f"pushed v{version} to GitHub"
    return f"tag push failed ({r.returncode}): {ANSI.sub('', (r.stdout + r.stderr).strip())[-300:]}"


def deploy(sha: str, why: str, pr: dict | None = None) -> None:
    pr = pr or {}
    number, merger = pr.get("number"), pr.get("merger") or ""
    hook_env = {"DEPLOY_PR_NUMBER": str(number or ""), "DEPLOY_PR_TITLE": (pr.get("title") or "")[:200],
                "DEPLOY_MERGER": merger}
    with deploy_lock:
        log(f"deploy {sha[:7]}: {why}")
        steps = [("fetch", ["fetch", "-q", "--tags", "origin", "main"], None),
                 ("contains", ["merge-base", "--is-ancestor", sha, "origin/main"], None),
                 ("checkout", ["checkout", "-q", "main"], None),
                 ("merge", ["merge", "--ff-only", sha], hook_env)]
        rel = None
        for name, args, env in steps:
            r = git(*args, env=env)
            out = ANSI.sub("", (r.stdout + r.stderr).strip())
            if r.returncode != 0:
                log(f"  {name} FAILED ({r.returncode}): {out[-2000:]}")
                state["last"] = {"sha": sha, "ok": False, "step": name, "at": time.time(), "pr": number}
                if number:
                    log("  " + announce(number, f"**Not released.** The deploy host could not `{name}` "
                                                f"this merge; see {HEALTH}."))
                return
            if out and name == "merge":
                log("  " + "\n  ".join(out.splitlines()))     # post-merge's output: the deploy itself
                rel = parse_result(out)
        head = git("rev-parse", "--short", "HEAD").stdout.strip()
        result = rel["result"] if rel else "no result line"
        state["last"] = {"sha": sha, "ok": result in ("ok", "already-released"), "head": head,
                         "version": rel["version"] if rel else None, "result": result,
                         "at": time.time(), "pr": number}
        log(f"  now at {head}: {result}" + (f" v{rel['version']}" if rel else ""))
        if rel is None:
            return
        if rel["result"] == "ok":
            log("  " + push_tag(rel["version"]))
        for n in sorted({*rel["prs"], *([number] if number else [])}):
            log("  " + announce(n, announcement(rel, n, merger)))


class Handler(BaseHTTPRequestHandler):
    server_version = f"samcloud-deploy-hook/{RECEIVER_VERSION}"

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):            # quiet: our own log says what matters
        pass

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", ""):
            return self._reply(200, {"ok": True, "repo": REPO, "receiver": RECEIVER_VERSION,
                                     "announces": token() is not None, "last": state["last"]})
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/github":
            return self._reply(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._reply(413, {"error": "bad length"})
        body = self.rfile.read(length)
        try:
            secret = SECRET_FILE.read_bytes().strip()
        except OSError:
            log("no secret file: refusing everything")
            return self._reply(503, {"error": "not configured"})
        sig = self.headers.get("X-Hub-Signature-256") or ""
        want = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            log(f"bad signature from {self.client_address[0]}")
            return self._reply(403, {"error": "bad signature"})
        try:
            payload = json.loads(body)
        except ValueError:
            return self._reply(400, {"error": "bad json"})
        ok, why, sha = verdict(self.headers.get("X-GitHub-Event") or "", payload)
        if not ok:
            if why != "ping":
                log(f"no deploy: {why}")
            return self._reply(200, {"deploy": False, "reason": why})
        pr_obj = payload.get("pull_request") or {}
        pr = {"number": pr_obj.get("number"), "title": pr_obj.get("title"),
              "merger": (pr_obj.get("merged_by") or {}).get("login")}
        threading.Thread(target=deploy, args=(sha, why), kwargs={"pr": pr}, daemon=True).start()
        self._reply(202, {"deploy": True, "sha": sha, "reason": why})


def main():
    if not MERGERS:
        raise SystemExit("DEPLOY_HOOK_MERGERS is empty: nobody could deploy")
    if not REPO:
        raise SystemExit("DEPLOY_HOOK_REPO is empty: set the owner/name whose merges deploy")
    log(f"receiver {RECEIVER_VERSION} listening on :{PORT} for {REPO}, mergers {sorted(MERGERS)}, "
        f"{'announcing on GitHub' if token() else 'no GitHub token: results stay in /health and this log'}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
