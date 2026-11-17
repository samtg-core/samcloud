"""
gh-broker — GitHub App installation token broker (multi-App, scope-gated).

Mints short-lived, repo-scoped GitHub App installation tokens for agents
authenticated via samcloud. Multiple Apps can be registered (one per org or
trust boundary); each has an `allowed_scopes` list checked against the
caller's samcloud scopes at mint time. Private keys never leave this host.

Run: uvicorn main:app --host 0.0.0.0 --port 7781
"""

import asyncio
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

import httpx
import jwt
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


# The plane this broker verifies tokens against (and, where it posts health, reports
# to). Required: the broker serves no instance by default.
SAMCLOUD_API = os.environ.get("SAMCLOUD_API", "").strip().rstrip("/")
if not SAMCLOUD_API:
    raise SystemExit("gh-broker: set SAMCLOUD_API to the plane's API base, e.g. https://cloud.example.org/api/v1")
CONFIG_DIR = Path(os.environ.get("GH_BROKER_CONFIG", str(Path.home() / ".config/samcloud/gh-broker")))
APP_FILE = CONFIG_DIR / "app.json"          # legacy single-App location (read once for migration)
APPS_FILE = CONFIG_DIR / "apps.json"        # canonical multi-App store
STATE_FILE = CONFIG_DIR / "manifest-state.json"
ALIASES_FILE = CONFIG_DIR / "repo-aliases.json"  # old owner/name -> new owner/name, for moved repos (#1004)
MINTS_LOG = Path(os.environ.get("GH_BROKER_MINTS", str(Path.home() / ".local/samcloud/gh-broker/mints.jsonl")))
PUBLIC_URL = os.environ.get("GH_BROKER_PUBLIC_URL", "").strip().rstrip("/")
if not PUBLIC_URL:
    raise SystemExit("gh-broker: set GH_BROKER_PUBLIC_URL to this broker's public URL, e.g. https://ghb.example.org")

# What a caller gets when they name no permissions. Unset (the default) keeps the
# historical behaviour: GitHub hands back the installation's full grant. Set it —
# e.g. GH_BROKER_DEFAULT_PERMISSIONS="metadata:read" — and an unqualified mint
# narrows to that instead, making the wide grant opt-in. Flipping it is a
# breaking change for callers who omit `permissions`; `/mints/recent` carries a
# `narrowed` flag so you can see who those are before you throw the switch (#768).
DEFAULT_PERMISSIONS_RAW = os.environ.get("GH_BROKER_DEFAULT_PERMISSIONS", "").strip()

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
MINTS_LOG.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="gh-broker", version="0.7.0")


# ---------- App store ------------------------------------------------------

def load_apps() -> list[dict]:
    """
    Returns the list of registered Apps.

    Migration: if apps.json doesn't exist but legacy app.json does, build
    apps.json from it with key='default', allowed_scopes=['*']. Leaves
    app.json in place as a backup.
    """
    if APPS_FILE.exists():
        return json.loads(APPS_FILE.read_text())
    if APP_FILE.exists():
        legacy = json.loads(APP_FILE.read_text())
        migrated = [{**legacy, "key": "default", "allowed_scopes": ["*"]}]
        save_apps(migrated)
        return migrated
    return []


def save_apps(apps: list[dict]) -> None:
    APPS_FILE.write_text(json.dumps(apps, indent=2))
    os.chmod(APPS_FILE, 0o600)


def get_app(key: str) -> Optional[dict]:
    for a in load_apps():
        if a["key"] == key:
            return a
    return None


def apps_for_scopes(scopes: list[str]) -> list[dict]:
    """Apps the caller is allowed to use given their samcloud scopes."""
    scope_set = set(scopes or [])
    out = []
    for a in load_apps():
        allowed = a.get("allowed_scopes") or ["*"]
        if "*" in allowed or "*" in scope_set or scope_set.intersection(allowed):
            out.append(a)
    return out


def caller_name(identity: dict) -> str:
    """The samcloud name of a verified caller. Agent tokens carry owner_id, people username."""
    return identity.get("username") or identity.get("owner_id") or identity.get("user_id") or ""


_BOT_IDS: dict[str, int] = {}


async def git_author(app_cfg: dict, name: str) -> Optional[dict]:
    """The commit author for a mint: the caller's samcloud name, at the App bot's address.

    The bot is the same for everyone an App serves; the name says who it acted for.
    GitHub links a commit to the bot when the email is the bot's noreply address,
    `<bot user id>+<slug>[bot]@users.noreply.github.com`. The bot user id is not the
    App id; it is read once from the public users API and kept. None when it can't be
    read, and the caller's git config stands.
    """
    slug = app_cfg.get("slug")
    if not slug or not name:
        return None
    bot_id = app_cfg.get("bot_user_id") or _BOT_IDS.get(slug)
    if not bot_id:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"https://api.github.com/users/{slug}%5Bbot%5D",
                                headers={"Accept": "application/vnd.github+json"})
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        bot_id = _BOT_IDS[slug] = r.json()["id"]
    return {"name": name, "email": f"{bot_id}+{slug}[bot]@users.noreply.github.com",
            "github_login": f"{slug}[bot]"}


def app_jwt(app_cfg: dict) -> str:
    now = int(time.time())
    payload = {"iat": now - 30, "exp": now + 540, "iss": app_cfg["id"]}
    return jwt.encode(payload, app_cfg["pem"], algorithm="RS256")


# ---------- Moved repos ----------------------------------------------------

def load_aliases() -> dict[str, str]:
    """
    Where a transferred repo went: {"old-owner/name": "new-owner/name"}.

    GitHub redirects git and web URLs after a transfer, but an installation is
    per account: a mint for the old name looks up the old owner's installation,
    which no longer holds the repo. The transfer run writes one line here per
    repo it moves (temp file, then rename), so callers that still name the old path (sc-pr, clones with
    old remotes) keep getting tokens. Read on every mint; keys match
    case-insensitively, as GitHub's names do.
    """
    if not ALIASES_FILE.exists():
        return {}
    try:
        return {k.lower(): v for k, v in json.loads(ALIASES_FILE.read_text()).items()}
    except (OSError, ValueError, AttributeError) as e:
        # A half-written or hand-broken file must not fail every mint: unmoved
        # repos still resolve, and moved ones fail as they would without it.
        print(f"gh-broker: ignoring unreadable {ALIASES_FILE}: {e!r}", flush=True)
        return {}


def resolve_repos(repos: list[str]) -> tuple[list[str], dict[str, str]]:
    """Map moved repos to their new names; also return what was moved."""
    aliases = load_aliases()
    out, moved = [], {}
    for r in repos:
        new = aliases.get(r.lower())
        if new:
            moved[r] = new
        out.append(new or r)
    return out, moved


# ---------- Permissions ----------------------------------------------------

VALID_LEVELS = ("read", "write", "admin")


def normalize_permissions(raw) -> Optional[dict]:
    """
    Accept either GitHub's own mapping or a plain list of verbs.

        {"contents": "write"}                -> {"contents": "write"}
        ["contents:write", "pull_requests"]  -> {"contents": "write", "pull_requests": "read"}

    The list form is what a caller reaches for when they are thinking "this is a
    push"; an unqualified name means `read`, the least-privilege reading of an
    unqualified ask. Returns None when nothing was asked for.
    """
    if not raw:
        return None
    if isinstance(raw, dict):
        bad = {k: v for k, v in raw.items() if v not in VALID_LEVELS}
        if bad:
            raise HTTPException(400, f"permission level must be one of {VALID_LEVELS}; got {bad}")
        return dict(raw)

    out: dict = {}
    for item in raw:
        if not isinstance(item, str):
            raise HTTPException(400, f"permissions list takes strings like 'contents:write'; got {item!r}")
        name, _, level = item.partition(":")
        name, level = name.strip(), (level.strip() or "read")
        if not name:
            raise HTTPException(400, f"empty permission name in {item!r}")
        if level not in VALID_LEVELS:
            raise HTTPException(400, f"permission level must be one of {VALID_LEVELS}; got '{level}' in {item!r}")
        out[name] = level
    return out


DEFAULT_PERMISSIONS = normalize_permissions(
    [s for s in DEFAULT_PERMISSIONS_RAW.split(",") if s.strip()]
)


# ---------- What a token may carry, and for whom (#898) --------------------

# Never in a token, for any caller, admin included: these delete, transfer or
# rename repos, or change who is in the org. The broker holds `administration`
# for one job of its own, creating a repo (POST /repos), and hands it to nobody.
DENIED_PERMISSIONS = frozenset({"administration", "organization_administration", "members"})

# GitHub permissions with no read level: a read-only caller simply goes without.
WRITE_ONLY_PERMISSIONS = frozenset({"workflows"})

ROLE_RANK = {"viewer": 0, "editor": 1, "owner": 2}


def caller_role(identity: dict, app_cfg: dict) -> str:
    """The caller's best role in the groups an App serves: viewer, editor or owner.

    Only an App marked `role_aware` in apps.json is read this way; any other App
    treats every caller its scopes admit as before (owner). The admin, or a token
    scoped `*`, is owner. A group the token is scoped to without a membership row
    reads as viewer, as the plane reads it (`registry/scope.py: memberships`).
    """
    if not app_cfg.get("role_aware"):
        return "owner"
    scopes = identity.get("scopes") or []
    groups = identity.get("groups")
    if "*" in scopes or groups == "*":
        return "owner"
    groups = groups if isinstance(groups, dict) else {}
    served = [s.split(":", 1)[1] for s in app_cfg.get("allowed_scopes") or [] if s.startswith("group:")]
    roles = [groups.get(g) or ("viewer" if f"group:{g}" in scopes else None) for g in served]
    roles = [r for r in roles if r in ROLE_RANK]
    return max(roles, key=ROLE_RANK.get) if roles else "viewer"


def read_only(perms: dict) -> dict:
    return {k: "read" for k in perms if k not in WRITE_ONLY_PERMISSIONS}


def shape_permissions(asked: Optional[dict], grant: dict, role: str) -> tuple[dict, list[str]]:
    """What the token carries: the ask (or the installation's grant when nothing was
    asked) without the denied permissions, and read-only for a viewer.

    An explicit ask for a denied permission, or a viewer's explicit ask to write, is
    refused with 403 rather than quietly narrowed: the caller should know it won't
    get what it named. Returns (permissions, notes)."""
    notes = []
    if asked:
        denied = sorted(set(asked) & DENIED_PERMISSIONS)
        if denied:
            raise HTTPException(403, f"{denied} never goes out in a token. Creating a repo is "
                                     "POST /repos; deleting or transferring one is not done through the broker")
        if role == "viewer" and any(v != "read" for v in asked.values()):
            raise HTTPException(403, "you are a viewer in the group this App serves, so tokens here are "
                                     "read-only; ask for read, or for editor in that group")
        return dict(asked), notes
    perms = {k: v for k, v in grant.items() if k not in DENIED_PERMISSIONS}
    if len(perms) < len(grant):
        notes.append("the installation's " + ", ".join(sorted(set(grant) - set(perms)))
                     + " never goes out in a token, so it was left off")
    if role == "viewer":
        perms = read_only(perms)
        notes.append("you are a viewer in the group this App serves, so this token is read-only")
    return perms, notes


async def installation_grant(app_cfg: dict, inst_id: int) -> dict:
    """The permissions an installation carries, read from GitHub with the App's JWT."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"https://api.github.com/app/installations/{inst_id}",
                        headers={"Authorization": f"Bearer {app_jwt(app_cfg)}",
                                 "Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        raise HTTPException(502, f"could not read installation {inst_id}'s permissions: {r.status_code}")
    return r.json().get("permissions") or {}


# ---------- samcloud auth --------------------------------------------------

async def verify_samcloud(authorization: Optional[str]) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{SAMCLOUD_API}/auth/verify", headers={"Authorization": authorization})
    if r.status_code != 200:
        raise HTTPException(401, f"samcloud rejected token: {r.status_code}")
    return r.json()


def log_mint(entry: dict) -> None:
    entry["ts"] = int(time.time())
    with MINTS_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------- Health / index -------------------------------------------------

@app.get("/")
async def root():
    return {"service": "gh-broker", "version": app.version, "docs": "/docs"}


@app.get("/health")
async def health():
    apps = load_apps()
    return {
        "status": "healthy",
        "apps_configured": len(apps),
        "apps": [{"key": a["key"], "slug": a.get("slug"), "id": a.get("id"),
                  "allowed_scopes": a.get("allowed_scopes", ["*"])} for a in apps],
    }


@app.get("/apps")
async def list_apps(authorization: Optional[str] = Header(None)):
    """List registered Apps the caller can mint from. PEMs never returned."""
    identity = await verify_samcloud(authorization)
    return [
        {"key": a["key"], "slug": a.get("slug"), "id": a.get("id"),
         "allowed_scopes": a.get("allowed_scopes", ["*"]), "html_url": a.get("html_url")}
        for a in apps_for_scopes(identity.get("scopes", []))
    ]


# ---------- GitHub App manifest flow ---------------------------------------

DEFAULT_MANIFEST = {
    "name": "samcloud-agents",
    "url": PUBLIC_URL,
    "hook_attributes": {"url": f"{PUBLIC_URL}/webhook", "active": False},
    "redirect_url": f"{PUBLIC_URL}/manifest/callback",
    "public": False,
    "default_permissions": {
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "workflows": "write",
        "metadata": "read",
    },
    "default_events": [],
}


@app.get("/manifest/new", response_class=HTMLResponse)
async def manifest_new(
    owner: str = "",
    name: str = "samcloud-agents",
    key: str = "default",
    allowed_scopes: str = "*",
):
    """
    Self-submitting form that POSTs the App manifest to GitHub.
    `key` identifies this App in apps.json (must be unique).
    `allowed_scopes` is comma-separated samcloud scopes (e.g. 'group:work').
    """
    if get_app(key):
        raise HTTPException(409, f"App with key '{key}' already registered. Pick a different key or delete the existing entry.")

    state = secrets.token_urlsafe(24)
    STATE_FILE.write_text(json.dumps({
        "state": state,
        "key": key,
        "allowed_scopes": [s.strip() for s in allowed_scopes.split(",") if s.strip()],
        "ts": int(time.time()),
    }))
    os.chmod(STATE_FILE, 0o600)

    manifest = dict(DEFAULT_MANIFEST)
    manifest["name"] = name

    action = "https://github.com/settings/apps/new"
    if owner:
        action = f"https://github.com/organizations/{owner}/settings/apps/new"

    manifest_json = json.dumps(manifest).replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    return f"""<!doctype html>
<html><body>
<h1>Create GitHub App: {name}</h1>
<p>Will be registered in gh-broker as <code>key={key}</code> with allowed_scopes <code>{allowed_scopes}</code>.</p>
<form id="f" action="{action}?state={state}" method="post">
  <input type="hidden" name="manifest" value='{manifest_json}'>
  <button type="submit">Create App on GitHub</button>
</form>
</body></html>
"""


@app.get("/manifest/callback")
async def manifest_callback(code: str, state: Optional[str] = None):
    """GitHub redirects here after App creation; exchange code, append to apps.json."""
    if not STATE_FILE.exists():
        raise HTTPException(400, "no manifest flow in progress")
    saved = json.loads(STATE_FILE.read_text())
    if state and saved.get("state") != state:
        raise HTTPException(400, "state mismatch")

    key = saved.get("key", "default")
    allowed_scopes = saved.get("allowed_scopes") or ["*"]

    if get_app(key):
        raise HTTPException(409, f"App with key '{key}' was registered while flow was in progress")

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"https://api.github.com/app-manifests/{code}/conversions",
            headers={"Accept": "application/vnd.github+json"},
        )
    if r.status_code not in (200, 201):
        raise HTTPException(502, f"manifest conversion failed: {r.status_code} {r.text}")
    data = r.json()

    new_app = {
        "key": key,
        "id": data["id"],
        "slug": data["slug"],
        "name": data["name"],
        "pem": data["pem"],
        "client_id": data.get("client_id"),
        "client_secret": data.get("client_secret"),
        "webhook_secret": data.get("webhook_secret"),
        "html_url": data.get("html_url"),
        "allowed_scopes": allowed_scopes,
    }
    apps = load_apps()
    apps.append(new_app)
    save_apps(apps)
    STATE_FILE.unlink()

    install_url = f"{data.get('html_url', '')}/installations/new"
    return HTMLResponse(f"""<!doctype html>
<html><body>
<h1>App created: {data['slug']}</h1>
<p>Registered in gh-broker as <code>key={key}</code>, allowed_scopes <code>{allowed_scopes}</code>.</p>
<p>App ID: <code>{data['id']}</code></p>
<p>Next: <a href="{install_url}">Install the App</a> on the org/user with the repos you want agents to access.</p>
</body></html>
""")


@app.get("/install-url")
async def install_url(key: str = "default"):
    cfg = get_app(key)
    if not cfg:
        raise HTTPException(404, f"App '{key}' not configured")
    return {"key": key, "url": f"{cfg['html_url']}/installations/new"}


# ---------- Installations / token minting ----------------------------------

@app.get("/installations")
async def list_installations(authorization: Optional[str] = Header(None)):
    """List installations across all Apps the caller can mint from."""
    identity = await verify_samcloud(authorization)
    out = []
    async with httpx.AsyncClient(timeout=15) as c:
        for a in apps_for_scopes(identity.get("scopes", [])):
            r = await c.get(
                "https://api.github.com/app/installations",
                headers={"Authorization": f"Bearer {app_jwt(a)}", "Accept": "application/vnd.github+json"},
            )
            if r.status_code != 200:
                continue
            for i in r.json():
                out.append({
                    "app_key": a["key"],
                    "app_slug": a.get("slug"),
                    "id": i["id"],
                    "account": i["account"]["login"],
                    "account_type": i["account"]["type"],
                    "repository_selection": i["repository_selection"],
                    "permissions": i["permissions"],
                })
    return out


class TokenRequest(BaseModel):
    repos: list[str] = Field(default_factory=list, description="owner/repo names to scope the token to")
    permissions: Optional[dict | list[str]] = Field(
        default=None,
        description=(
            "Subset of the App's permissions to mint. Either a mapping "
            "({\"contents\": \"write\"}) or a list of verbs "
            "([\"contents:write\"], bare name means read). Omit and you get the "
            "installation's full grant."
        ),
    )
    installation_id: Optional[int] = Field(default=None, description="explicit installation; else inferred from repos")
    app: Optional[str] = Field(default=None, description="App key; else auto-pick first App that can serve the owner")


async def _resolve_app_and_installation(
    candidates: list[dict],
    repos: list[str],
    explicit_id: Optional[int],
    explicit_app_key: Optional[str],
) -> tuple[dict, int]:
    """Pick the App + installation_id given caller scope-filtered candidates."""
    if explicit_app_key:
        candidates = [a for a in candidates if a["key"] == explicit_app_key]
        if not candidates:
            raise HTTPException(403, f"App '{explicit_app_key}' not in your scope or not registered")

    if explicit_id and len(candidates) == 1:
        return candidates[0], explicit_id

    if not repos and not explicit_id:
        raise HTTPException(400, "must provide 'repos', or both 'app' and 'installation_id'")

    owners = {r.split("/", 1)[0] for r in repos}
    if len(owners) > 1:
        raise HTTPException(400, f"repos must share an owner (got {owners}); installation tokens are per-account")
    owner = owners.pop() if owners else None

    async with httpx.AsyncClient(timeout=10) as c:
        for a in candidates:
            jwt_tok = app_jwt(a)
            for path in (f"/orgs/{owner}/installation", f"/users/{owner}/installation"):
                r = await c.get(
                    f"https://api.github.com{path}",
                    headers={"Authorization": f"Bearer {jwt_tok}", "Accept": "application/vnd.github+json"},
                )
                if r.status_code == 200:
                    return a, r.json()["id"]

    raise HTTPException(404, f"no installation found for owner '{owner}' across Apps in your scope ({[a['key'] for a in candidates]})")


@app.post("/token")
async def mint_token(req: TokenRequest, authorization: Optional[str] = Header(None)):
    """Mint an installation token. Caller's samcloud scope decides which Apps are available."""
    identity = await verify_samcloud(authorization)
    candidates = apps_for_scopes(identity.get("scopes", []))
    if not candidates:
        raise HTTPException(403, "no Apps available for your samcloud scopes")

    repos, moved = resolve_repos(req.repos)
    chosen_app, inst_id = await _resolve_app_and_installation(
        candidates, repos, req.installation_id, req.app
    )

    requested = normalize_permissions(req.permissions)
    role = caller_role(identity, chosen_app)
    # An unqualified mint gets GH_BROKER_DEFAULT_PERMISSIONS if set, else the
    # installation's grant; either way it is narrowed, never refused.
    base = {} if requested else (DEFAULT_PERMISSIONS or await installation_grant(chosen_app, inst_id))
    effective, shaped_notes = shape_permissions(requested, base, role)

    # Always name the permissions: an unnamed ask gets the installation's full
    # grant from GitHub, and that would carry anything added to the App later.
    body: dict = {"permissions": effective}
    if repos:
        body["repositories"] = [r.split("/", 1)[1] for r in repos]

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"https://api.github.com/app/installations/{inst_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt(chosen_app)}", "Accept": "application/vnd.github+json"},
            json=body or None,
        )
    if r.status_code not in (200, 201):
        detail = f"GitHub rejected token request: {r.text}"
        if effective:
            # The usual cause is asking for something the installation itself does
            # not carry — an installation token can narrow the App's grant, never
            # widen it. Say so, and say where to look.
            detail += (
                f" — requested permissions {effective}. An installation token can only"
                " narrow what the App was installed with; GET /installations shows"
                " what this one carries."
            )
        raise HTTPException(r.status_code, detail)
    data = r.json()
    granted = data.get("permissions") or {}

    notes = list(shaped_notes)
    for old, new in moved.items():
        notes.append(f"{old} has moved to {new}; the token is for {new}. Old git URLs redirect,"
                     f" but update the remote: git remote set-url origin https://github.com/{new}.git")
    if not requested:
        notes.append(
            "No 'permissions' given, so this token carries the installation's grant ("
            + ", ".join(f"{k}:{v}" for k, v in sorted(granted.items()))
            + "). Ask for only what the task needs — {\"permissions\": [\"contents:write\"]}"
            " is enough for a push."
        )

    log_mint({
        # agent tokens carry owner_id and leave username/user_id null — without it
        # every agent mint logged as `null`, which is the one thing an audit log
        # must not do (#768)
        "identity": (identity.get("username") or identity.get("owner_id")
                     or identity.get("user_id")),
        "scopes": identity.get("scopes", []),
        "app_key": chosen_app["key"],
        "app_slug": chosen_app.get("slug"),
        "installation_id": inst_id,
        "repos": repos,
        **({"moved": moved} if moved else {}),
        "permissions": sorted(effective.keys()) if effective else [],
        "narrowed": bool(requested),
        "role": role,
        "granted": granted,
        "expires_at": data.get("expires_at"),
    })
    out = {
        "token": data["token"],
        "expires_at": data["expires_at"],
        "permissions": granted,
        "repository_selection": data.get("repository_selection"),
        "installation_id": inst_id,
        "app_key": chosen_app["key"],
        "app_slug": chosen_app.get("slug"),
        "role": role,
    }
    author = await git_author(chosen_app, caller_name(identity))
    if author:
        out["git_author"] = author
    if moved:
        out["moved"] = moved
    if notes:
        out["notes"] = notes
    return out


# ---------- Create a repo (#898) --------------------------------------------

REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class RepoCreate(BaseModel):
    org: str = Field(description="the org to create it in; an App the caller's scopes admit must be installed there")
    name: str = Field(description="repository name")
    description: str = Field(default="", description="one line")
    private: bool = Field(default=True, description="only private repos are created here; going public is the operator's step")
    auto_init: bool = Field(default=False, description="create a first commit with a README")


async def _installation_token(c, app_cfg: dict, inst_id: int, body: dict) -> dict:
    return await c.post(f"https://api.github.com/app/installations/{inst_id}/access_tokens",
                        headers={"Authorization": f"Bearer {app_jwt(app_cfg)}",
                                 "Accept": "application/vnd.github+json"},
                        json=body)


@app.post("/repos")
async def create_repo(req: RepoCreate, authorization: Optional[str] = Header(None)):
    """Create a private repo in an org, for an editor or owner of the group its App serves.

    The broker creates it with an administration token of its own, which never
    leaves this process, and answers a push token for the new repo (contents and
    pull requests, write) as /token would. Nobody deletes or transfers through here."""
    identity = await verify_samcloud(authorization)
    if not REPO_NAME.match(req.name) or req.name.lower().endswith(".git") or req.name in (".", ".."):
        raise HTTPException(400, f"not a repository name: {req.name!r}")
    if not req.private:
        raise HTTPException(403, "repos are created private here; making one public is the operator's step, "
                                 "behind the history scan for secrets")
    candidates = apps_for_scopes(identity.get("scopes", []))
    if not candidates:
        raise HTTPException(403, "no Apps available for your samcloud scopes")
    chosen_app, inst_id = await _resolve_app_and_installation(candidates, [f"{req.org}/{req.name}"], None, None)
    if not chosen_app.get("role_aware"):
        raise HTTPException(403, f"App '{chosen_app['key']}' does not create repos; only Apps marked "
                                 "role_aware in apps.json do, for the editors of the groups they serve")
    role = caller_role(identity, chosen_app)
    if ROLE_RANK[role] < ROLE_RANK["editor"]:
        raise HTTPException(403, f"creating a repo in {req.org} needs editor in a group App "
                                 f"'{chosen_app['key']}' serves ({chosen_app.get('allowed_scopes')}); you are {role}")

    async with httpx.AsyncClient(timeout=20) as c:
        r = await _installation_token(c, chosen_app, inst_id,
                                      {"permissions": {"administration": "write", "metadata": "read"}})
        if r.status_code not in (200, 201):
            raise HTTPException(409, f"App '{chosen_app['key']}' can't create repos yet: its installation on "
                                     f"{req.org} has no Administration permission ({r.status_code})")
        admin_token = r.json()["token"]
        r = await c.post(f"https://api.github.com/orgs/{req.org}/repos",
                         headers={"Authorization": f"token {admin_token}",
                                  "Accept": "application/vnd.github+json"},
                         json={"name": req.name, "description": req.description,
                               "private": True, "auto_init": req.auto_init})
        if r.status_code == 422:
            raise HTTPException(409, f"GitHub refused {req.org}/{req.name}: {r.text[:300]}")
        if r.status_code not in (200, 201):
            raise HTTPException(r.status_code, f"GitHub refused the create: {r.text[:300]}")
        repo = r.json()

        # The App sees a repo it just made only after a moment; try a few times.
        push = None
        for _ in range(5):
            t = await _installation_token(c, chosen_app, inst_id, {
                "repositories": [req.name],
                "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"}})
            if t.status_code in (200, 201):
                push = t.json()
                break
            await asyncio.sleep(2)

    log_mint({
        "action": "create_repo",
        "identity": caller_name(identity),
        "scopes": identity.get("scopes", []),
        "app_key": chosen_app["key"],
        "installation_id": inst_id,
        "repos": [repo["full_name"]],
        "role": role,
        "private": repo.get("private"),
    })
    out = {
        "repo": repo["full_name"],
        "html_url": repo.get("html_url"),
        "clone_url": repo.get("clone_url"),
        "private": repo.get("private"),
        "default_branch": repo.get("default_branch"),
        "app_key": chosen_app["key"],
        "role": role,
    }
    if push:
        out.update({"token": push["token"], "expires_at": push["expires_at"],
                    "permissions": push.get("permissions")})
        author = await git_author(chosen_app, caller_name(identity))
        if author:
            out["git_author"] = author
    else:
        out["notes"] = ["the repo exists, but a push token wasn't ready yet: POST /token for it in a moment"]
    return out


@app.get("/mints/recent")
async def recent_mints(authorization: Optional[str] = Header(None), limit: int = 50):
    await verify_samcloud(authorization)
    if not MINTS_LOG.exists():
        return []
    lines = MINTS_LOG.read_text().splitlines()[-limit:]
    return [json.loads(l) for l in lines]
