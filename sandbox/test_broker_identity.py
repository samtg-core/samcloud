#!/usr/bin/env python3
"""On GitHub the bot is shared and the name is the caller's (ticket 1010).

Every gh-broker mint answers `git_author`: the caller's samcloud name, at the
noreply address of the App bot that minted, so a commit says who it is from
and GitHub links it to the bot that vouches for it. App choice is unchanged:
scope picks the App. sc-pr sets a clone's author from the answer and warns
about commits authored otherwise. GitHub is faked; nothing leaves the process.

Run: python3 sandbox/test_broker_identity.py
"""
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results = []
TMP = tempfile.mkdtemp(prefix="ghb-identity-")
os.environ["GH_BROKER_CONFIG"] = TMP
os.environ["GH_BROKER_MINTS"] = f"{TMP}/mints.jsonl"
os.environ.setdefault("SAMCLOUD_API", "https://cloud.example.org/api/v1")
os.environ.setdefault("GH_BROKER_PUBLIC_URL", "https://ghb.example.org")
sys.modules.setdefault("jwt", types.SimpleNamespace(encode=lambda *a, **k: "jwt"))  # signing is not under test


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"  [{detail}]"))


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


APPS = [
    {"key": "core", "slug": "samcloud-agents-core", "id": 1, "pem": "x", "allowed_scopes": ["group:samcloud-contributors"]},
    {"key": "projects", "slug": "samcloud-agents-projects", "id": 2, "pem": "x", "allowed_scopes": ["group:projects"]},
]
BOT_IDS = {"samcloud-agents-core": 111, "samcloud-agents-projects": 222}


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.text = code, body, json.dumps(body)

    def json(self):
        return self._body


def fake_client(installed_on, users_api_up=True):
    """GitHub as far as a mint needs it. `installed_on` maps App id -> owners it is installed on."""
    calls = []

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            calls.append(("GET", url))
            if "/users/" in url and url.endswith("%5Bbot%5D"):
                slug = url.split("/users/")[1].replace("%5Bbot%5D", "")
                return Resp(200, {"id": BOT_IDS[slug]}) if users_api_up else Resp(503, {})
            if "/installation" in url:
                app_id = int(headers["Authorization"].split()[-1])   # app_jwt is faked to the App id
                owner = url.split("/")[-2]
                return Resp(200, {"id": 900 + app_id}) if owner in installed_on.get(app_id, ()) else Resp(404, {})
            return Resp(404, {})

        async def post(self, url, headers=None, json=None):
            calls.append(("POST", url))
            return Resp(201, {"token": "tok", "expires_at": "later", "permissions": {"contents": "write"}})

    return Client, calls


def main():
    print("gh-broker identity")
    ghb = load("ghb", ROOT / "gh-broker" / "main.py")
    ghb.save_apps(APPS)
    ghb.app_jwt = lambda a: str(a["id"])

    def run_mint(identity, installed_on, repo="example-org/samcloud", users_api_up=True):
        Client, calls = fake_client(installed_on, users_api_up)
        ghb.httpx.AsyncClient = Client

        async def verify(_):
            return identity
        ghb.verify_samcloud = verify
        ghb._BOT_IDS.clear()
        req = ghb.TokenRequest(repos=[repo], permissions=["contents:write"])
        return asyncio.run(ghb.mint_token(req, "Bearer x")), calls

    admin = {"owner_id": "samclaude-admin", "scopes": ["*"]}
    person = {"username": "alice", "scopes": ["*"]}
    member = {"owner_id": "hermes-seat", "scopes": ["group:projects"]}

    out, _ = run_mint(admin, {1: {"example-org"}})
    check("the mint names the caller at the bot's address",
          out.get("git_author") == {"name": "samclaude-admin",
                                    "email": "111+samcloud-agents-core[bot]@users.noreply.github.com",
                                    "github_login": "samcloud-agents-core[bot]"}, out.get("git_author"))
    check("scope still picks the App", out["app_key"] == "core", out.get("app_key"))

    out, _ = run_mint(person, {1: {"example-org"}})
    check("a person's username is the name", (out.get("git_author") or {}).get("name") == "alice")

    out, _ = run_mint(member, {2: {"example-projects"}}, repo="example-projects/x")
    check("another App's bot carries another seat's name",
          out.get("git_author", {}).get("email", "").startswith("222+samcloud-agents-projects[bot]")
          and out["git_author"]["name"] == "hermes-seat", out.get("git_author"))

    out, _ = run_mint(admin, {1: {"example-org"}}, users_api_up=False)
    check("if the bot can't be looked up, the token still comes, without git_author",
          out.get("token") == "tok" and "git_author" not in out, out)

    # --- sc-pr ---------------------------------------------------------------
    scpr = load("scpr", ROOT / "registry" / "helpers" / "samcloud-pr.py")
    config = {}
    author = {"name": "samclaude-admin", "email": "111+samcloud-agents-core[bot]@users.noreply.github.com",
              "github_login": "samcloud-agents-core[bot]"}

    def fake_git(*args, check=True):
        if args[:2] == ("remote", "get-url"):
            return "https://github.com/example-org/samcloud.git"
        if args[0] == "config" and len(args) == 3:
            config[args[1]] = args[2]
            return ""
        if args[0] == "config":
            return config.get(args[1], "")
        if args[0] == "log":
            return f"aaa111 {author['email']}\nbbb222 me@example.org"
        return ""

    scpr.git = fake_git
    scpr.auth_header = lambda: ("Authorization", "Bearer x")
    scpr.http = lambda method, url, body=None, headers=None: {"token": "t", "git_author": author}
    scpr.cmd_author([])
    check("sc-pr author sets the clone's author from the mint",
          config.get("user.name") == author["name"] and config.get("user.email") == author["email"], config)
    check("sc-pr names the commits authored otherwise", scpr.author_mismatch("main") == ["bbb222 me@example.org"])

    scpr.http = lambda method, url, body=None, headers=None: {"token": "t"}
    config.clear()
    scpr.cmd_author([])
    check("with an older broker, sc-pr author changes nothing", config == {}, config)
    check("and open has nothing to warn about", scpr.author_mismatch("main") == [])

    if all(results):
        print(f"ALL PASSED: gh-broker identity ({len(results)} checks)")
        return 0
    print(f"FAILED: {results.count(False)} of {len(results)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
