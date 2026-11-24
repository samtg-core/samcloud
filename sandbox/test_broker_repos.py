#!/usr/bin/env python3
"""Agents create repos in their own scope; no token can delete one (task 898).

gh-broker 0.7.0: `administration` never goes out in a token, for any caller; a
mint always names its permissions; on an App marked `role_aware` a viewer of the
groups it serves gets read-only tokens; POST /repos creates a private repo for an
editor or owner, with an administration token the broker keeps to itself.
GitHub is faked; nothing leaves the process.

Run: python3 sandbox/test_broker_repos.py   (needs httpx, fastapi, pydantic)
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
TMP = tempfile.mkdtemp(prefix="ghb-repos-")
os.environ["GH_BROKER_CONFIG"] = TMP
os.environ["GH_BROKER_MINTS"] = f"{TMP}/mints.jsonl"
os.environ.setdefault("SAMCLOUD_API", "https://cloud.example.org/api/v1")
os.environ.setdefault("GH_BROKER_PUBLIC_URL", "https://ghb.example.org")
sys.modules.setdefault("jwt", types.SimpleNamespace(encode=lambda *a, **k: "jwt"))


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
    {"key": "projects", "slug": "agents-projects", "id": 2, "pem": "x",
     "allowed_scopes": ["group:projects"], "role_aware": True},
    {"key": "work", "slug": "agents-work", "id": 3, "pem": "x", "allowed_scopes": ["group:work"]},
]
INSTALLED = {2: "example-projects", 3: "example-work"}
GRANT = {"contents": "write", "pull_requests": "write", "issues": "write", "workflows": "write",
         "metadata": "read", "administration": "write"}


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.text = code, body, json.dumps(body)

    def json(self):
        return self._body


def fake_github(admin_on_app=True):
    calls = []

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            calls.append(("GET", url, None))
            if url.endswith("%5Bbot%5D"):
                return Resp(200, {"id": 7})
            if "/app/installations/" in url:
                return Resp(200, {"permissions": dict(GRANT)})
            if url.endswith("/installation"):
                app_id = int(headers["Authorization"].split()[-1])
                owner = url.split("/")[-2]
                return Resp(200, {"id": 900 + app_id}) if INSTALLED.get(app_id) == owner else Resp(404, {})
            return Resp(404, {})

        async def post(self, url, headers=None, json=None):
            calls.append(("POST", url, json))
            if url.endswith("/access_tokens"):
                perms = (json or {}).get("permissions")
                if perms is None:
                    perms = dict(GRANT)       # what GitHub does with an unnamed ask
                if "administration" in perms and not admin_on_app:
                    return Resp(422, {"message": "permissions not granted"})
                tok = "ADMIN-TOKEN" if "administration" in perms else "push-token"
                return Resp(201, {"token": tok, "expires_at": "later", "permissions": perms})
            if "/orgs/" in url and url.endswith("/repos"):
                org = url.split("/orgs/")[1].split("/")[0]
                return Resp(201, {"full_name": f"{org}/{json['name']}", "private": json["private"],
                                  "html_url": "h", "clone_url": "c", "default_branch": "main"})
            return Resp(404, {})

    return Client, calls


def main():
    print("gh-broker 0.7.0: repos in scope, nothing that deletes")
    ghb = load("ghb", ROOT / "gh-broker" / "main.py")
    ghb.save_apps(APPS)
    ghb.app_jwt = lambda a: str(a["id"])

    async def no_sleep(_):
        return None
    ghb.asyncio.sleep = no_sleep

    def call(fn, identity, admin_on_app=True):
        Client, calls = fake_github(admin_on_app)
        ghb.httpx.AsyncClient = Client

        async def verify(_):
            return identity
        ghb.verify_samcloud = verify
        try:
            return asyncio.run(fn), calls, None
        except ghb.HTTPException as e:
            return None, calls, e

    def mint(identity, repo, permissions=None):
        return call(ghb.mint_token(ghb.TokenRequest(repos=[repo], permissions=permissions), "Bearer x"), identity)

    def create(identity, admin_on_app=True, **kw):
        req = ghb.RepoCreate(**{"org": "example-projects", "name": "new-thing", **kw})
        return call(ghb.create_repo(req, "Bearer x"), identity, admin_on_app)

    def token_bodies(calls):
        return [b for m, u, b in calls if m == "POST" and u.endswith("/access_tokens")]

    admin = {"owner_id": "samclaude-admin", "scopes": ["*"], "groups": "*"}
    editor = {"owner_id": "claude-ios", "scopes": ["group:projects"], "groups": {"projects": "editor"}}
    viewer = {"owner_id": "android-claude", "scopes": ["group:projects"], "groups": {"projects": "viewer"}}
    legacy = {"owner_id": "old-seat", "scopes": ["group:projects"], "groups": {}}
    worker = {"owner_id": "work-seat", "scopes": ["group:work"], "groups": {"work": "viewer"}}

    # --- /token: administration never goes out -------------------------------
    out, calls, err = mint(admin, "example-projects/x")
    body = token_bodies(calls)[0]
    check("an unqualified mint names its permissions", "permissions" in body, body)
    check("and leaves administration off, even for the admin", "administration" not in body["permissions"]
          and "administration" not in out["permissions"], body)
    check("and says so", any("administration" in n for n in out.get("notes", [])), out.get("notes"))
    _, _, err = mint(admin, "example-projects/x", ["administration:write"])
    check("an explicit ask for administration is refused, admin included", err and err.status_code == 403, err)
    _, _, err = mint(editor, "example-projects/x", {"members": "read"})
    check("members is refused too", err and err.status_code == 403, err)

    # --- /token: roles on a role-aware App -----------------------------------
    out, calls, _ = mint(viewer, "example-projects/x")
    p = token_bodies(calls)[0]["permissions"]
    check("a viewer's unqualified mint is read-only", p and set(p.values()) == {"read"}, p)
    check("and goes without write-only workflows", "workflows" not in p, p)
    _, _, err = mint(viewer, "example-projects/x", ["contents:write"])
    check("a viewer's explicit write is refused, not narrowed", err and err.status_code == 403, err)
    out, _, err = mint(viewer, "example-projects/x", ["contents"])
    check("a viewer's read is served", out and out["role"] == "viewer", err)
    out, calls, err = mint(editor, "example-projects/x", ["contents:write"])
    check("an editor's write is served", out and token_bodies(calls)[0]["permissions"] == {"contents": "write"}, err)
    out, _, _ = mint(legacy, "example-projects/x", ["contents"])
    check("a group scope with no membership row reads as viewer", out and out["role"] == "viewer", out)

    # --- /token: an App that isn't role-aware is unchanged -----------------------
    out, _, err = mint(worker, "example-work/x", ["contents:write"])
    check("an App without role_aware still serves its members as before", out and out["role"] == "owner", err)

    # --- /repos --------------------------------------------------------------
    out, calls, err = create(editor)
    made = [b for m, u, b in calls if m == "POST" and u.endswith("/orgs/example-projects/repos")]
    check("an editor creates a repo in the org its group's App serves", out and out["repo"] == "example-projects/new-thing", err)
    check("private, as asked of GitHub", made and made[0]["private"] is True, made)
    check("the answer carries a push token for it, never the admin token",
          out and out.get("token") == "push-token" and "ADMIN-TOKEN" not in json.dumps(out), out)
    push = token_bodies(calls)[-1]
    check("the push token is scoped to the new repo, without administration",
          push.get("repositories") == ["new-thing"] and "administration" not in push["permissions"], push)
    log = [json.loads(l) for l in Path(f"{TMP}/mints.jsonl").read_text().splitlines()]
    check("the create is logged with who, where and role",
          any(e.get("action") == "create_repo" and e["identity"] == "claude-ios" and e["role"] == "editor"
              and e["repos"] == ["example-projects/new-thing"] for e in log), log[-1])
    _, calls, err = create(viewer)
    check("a viewer can't create", err and err.status_code == 403
          and not any(u.endswith("/repos") and m == "POST" for m, u, _ in calls), err)
    _, _, err = create(editor, private=False)
    check("public isn't created here", err and err.status_code == 403, err)
    _, _, err = create(editor, name="../evil")
    check("a bad name is refused", err and err.status_code == 400, err)
    _, _, err = call(ghb.create_repo(ghb.RepoCreate(org="example-work", name="w"), "Bearer x"), worker)[0:3]
    check("an App without role_aware doesn't create", err and err.status_code == 403, err)
    _, calls, err = create(editor, admin_on_app=False)
    check("an App without Administration says so, and creates nothing",
          err and err.status_code == 409 and not any(m == "POST" and "/orgs/" in u for m, u, _ in calls), err)

    if all(results):
        print(f"ALL PASSED: gh-broker repos in scope ({len(results)} checks)")
        return 0
    print(f"FAILED: {results.count(False)} of {len(results)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
