#!/usr/bin/env python3
"""sc-pr on a clone whose origin names a moved repo (ticket #1004).

GitHub redirects git for a transferred repo, but answers a POST on the old API
path with 307, which urllib does not follow. gh-broker 0.4.0 answers `moved`;
sc-pr must use the new name for every API call. Run: python3 sandbox/test_sc_pr_moved.py
"""
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"  [{detail}]"))


def load():
    path = str(ROOT / "registry/helpers/samcloud-pr.py")
    loader = importlib.machinery.SourceFileLoader("scpr", path)
    spec = importlib.util.spec_from_loader("scpr", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def main():
    m = load()
    calls = []

    def fake_http(method, url, body=None, headers=None):
        calls.append((method, url, body))
        if url.endswith("/token"):
            out = {"token": "t"}
            if body["repos"] == ["old/r"]:
                out["moved"] = {"old/r": "new/r"}
            return out
        if "/repos/old/" in url:
            raise SystemExit(f"{method} {url} -> HTTP 307")  # what GitHub does
        return [] if method == "GET" else {"number": 7, "html_url": "https://github.com/new/r/pull/7"}

    def fake_git(*args, check=True):
        if args[:2] == ("remote", "get-url"):
            return "https://github.com/old/r.git"
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            return "me/change"
        return ""

    class Push:
        returncode = 0
        stderr = ""

    m.http, m.git, m.seat_identity = fake_http, fake_git, (lambda: "")
    m.auth_header = lambda: ("Authorization", "Bearer x")
    m.subprocess.run = lambda *a, **k: Push()
    sys.stdin = open("/dev/null")

    try:
        m.cmd_open(["--title", "t"])
        ok = True
    except SystemExit as e:
        ok, detail = False, str(e)
    check("open on a moved repo succeeds", ok, "" if ok else detail)
    api = [(meth, u) for meth, u, _ in calls if "api.github.com" in u]
    check("every API call uses the new name", api and all("/repos/new/r/" in u for _, u in api), api)
    check("the head owner is the new owner", any("head=new:me/change" in u for _, u in api), api)

    calls.clear()
    try:
        m.cmd_list([])
        ok = True
    except SystemExit as e:
        ok = False
    check("list on a moved repo uses the new name",
          ok and all("/repos/new/r/" in u for meth, u, _ in calls if "api.github.com" in u))

    calls.clear()
    def same_git(*args, check=True):
        return "https://github.com/same/r.git" if args[:2] == ("remote", "get-url") else fake_git(*args)
    m.git = same_git
    m.cmd_open(["--title", "t"])
    check("an unmoved repo is unchanged",
          all("/repos/same/r/" in u for meth, u, _ in calls if "api.github.com" in u))

    print(("ALL PASSED" if all(results) else "FAILED") + f": sc-pr moved repos ({len(results)} checks)")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
