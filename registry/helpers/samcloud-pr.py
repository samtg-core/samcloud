#!/usr/bin/env python3
# PR_VERSION: 1.3.0
# Served as GET /api/v1/helpers/pr. Install from the plane you write to:
#   curl -s -H @"$HOME/.samcloud/__TOKEN_HDR__" __API__/helpers/pr > ~/.local/bin/sc-pr && chmod +x ~/.local/bin/sc-pr
"""sc-pr — push a branch and open a pull request, as your seat.

    sc-pr setup                               once per seat: git asks sc-pr for GitHub credentials
    sc-pr author                              in a clone: commit under your samcloud name, as the App's bot
    sc-pr open --title "…" [-f body.md] [--ticket N] [--draft] [--base main]
    sc-pr list                                open pull requests on this repository

Run inside a clone whose `origin` is on github.com. `open` refuses to push the
base branch: work goes on a branch, a pull request carries it, and the admin
merges it.

The GitHub token is minted per call by the gh-broker (`POST /token`), scoped to
this one repository, and lives for an hour. It never touches argv: git receives
it from `sc-pr credential` on stdout, and the pull request call sends it in a
header from this process. Nothing is written to disk.

`remote: Repository not found.` on a clone that otherwise works is NOT your
access being revoked. It is GitHub answering 404 on the git transport for a token
minted seconds earlier and not yet propagated there -- `api.github.com` accepts
the very same token at that moment. Retry once; it clears. Measured settle ~6s,
and the rate is bursty, so tune nothing to it (#867).
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = os.environ.get("SAMCLOUD_API", "__API__").rstrip("/")
BROKER = os.environ.get("GH_BROKER_URL", "__GH_BROKER_URL__").rstrip("/")
GITHUB = "https://api.github.com"
PROTECTED = {"main", "master"}


def die(msg, code=1):
    print(f"sc-pr: {msg}", file=sys.stderr)
    sys.exit(code)


def git(*args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode:
        die(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def auth_header():
    """(name, value) of the seat's samcloud auth, from the 0600 header file."""
    candidates = [os.environ.get("SAMCLOUD_AUTH_HDR", ""),
                  str(Path.home() / ".samcloud" / "__TOKEN_HDR__"),
                  str(Path.home() / ".samcloud" / "token.hdr")]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.R_OK):
            for line in Path(c).read_text().splitlines():
                name, _, value = line.partition(":")
                if name.strip().lower() == "authorization" and value.strip():
                    return "Authorization", value.strip()
    die("no samcloud header file (SAMCLOUD_AUTH_HDR or ~/.samcloud/token.hdr)")


def origin_repo():
    """owner/name of origin on github.com."""
    url = git("remote", "get-url", "origin")
    m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?/?$", url)
    if not m:
        die("origin is not a github.com repository")
    return f"{m.group(1)}/{m.group(2)}"


def http(method, url, body=None, headers=None):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:600]
        die(f"{method} {url} -> HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        die(f"{method} {url} -> {e.reason}")


def seat_identity():
    if "__" in API:
        return ""
    name, value = auth_header()
    try:
        req = urllib.request.Request(f"{API}/auth/verify", headers={name: value})
        with urllib.request.urlopen(req, timeout=15) as r:
            me = json.load(r)
        # People carry username; agent tokens carry owner_id and no username.
        return me.get("username") or me.get("owner_id") or ""
    except Exception:
        return ""


def mint_for(repo):
    """(token, repo) — the repo as it is now. The broker answers `moved` when the
    name given is an old one (gh-broker 0.4.0); GitHub answers 307 to a POST on
    an old path, so the API calls must use the new name (#1004)."""
    name, value = auth_header()
    out = http("POST", f"{BROKER}/token",
               {"repos": [repo], "permissions": {"contents": "write", "pull_requests": "write"}},
               {name: value})
    MINTED["git_author"] = out.get("git_author")
    now = (out.get("moved") or {}).get(repo, repo)
    if now != repo:
        print(f"sc-pr: {repo} has moved to {now}; "
              f"git remote set-url origin https://github.com/{now}.git", file=sys.stderr)
    return out["token"], now


# The last mint's answer: `git_author` (gh-broker 0.5.0) is your samcloud name at
# the App bot's address, so a commit shows who it is from and which bot vouches.
MINTED = {"git_author": None}


def mint(repo):
    return mint_for(repo)[0]


def cmd_author(_args):
    """Set this clone's commit author to your samcloud name, at the App bot's address."""
    mint_for(origin_repo())
    author = MINTED["git_author"]
    if not author:
        name = git("config", "user.name", check=False) or "?"
        email = git("config", "user.email", check=False) or "?"
        print(f"the broker gave no commit author (gh-broker before 0.5.0?); commits keep your git config ({name} <{email}>)")
        return
    git("config", "user.name", author["name"])
    git("config", "user.email", author["email"])
    print(f"this clone now commits as {author['name']} <{author['email']}> (shown on GitHub through {author.get('github_login', 'the App bot')})")


def author_mismatch(base):
    """Commits on this branch not authored as the broker's git_author ([] when it gave none)."""
    author = MINTED["git_author"]
    if not author:
        return []
    lines = git("log", "--format=%h %ae", f"origin/{base}..HEAD", check=False).splitlines()
    return [ln for ln in lines if ln.split(" ", 1)[-1] != author["email"]]


def gh(method, path, token, body=None):
    return http(method, f"{GITHUB}{path}", body,
                {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"})


def cmd_credential(args):
    """git credential helper protocol: answer `get` for github.com, ignore the rest."""
    if not args or args[0] != "get":
        return
    fields = dict(line.split("=", 1) for line in sys.stdin.read().splitlines() if "=" in line)
    if fields.get("host") != "github.com":
        return
    path = fields.get("path", "")
    repo = path[:-4] if path.endswith(".git") else path
    if not repo:
        repo = origin_repo()
    sys.stdout.write(f"username=x-access-token\npassword={mint(repo)}\n")


def cmd_setup(_args):
    me = str(Path(sys.argv[0]).resolve())
    key = "credential.https://github.com.helper"
    git("config", "--global", "--unset-all", key, check=False)
    # An empty entry first clears helpers inherited from system config (the
    # macOS keychain would otherwise store each minted token).
    git("config", "--global", "--add", key, "")
    git("config", "--global", "--add", key, f"!{me} credential")
    print(f"git asks {me} for github.com credentials (minted per push, one repo, one hour)")


def cmd_open(args):
    import argparse
    p = argparse.ArgumentParser(prog="sc-pr open")
    p.add_argument("--title", required=True)
    p.add_argument("-f", "--file", help="pull request body (markdown); else stdin if piped")
    p.add_argument("--ticket", type=int, help="the samcloud ticket this closes; named in the body")
    p.add_argument("--base", default="main")
    p.add_argument("--draft", action="store_true")
    a = p.parse_args(args)

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch in PROTECTED or branch == a.base or branch == "HEAD":
        die(f"you are on '{branch}'. Work on a branch: git checkout -b <you>/<change>")
    if git("status", "--porcelain"):
        die("uncommitted changes; commit them first (git status)")

    body = ""
    if a.file:
        body = Path(a.file).read_text()
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    if a.ticket:
        body = f"samcloud ticket #{a.ticket}\n\n{body}"
    # Every pull request is authored by the App on GitHub; the seat is named here.
    who = seat_identity()
    if who:
        body = f"{body.rstrip()}\n\n---\nOpened by samcloud seat `{who}` with sc-pr."

    token, repo = mint_for(origin_repo())
    others = author_mismatch(a.base)
    if others:
        author = MINTED["git_author"]
        print(f"sc-pr: {len(others)} commit(s) here are not authored as {author['name']}"
              f" ({', '.join(o.split()[0] for o in others[:5])}). To fix: sc-pr author, then\n"
              f"  git rebase origin/{a.base} --exec 'git commit --amend --no-edit --reset-author'",
              file=sys.stderr)
    push = subprocess.run(["git", "push", "-u", "origin", f"HEAD:refs/heads/{branch}"],
                          capture_output=True, text=True)
    if push.returncode:
        die(f"push failed: {push.stderr.strip()}\n(run `sc-pr setup` once if git asked for a password)")

    owner = repo.split("/")[0]
    existing = gh("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}", token)
    if existing:
        pr = existing[0]
        print(f"pushed; pull request already open: #{pr['number']} {pr['html_url']}")
        return
    pr = gh("POST", f"/repos/{repo}/pulls", token,
            {"title": a.title, "head": branch, "base": a.base, "body": body, "draft": a.draft})
    print(f"#{pr['number']} {pr['html_url']}")


def cmd_list(_args):
    token, repo = mint_for(origin_repo())
    for pr in gh("GET", f"/repos/{repo}/pulls?state=open&per_page=50", token):
        print(f"#{pr['number']}  {pr['head']['ref']}  {pr['user']['login']}  {pr['title']}")


COMMANDS = {"credential": cmd_credential, "setup": cmd_setup, "author": cmd_author,
            "open": cmd_open, "list": cmd_list}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help") or sys.argv[1] not in COMMANDS:
        print(__doc__.strip())
        sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help") else 2)
    COMMANDS[sys.argv[1]](sys.argv[2:])
