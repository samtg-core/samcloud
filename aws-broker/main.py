"""
aws-broker — AWS STS token broker, backed by IAM Roles Anywhere.

Mints short-lived, scope-narrowed AWS credentials for agents authenticated
via samcloud. The broker authenticates to AWS using an X.509 leaf cert
(issued by a self-managed root CA registered as a Roles Anywhere trust
anchor) — no long-lived access keys on disk.

Shape mirrors gh-broker: samcloud Bearer auth on every mint, JSONL audit log.

Run: uvicorn main:app --host 127.0.0.1 --port 7782
"""

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from iam_rolesanywhere_session import IAMRolesAnywhereSession


# The plane this broker verifies tokens against (and, where it posts health, reports
# to). Required: the broker serves no instance by default.
SAMCLOUD_API = os.environ.get("SAMCLOUD_API", "").strip().rstrip("/")
if not SAMCLOUD_API:
    raise SystemExit("aws-broker: set SAMCLOUD_API to the plane's API base, e.g. https://cloud.example.org/api/v1")
CONFIG_DIR = Path(os.environ.get("AWS_BROKER_CONFIG", str(Path.home() / ".config/samcloud/aws-broker")))
AWS_FILE = CONFIG_DIR / "aws.json"
SAMCLOUD_FILE = CONFIG_DIR / "samcloud.json"
MINTS_LOG = Path(os.environ.get("AWS_BROKER_MINTS", str(Path.home() / ".local/samcloud/aws-broker/mints.jsonl")))
HEALTH_INTERVAL = int(os.environ.get("AWS_BROKER_HEALTH_INTERVAL", "30"))

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
MINTS_LOG.parent.mkdir(parents=True, exist_ok=True)


async def _health_loop():
    """POST to samcloud /services/{id}/health every HEALTH_INTERVAL seconds."""
    if not SAMCLOUD_FILE.exists():
        return
    sc = json.loads(SAMCLOUD_FILE.read_text())
    service_id = sc.get("service_id")
    token = sc.get("service_token")
    if not (service_id and token):
        return
    url = f"{SAMCLOUD_API}/services/{service_id}/health"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                await client.post(url, headers=headers)
            except Exception:
                pass
            await asyncio.sleep(HEALTH_INTERVAL)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_health_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="aws-broker", version="0.2.0", lifespan=lifespan)


def load_aws() -> Optional[dict]:
    if not AWS_FILE.exists():
        return None
    return json.loads(AWS_FILE.read_text())


def save_aws(data: dict) -> None:
    AWS_FILE.write_text(json.dumps(data, indent=2))
    os.chmod(AWS_FILE, 0o600)


_broker_session_cache: dict = {}


def broker_session():
    """
    Roles-Anywhere session for the broker's own identity
    (the `<host>-aws-broker` role aws-setup.sh made). Cached until 60s before expiry.
    """
    cfg = load_aws()
    if not cfg:
        raise HTTPException(503, "AWS broker not configured — run aws-setup.sh")

    now = time.time()
    if _broker_session_cache.get("expires", 0) > now + 60:
        return _broker_session_cache["session"]

    sess = IAMRolesAnywhereSession(
        profile_arn=cfg["profile_arn"],
        trust_anchor_arn=cfg["trust_anchor_arn"],
        role_arn=cfg["broker_role_arn"],
        certificate=cfg["certificate_path"],
        private_key=cfg["private_key_path"],
        region=cfg.get("region", "us-east-1"),
        session_duration=cfg.get("broker_session_duration", 3600),
    ).get_session()
    _broker_session_cache["session"] = sess
    _broker_session_cache["expires"] = now + cfg.get("broker_session_duration", 3600)
    return sess


async def verify_samcloud(authorization: Optional[str]) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{SAMCLOUD_API}/auth/verify", headers={"Authorization": authorization})
    if r.status_code != 200:
        raise HTTPException(401, f"samcloud rejected token: {r.status_code}")
    return r.json()


def caller_identity(identity: dict) -> str:
    """Agent-type tokens carry username=None — the identity lives in owner_id.
    Without this fallback every agent mint logs as caller None / 'user-',
    which is exactly the unattributable audit trail #317 found (72 mints,
    zero attributable)."""
    return (identity.get("username") or identity.get("owner_id")
            or f"user-{identity.get('user_id') or 'unknown'}")


def is_admin(identity: dict) -> bool:
    return identity.get("role") == "admin" or "*" in (identity.get("scopes") or [])


def log_mint(entry: dict) -> None:
    entry["ts"] = int(time.time())
    with MINTS_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


@app.get("/")
async def root():
    return {"service": "aws-broker", "version": "0.1.0", "docs": "/docs"}


@app.get("/health")
async def health():
    cfg = load_aws()
    if not cfg:
        return JSONResponse({"status": "unconfigured"}, status_code=503)

    result = {
        "status": "healthy",
        "configured": True,
        "account": cfg.get("account"),
        "region": cfg.get("region"),
        "broker_role": cfg.get("broker_role_arn", "").split("/")[-1],
        "agent_roles": list(cfg.get("agent_roles", {}).keys()),
    }

    cert_path = Path(cfg["certificate_path"])
    if cert_path.exists():
        try:
            import subprocess
            out = subprocess.run(
                ["openssl", "x509", "-in", str(cert_path), "-noout", "-enddate"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                result["cert_expires"] = out.stdout.strip().replace("notAfter=", "")
        except Exception:
            pass
    else:
        result["status"] = "degraded"
        result["cert_missing"] = str(cert_path)

    return result


SCOPE_RE = __import__("re").compile(r"^[A-Za-z0-9_./@:+=-]{1,128}$")


class TokenRequest(BaseModel):
    role: str = Field(default="samcloud-agent", description="agent role key from aws.json")
    agent_scope: str = Field(..., description="declared purpose of this mint (free-form, audited; future per-identity gating)")
    session_policy: Optional[dict] = Field(default=None, description="inline IAM policy to narrow the session")
    duration: int = Field(default=3600, ge=900, le=43200, description="seconds, 15min–12h")
    session_name: Optional[str] = Field(default=None, description="defaults to samcloud identity")


@app.post("/token")
async def mint_token(req: TokenRequest, authorization: Optional[str] = Header(None)):
    """Mint short-lived AWS creds scoped via session policy. Requires samcloud Bearer auth."""
    identity = await verify_samcloud(authorization)
    cfg = load_aws()
    if not cfg:
        raise HTTPException(503, "AWS broker not configured")

    if not SCOPE_RE.match(req.agent_scope):
        raise HTTPException(422, "agent_scope must match [A-Za-z0-9_./@:+=-]{1,128}")

    role_arn = cfg.get("agent_roles", {}).get(req.role)
    if not role_arn:
        known = list(cfg.get("agent_roles", {}).keys())
        raise HTTPException(404, f"unknown role '{req.role}'. Known: {known}")

    # Privilege gate (#317): a valid samcloud bearer alone must not be enough
    # for the admin-grade role — the account behind it may be production.
    # Configurable per role via aws.json "role_gates":
    #   {"<role-key>": "admin" | "group:<name>" | "any"}
    # samcloud-agent defaults to "admin" when unlisted; other roles stay open.
    gate = cfg.get("role_gates", {}).get(
        req.role, "admin" if req.role == "samcloud-agent" else "any")
    scopes = identity.get("scopes") or []
    user_id = str(identity.get("user_id") or "")
    username = caller_identity(identity)
    # Per-identity grant (#298, first consumer: splat-service): aws.json
    #   "identity_grants": {"<identity>": {"<role>": {"session_policy": {...},
    #                                                 "max_duration": 3600}}}
    # lets a named non-admin identity through the role gate, but the session
    # policy is the BROKER's, applied unconditionally below — a grant holder
    # cannot mint the role wider than its grant, whatever it sends.
    grant = (cfg.get("identity_grants", {}).get(username) or {}).get(req.role)
    if not (gate == "any" or is_admin(identity)
            or (gate.startswith("group:") and gate in scopes)
            or grant):
        raise HTTPException(
            403, f"role '{req.role}' is gated to '{gate}' and your bearer does not "
                 f"satisfy it — file a ticket to your plane's admin if you need this")
    session_name = req.session_name or username
    session_name = session_name.replace("@", "-at-").replace(":", "-")[:64]

    # Session tags surface in CloudTrail as PrincipalTags on every event
    # the assumed session emits — that's how the trail gets demarcated.
    session_tags = [
        {"Key": "samcloud-user", "Value": username[:256]},
        {"Key": "samcloud-user-id", "Value": user_id[:256]},
        {"Key": "agent-scope", "Value": req.agent_scope},
    ]

    sts = broker_session().client("sts")
    kwargs = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name,
        "DurationSeconds": req.duration,
        "Tags": session_tags,
    }
    if grant and not is_admin(identity):
        kwargs["Policy"] = json.dumps(grant["session_policy"])
        kwargs["DurationSeconds"] = min(req.duration, grant.get("max_duration", 3600))
        policy_source = "grant"
    elif req.session_policy:
        kwargs["Policy"] = json.dumps(req.session_policy)
        policy_source = "caller"
    elif cfg.get("default_session_policy"):
        # Ceiling for mints that don't narrow themselves (#317): a session
        # policy can only intersect the role's policy, never widen it, so a
        # default costs nothing legitimate — it just stops "forgot to scope"
        # from meaning AdministratorAccess. Populated in aws.json during the
        # role scope-down.
        kwargs["Policy"] = json.dumps(cfg["default_session_policy"])
        policy_source = "default"
    else:
        policy_source = "none"

    try:
        resp = sts.assume_role(**kwargs)
    except Exception as e:
        raise HTTPException(502, f"STS AssumeRole failed: {e}")

    creds = resp["Credentials"]
    log_mint({
        "identity": username,
        "user_id": user_id,
        "token_type": identity.get("token_type"),
        "caller_role": identity.get("role"),
        "agent_scope": req.agent_scope,
        "role": req.role,
        "role_arn": role_arn,
        "session_name": session_name,
        "duration": req.duration,
        "session_policy": policy_source,
        "expires_at": creds["Expiration"].isoformat(),
    })

    return {
        "access_key_id": creds["AccessKeyId"],
        "secret_access_key": creds["SecretAccessKey"],
        "session_token": creds["SessionToken"],
        "expiration": creds["Expiration"].isoformat(),
        "role": req.role,
        "role_arn": role_arn,
        "session_name": session_name,
        "account": cfg.get("account"),
        "region": cfg.get("region"),
    }


@app.get("/mints/recent")
async def recent_mints(authorization: Optional[str] = Header(None), limit: int = 50):
    await verify_samcloud(authorization)
    if not MINTS_LOG.exists():
        return []
    lines = MINTS_LOG.read_text().splitlines()[-limit:]
    return [json.loads(l) for l in lines]
