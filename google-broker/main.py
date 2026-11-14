"""
google-broker — Google OAuth token broker + Claude token distribution (doc #16).

Mints short-lived (1h), scope-narrowed Google access tokens for agents
authenticated via samcloud. The long-lived refresh token(s) never leave this
host. Multiple Google accounts can be registered (one per trust boundary,
e.g. one for an assistant); each has an `allowed_scopes` list checked against
the caller's samcloud scopes at mint time — same model as gh-broker.

Also holds Claude Code OAuth tokens as *distribution* slots (GET /claude-token):
central custody + revocation point + audit. Not minting — Anthropic offers no
token-exchange API — but tokens are fetched into process env at session start
instead of living on agent disks.

Consent flow is broker-hosted (web-application OAuth client with redirect
<GOOGLE_BROKER_PUBLIC_URL>/oauth/callback), so the one-time authorize works from
any browser including a phone — no localhost loopback needed.

Run: uvicorn main:app --host 0.0.0.0 --port 7783
"""

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field


# The plane this broker verifies tokens against (and, where it posts health, reports
# to). Required: the broker serves no instance by default.
SAMCLOUD_API = os.environ.get("SAMCLOUD_API", "").strip().rstrip("/")
if not SAMCLOUD_API:
    raise SystemExit("google-broker: set SAMCLOUD_API to the plane's API base, e.g. https://cloud.example.org/api/v1")
CONFIG_DIR = Path(os.environ.get("GOOGLE_BROKER_CONFIG", str(Path.home() / ".config/samcloud/google-broker")))
CLIENT_FILE = CONFIG_DIR / "client_secret.json"     # GCP OAuth web client (id + secret): client "default"
CLIENTS_DIR = CONFIG_DIR / "clients"                # further named clients, <name>.json (#1246)
ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"        # [{key, refresh_token, allowed_scopes, google_scopes, email?}]
CLAUDE_FILE = CONFIG_DIR / "claude_tokens.json"     # {key: {token, allowed_scopes, note?}}
LEGACY_CLAUDE = CONFIG_DIR / "claude_oauth_token"   # raw token file (migrated to key 'default')
STATE_FILE = CONFIG_DIR / "oauth-state.json"
MINTS_LOG = Path(os.environ.get("GOOGLE_BROKER_MINTS", str(Path.home() / ".local/samcloud/google-broker/mints.jsonl")))
PUBLIC_URL = os.environ.get("GOOGLE_BROKER_PUBLIC_URL", "").strip().rstrip("/")
if not PUBLIC_URL:
    raise SystemExit("google-broker: set GOOGLE_BROKER_PUBLIC_URL to this broker's public URL, e.g. https://gglb.example.org")

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"

# Short-name sugar for the scopes agents actually use.
SCOPE_ALIASES = {
    "calendar": "https://www.googleapis.com/auth/calendar",
    "calendar.readonly": "https://www.googleapis.com/auth/calendar.readonly",
    "calendar.events": "https://www.googleapis.com/auth/calendar.events",
    "gmail.readonly": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail.send": "https://www.googleapis.com/auth/gmail.send",
    "gmail.modify": "https://www.googleapis.com/auth/gmail.modify",
    "userinfo.email": "https://www.googleapis.com/auth/userinfo.email",
    # GA4: edit creates properties and data streams (Admin API), readonly runs
    # reports (Data API). Separate entries because a key enrolled for both mints
    # them separately -- setup asks for edit once, the weekly report asks for
    # readonly, and /mint down-scopes per call (#761).
    "analytics.edit": "https://www.googleapis.com/auth/analytics.edit",
    "analytics.readonly": "https://www.googleapis.com/auth/analytics.readonly",
}

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
MINTS_LOG.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="google-broker", version="0.2.0")


# ---------- stores ----------------------------------------------------------

def _read_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, 0o600)


CLIENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def client_path(name: str) -> Path:
    """Where a named OAuth client lives. One broker can hold clients from several
    Google Cloud projects (#1246: the family project and Sam's professional one);
    each enrolled account remembers which client it consented through."""
    if name == "default":
        return CLIENT_FILE
    if not CLIENT_NAME.match(name):
        raise HTTPException(400, "client names are lowercase letters, digits and dashes")
    return CLIENTS_DIR / f"{name}.json"


def client_names() -> list[str]:
    names = ["default"] if load_client() is not None else []
    if CLIENTS_DIR.is_dir():
        names += sorted(f.stem for f in CLIENTS_DIR.glob("*.json") if load_client(f.stem) is not None)
    return names


def load_client(name: str = "default") -> Optional[dict]:
    """Accepts either the raw GCP download ({'web': {...}} / {'installed': {...}}) or flat."""
    raw = _read_json(client_path(name), None)
    if raw is None:
        return None
    for k in ("web", "installed"):
        if k in raw:
            raw = raw[k]
            break
    if "client_id" not in raw or "client_secret" not in raw:
        return None
    return raw


def load_accounts() -> list[dict]:
    return _read_json(ACCOUNTS_FILE, [])


def load_claude_tokens() -> dict:
    tokens = _read_json(CLAUDE_FILE, {})
    # Migration: raw single-token file from before the broker existed.
    if not tokens and LEGACY_CLAUDE.exists():
        tokens = {"default": {
            "token": LEGACY_CLAUDE.read_text().strip(),
            "allowed_scopes": ["group:personal"],
            "note": "migrated from claude_oauth_token",
        }}
        _write_json(CLAUDE_FILE, tokens)
    return tokens


def resolve_scopes(requested: list[str]) -> list[str]:
    return [SCOPE_ALIASES.get(s, s) for s in requested]


def caller_allowed(entry_scopes: list[str], caller: dict) -> bool:
    allowed = entry_scopes or ["*"]
    if "*" in allowed:
        return True
    caller_set = set(caller.get("scopes") or [])
    if "*" in caller_set:                       # admin
        return True
    return bool(caller_set.intersection(allowed))


# ---------- samcloud auth / audit -------------------------------------------

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


# ---------- health -----------------------------------------------------------

@app.get("/")
async def root():
    return {"service": "google-broker", "version": "0.1.0", "docs": "/docs"}


@app.get("/health")
async def health():
    accounts = load_accounts()
    return {
        "status": "healthy",
        "client_configured": load_client() is not None,
        "clients": client_names(),
        "accounts": [{"key": a["key"], "email": a.get("email"),
                      "client": a.get("client", "default"),
                      "allowed_scopes": a.get("allowed_scopes", ["*"]),
                      "google_scopes": a.get("google_scopes", [])} for a in accounts],
        "claude_token_keys": list(load_claude_tokens().keys()),
    }


# ---------- OAuth client + consent flow (one-time setup, phone-friendly) ----

@app.post("/oauth/client")
async def upload_client(body: dict, name: str = "default",
                        authorization: Optional[str] = Header(None)):
    """Store a GCP OAuth client (web-application type) as client `name`. Admin only.

    The body is the client JSON, or `{"client_id": "...", "secret_from": "<key>"}`:
    the client secret was pasted by a person into a one-time `/secrets/link`
    under `<key>`, and moves from there into the client record, so it never
    passes through the admin's hands or a file (#1246)."""
    identity = await verify_samcloud(authorization)
    if "*" not in (identity.get("scopes") or []):
        raise HTTPException(403, "admin token required")
    path = client_path(name)
    secret_key = body.get("secret_from")
    if secret_key:
        store = _read_json(SECRETS_FILE, {})
        if secret_key not in store:
            raise HTTPException(404, f"no secret under key '{secret_key}'")
        body = {"client_id": body.get("client_id"), "client_secret": store[secret_key]["secret"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, body)
    if load_client(name) is None:
        path.unlink()
        raise HTTPException(400, "not a valid OAuth client JSON (need client_id + client_secret, type web)")
    if secret_key:
        store.pop(secret_key, None)
        _write_json(SECRETS_FILE, store)
    log_mint({"event": "client_stored", "client": name, "by": identity.get("username"),
              "from_secret": bool(secret_key)})
    return {"status": "stored", "client": name}


@app.get("/oauth/start")
async def oauth_start(key: str = "assistant",
                      scopes: str = "calendar,gmail.modify,userinfo.email",
                      allowed_scopes: str = "group:personal",
                      client: str = "default"):
    """
    Begin consent for a Google account → stored as account `key`.
    Open this URL in any browser logged into (or able to log into) the target
    Google account. `scopes` = Google scopes granted at consent (aliases ok);
    `allowed_scopes` = samcloud scopes allowed to mint from this account.
    `client` = which OAuth client (Google Cloud project) asks for consent.
    """
    client_name = client
    client = load_client(client_name)
    if client is None:
        raise HTTPException(503, f"no OAuth client '{client_name}' configured — POST /oauth/client first")
    google_scopes = resolve_scopes([s.strip() for s in scopes.split(",") if s.strip()])
    state = secrets.token_urlsafe(24)
    _write_json(STATE_FILE, {
        "state": state, "key": key, "client": client_name,
        "allowed_scopes": [s.strip() for s in allowed_scopes.split(",") if s.strip()],
        "google_scopes": google_scopes, "ts": int(time.time()),
    })
    params = httpx.QueryParams({
        "client_id": client["client_id"],
        "redirect_uri": f"{PUBLIC_URL}/oauth/callback",
        "response_type": "code",
        "scope": " ".join(google_scopes + ["https://www.googleapis.com/auth/userinfo.email"]),
        "access_type": "offline",       # -> refresh token
        "prompt": "consent",            # force refresh token even on re-auth
        "state": state,
    })
    return RedirectResponse(f"{GOOGLE_AUTH}?{params}")


@app.get("/oauth/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        raise HTTPException(400, f"consent denied: {error}")
    saved = _read_json(STATE_FILE, None)
    if not saved or saved.get("state") != state:
        raise HTTPException(400, "no consent flow in progress / state mismatch")
    if int(time.time()) - saved.get("ts", 0) > 900:
        raise HTTPException(400, "consent flow expired — restart /oauth/start")
    client_name = saved.get("client", "default")
    client = load_client(client_name)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(GOOGLE_TOKEN, data={
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "code": code, "grant_type": "authorization_code",
            "redirect_uri": f"{PUBLIC_URL}/oauth/callback",
        })
    if r.status_code != 200:
        raise HTTPException(502, f"code exchange failed: {r.text}")
    tok = r.json()
    if "refresh_token" not in tok:
        raise HTTPException(502, "no refresh_token in response (consent may have been a re-approval)")

    email = None
    async with httpx.AsyncClient(timeout=10) as c:
        u = await c.get("https://www.googleapis.com/oauth2/v2/userinfo",
                        headers={"Authorization": f"Bearer {tok['access_token']}"})
        if u.status_code == 200:
            email = u.json().get("email")

    accounts = [a for a in load_accounts() if a["key"] != saved["key"]]
    accounts.append({
        "key": saved["key"], "email": email,
        "refresh_token": tok["refresh_token"],
        "allowed_scopes": saved["allowed_scopes"],
        "google_scopes": saved["google_scopes"],
        **({} if client_name == "default" else {"client": client_name}),
    })
    _write_json(ACCOUNTS_FILE, accounts)
    STATE_FILE.unlink()
    log_mint({"event": "account_enrolled", "key": saved["key"], "email": email, "client": client_name,
              "google_scopes": saved["google_scopes"]})
    return HTMLResponse(f"""<!doctype html><html><body>
<h1>Account enrolled: {saved['key']}</h1>
<p><code>{email}</code> — agents with samcloud scopes <code>{saved['allowed_scopes']}</code>
can now mint 1h tokens scoped within <code>{saved['google_scopes']}</code>.</p>
<p>You can close this tab.</p></body></html>""")


# ---------- minting ----------------------------------------------------------

class MintRequest(BaseModel):
    scopes: list[str] = Field(default_factory=list, description="Google scopes (aliases ok); default = all granted")
    account: str = Field(default="assistant", description="account key in accounts.json")


@app.post("/mint")
async def mint(req: MintRequest, authorization: Optional[str] = Header(None)):
    """Mint a 1h Google access token, narrowed to the requested scopes."""
    identity = await verify_samcloud(authorization)
    acct = next((a for a in load_accounts() if a["key"] == req.account), None)
    if acct is None:
        raise HTTPException(404, f"no Google account '{req.account}' enrolled — see /oauth/start")
    if not caller_allowed(acct.get("allowed_scopes", []), identity):
        raise HTTPException(403, f"your samcloud scopes don't allow account '{req.account}'")

    granted = set(acct.get("google_scopes") or [])
    requested = resolve_scopes(req.scopes) if req.scopes else sorted(granted)
    over = set(requested) - granted
    if over:
        raise HTTPException(403, f"scopes not granted at consent: {sorted(over)}")

    client = load_client(acct.get("client", "default"))
    if client is None:
        raise HTTPException(503, f"account '{req.account}' needs OAuth client '{acct.get('client', 'default')}', which is not configured")
    data = {"client_id": client["client_id"], "client_secret": client["client_secret"],
            "refresh_token": acct["refresh_token"], "grant_type": "refresh_token"}
    if set(requested) != granted:
        data["scope"] = " ".join(requested)     # down-scope on refresh
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(GOOGLE_TOKEN, data=data)
    if r.status_code != 200:
        raise HTTPException(502, f"Google rejected refresh: {r.text}")
    tok = r.json()

    log_mint({"identity": identity.get("username") or identity.get("user_id"),
              "samcloud_scopes": identity.get("scopes", []),
              "account": req.account, "google_scopes": requested,
              "expires_in": tok.get("expires_in")})
    return {"access_token": tok["access_token"], "expires_in": tok.get("expires_in"),
            "scope": tok.get("scope"), "account": req.account, "email": acct.get("email")}


# ---------- Generic secret distribution slots ---------------------------------
# Same custody pattern as Claude tokens: long-lived secret lives only here,
# scope-gated + audited distribution, callers hold it in process env only.
# Example use: a per-account app-specific password (IMAP/SMTP) for an
# assistant; revocable at the provider.

SECRETS_FILE = CONFIG_DIR / "secrets.json"   # {key: {secret, allowed_scopes, note?}}


class SecretUpsert(BaseModel):
    key: str
    secret: str
    allowed_scopes: list[str] = Field(default_factory=lambda: ["group:personal"])
    note: Optional[str] = None


@app.post("/secrets")
async def upsert_secret(req: SecretUpsert, authorization: Optional[str] = Header(None)):
    """Store/replace a distribution secret. Admin only. Secret never echoed."""
    identity = await verify_samcloud(authorization)
    if "*" not in (identity.get("scopes") or []):
        raise HTTPException(403, "admin token required")
    secrets_store = _read_json(SECRETS_FILE, {})
    secrets_store[req.key] = {"secret": req.secret,
                              "allowed_scopes": req.allowed_scopes, "note": req.note}
    _write_json(SECRETS_FILE, secrets_store)
    log_mint({"event": "secret_stored", "key": req.key,
              "allowed_scopes": req.allowed_scopes,
              "by": identity.get("username")})
    return {"status": "stored", "key": req.key}


PENDING_SECRETS = CONFIG_DIR / "pending-secrets.json"   # {token: {key, allowed_scopes, note, exp}}


class SecretLinkRequest(BaseModel):
    key: str
    allowed_scopes: list[str] = Field(default_factory=lambda: ["group:personal"])
    note: Optional[str] = None
    ttl_min: int = 60


@app.post("/secrets/link")
async def create_secret_link(req: SecretLinkRequest, authorization: Optional[str] = Header(None)):
    """Mint a one-time browser submission link for a secret (connector-flow UX):
    admin prepares the slot, the human opens the URL anywhere, pastes, done.
    No bearer needed at the browser; the link burns on use / expiry."""
    identity = await verify_samcloud(authorization)
    if "*" not in (identity.get("scopes") or []):
        raise HTTPException(403, "admin token required")
    tok = secrets.token_urlsafe(24)
    pending = _read_json(PENDING_SECRETS, {})
    pending[tok] = {"key": req.key, "allowed_scopes": req.allowed_scopes,
                    "note": req.note, "exp": int(time.time()) + req.ttl_min * 60}
    _write_json(PENDING_SECRETS, pending)
    log_mint({"event": "secret_link_created", "key": req.key, "by": identity.get("username")})
    return {"url": f"{PUBLIC_URL}/secrets/submit/{tok}", "expires_min": req.ttl_min}


def _pending_valid(tok: str) -> Optional[dict]:
    pending = _read_json(PENDING_SECRETS, {})
    entry = pending.get(tok)
    if not entry or entry["exp"] < int(time.time()):
        return None
    return entry


@app.get("/secrets/submit/{tok}", response_class=HTMLResponse)
async def secret_form(tok: str):
    entry = _pending_valid(tok)
    if entry is None:
        raise HTTPException(404, "link expired or already used")
    note = entry.get("note") or ""
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>samcloud — store secret</title></head>
<body style="font-family:-apple-system,sans-serif;background:#18181b;color:#e4e4e7;display:flex;justify-content:center;padding-top:10vh">
<div style="max-width:420px;width:90%">
<h2 style="font-weight:600">Store secret: <code style="color:#fbbf24">{entry['key']}</code></h2>
<p style="color:#a1a1aa;font-size:14px">{note}</p>
<p style="color:#a1a1aa;font-size:13px">One-time link. The value goes straight into broker custody on the host (0600) — it is never shown again, and agents fetch it audited + scope-gated ({', '.join(entry['allowed_scopes'])}).</p>
<input id="s" type="password" placeholder="paste secret" autocomplete="off"
 style="width:100%;padding:10px;border-radius:8px;border:1px solid #3f3f46;background:#09090b;color:#e4e4e7;font-size:16px">
<button onclick="go()" style="margin-top:12px;width:100%;padding:10px;border-radius:8px;border:0;background:#e4e4e7;color:#18181b;font-weight:600;font-size:15px">Store</button>
<p id="m" style="font-size:14px"></p>
<script>
async function go() {{
  const v = document.getElementById('s').value.replace(/\\s+/g,'');
  if (!v) return;
  const r = await fetch(location.pathname, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{secret: v}})}});
  document.getElementById('m').textContent = r.ok ? '✓ stored — you can close this tab' : '✗ ' + (await r.text());
  if (r.ok) document.getElementById('s').value = '';
}}
</script></div></body></html>"""


class SecretSubmit(BaseModel):
    secret: str


@app.post("/secrets/submit/{tok}")
async def secret_submit(tok: str, req: SecretSubmit):
    entry = _pending_valid(tok)
    if entry is None:
        raise HTTPException(404, "link expired or already used")
    secrets_store = _read_json(SECRETS_FILE, {})
    secrets_store[entry["key"]] = {"secret": req.secret,
                                   "allowed_scopes": entry["allowed_scopes"],
                                   "note": entry.get("note")}
    _write_json(SECRETS_FILE, secrets_store)
    pending = _read_json(PENDING_SECRETS, {})
    pending.pop(tok, None)
    _write_json(PENDING_SECRETS, pending)
    log_mint({"event": "secret_stored_via_link", "key": entry["key"]})
    return {"status": "stored", "key": entry["key"]}


@app.get("/secret")
async def get_secret(key: str, authorization: Optional[str] = Header(None)):
    """Fetch a distribution secret (scope-gated, audited). Process env only —
    never write it to disk."""
    identity = await verify_samcloud(authorization)
    entry = _read_json(SECRETS_FILE, {}).get(key)
    if entry is None:
        raise HTTPException(404, f"no secret under key '{key}'")
    if not caller_allowed(entry.get("allowed_scopes", []), identity):
        raise HTTPException(403, f"your samcloud scopes don't allow secret '{key}'")
    log_mint({"identity": identity.get("username") or identity.get("user_id"),
              "samcloud_scopes": identity.get("scopes", []),
              "secret_key": key, "event": "secret_fetch"})
    return {"secret": entry["secret"], "key": key, "note": entry.get("note")}


# ---------- Claude token distribution ----------------------------------------

@app.get("/claude-token")
async def claude_token(key: str = "default", authorization: Optional[str] = Header(None)):
    """
    Fetch a Claude Code OAuth token (distribution slot, not minting).
    Callers should export it into process env only — never write it to disk.
    """
    identity = await verify_samcloud(authorization)
    entry = load_claude_tokens().get(key)
    if entry is None:
        raise HTTPException(404, f"no Claude token under key '{key}'")
    if not caller_allowed(entry.get("allowed_scopes", []), identity):
        raise HTTPException(403, f"your samcloud scopes don't allow Claude token '{key}'")
    log_mint({"identity": identity.get("username") or identity.get("user_id"),
              "samcloud_scopes": identity.get("scopes", []),
              "claude_token_key": key, "event": "claude_token_fetch"})
    return {"token": entry["token"], "key": key, "note": entry.get("note")}


@app.get("/mints/recent")
async def recent_mints(authorization: Optional[str] = Header(None), limit: int = 50):
    identity = await verify_samcloud(authorization)
    if "*" not in (identity.get("scopes") or []):
        raise HTTPException(403, "admin token required")
    if not MINTS_LOG.exists():
        return []
    return [json.loads(l) for l in MINTS_LOG.read_text().splitlines()[-limit:]]
