"""
samcloud Registry API — central source of truth for devices, services, ports, and routes.

Run: uvicorn main:app --host 127.0.0.1 --port 7777
"""

import asyncio
import base64
import binascii
import errno
import time
import hashlib
import json
import math
import os
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import (FastAPI, File, Form, HTTPException, Header, Request, Response,
                     UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Optional
from pathlib import Path


def _service_domain() -> str:
    """The domain services are routed under when a request names none.

    SAMCLOUD_DOMAIN when set; otherwise PUBLIC_URL's host itself, so a service
    lands at <subdomain>.<host> (a.cloud.example.org, a.localhost). No parent
    domain is guessed: which part of a name an instance controls isn't knowable
    from the name, so an instance that routes under a parent says so.
    """
    explicit = os.environ.get("SAMCLOUD_DOMAIN", "").strip().strip(".")
    if explicit:
        return explicit
    from urllib.parse import urlparse
    return (urlparse(os.environ.get("PUBLIC_URL", "")).hostname or "localhost").rstrip(".")


# PLANE_HOST / PLANE_SSH_PORT / PLANE_SSH_USER were SLICE_*, from when the plane
# host was one machine's name (#1010). The old names are read when the new are unset.
def _plane_ssh_port() -> str:
    return os.environ.get("PLANE_SSH_PORT") or os.environ.get("SLICE_SSH_PORT") or "2222"


def _plane_ssh_user() -> str:
    return os.environ.get("PLANE_SSH_USER") or os.environ.get("SLICE_SSH_USER") or "samcloud"


def _tunnel_host() -> str:
    """Where devices dial their reverse tunnel: PLANE_HOST, else PUBLIC_URL's host."""
    from urllib.parse import urlparse
    return (os.environ.get("PLANE_HOST") or os.environ.get("SLICE_HOST")
            or urlparse(os.environ.get("PUBLIC_URL", "")).hostname or "localhost")


def _device_context(value: Optional[str]) -> Optional[str]:
    """A device's stored context label. `workcloud` was the early name for a
    device reached by reverse tunnel; it is accepted and stored as `tunnel`."""
    return "tunnel" if value == "workcloud" else value


def _modules() -> Optional[set]:
    """Optional modules switched on by SAMCLOUD_MODULES (comma-separated), or None when unset.

    Unset or empty, each module decides from its own config, which is how every
    instance behaved before the switch existed. `none` switches them all off.
    """
    raw = os.environ.get("SAMCLOUD_MODULES", "").strip()
    if not raw:
        return None
    return {m.strip() for m in raw.split(",") if m.strip() and m.strip() != "none"}


def _dns_module_on() -> bool:
    """dns-namecheap: named in SAMCLOUD_MODULES, or (unset) Namecheap credentials configured."""
    mods = _modules()
    if mods is not None:
        return "dns-namecheap" in mods
    if os.environ.get("NAMECHEAP_API_KEY") and os.environ.get("NAMECHEAP_API_USER"):
        return True
    return (Path.home() / ".config" / "samcloud" / "namecheap.env").exists()


def _dns_record(op: str, subdomain: str, domain: str) -> dict:
    """Add or remove a service's DNS record through the dns-namecheap module.

    With the module off the record is the instance's own to manage, and the answer
    says so rather than reporting a failure.
    """
    if caddy.by_port(domain):
        return {"success": None, "skipped": True,
                "message": "routes under an address are reached by port; there is no name to record"}
    if not _dns_module_on():
        return {"success": None, "skipped": True,
                "message": "dns-namecheap module is off; point the name at this host with your own DNS"}
    try:
        import dns as dns_mod
        fn = dns_mod.add_record if op == "add" else dns_mod.remove_record
        success, msg = fn(subdomain, domain)
        return {"success": success, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}


def _external_url(subdomain: str, domain: Optional[str] = None) -> str:
    """Build external URL for a service, respecting the environment's HTTPS port.

    Under a bare address there are no subdomains: the service gets its own port
    on the plane, plain HTTP (caddy.by_port).
    """
    domain = domain or _service_domain()
    if caddy.by_port(domain):
        return f"http://{domain}:{caddy.route_port(subdomain, domain)}"
    port = os.environ.get("PUBLIC_HTTPS_PORT", "443")
    if port == "443":
        return f"https://{subdomain}.{domain}"
    return f"https://{subdomain}.{domain}:{port}"

from fastapi import Depends

import re as _re

import db
from lease_rollover import RolloverRequest, rollover as rollover_residency
from lease_admission import reserve as reserve_lease
from lease_admission import (release as release_lease_rows, reap_and_promote as reap_leases,
                             queue_view as lease_queue_view, unsettled_view as lease_unsettled_view,
                             UNSETTLED as LEASE_UNSETTLED)
import lease_groups
import ports as port_alloc
import scope as groupscope   # scope v1 (doc 261) — aliased: many routes hold a local `scope` string


# --- Input Validation ---

VALID_SERVICE_STATUSES = {"online", "offline", "maintenance", "degraded",
                          "active", "pending", "stopped", "unknown"}

_NAME_RE = _re.compile(r'^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]{1,2}$')
_SUBDOMAIN_RE = _re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')
SSH_USER_RE = _re.compile(r'^[A-Za-z0-9._-]+$')  # OpenSSH valid_user character set


def _validate_service_name(name: str):
    if not name or not _NAME_RE.match(name):
        raise HTTPException(400,
            f"Invalid service name '{name}'. Must be 1-64 lowercase alphanumeric + hyphens, "
            "no leading/trailing hyphens, no special characters.")


def _verify_route(external_url: str, health_endpoint: str = None, timeout: int = 10) -> dict:
    """Verify a route is reachable end-to-end: DNS resolves + HTTPS responds.

    Returns {"reachable": bool, "status_code": int|None, "error": str|None}
    """
    import urllib.request
    import urllib.error
    import ssl

    if not external_url:
        return {"reachable": False, "error": "no external URL"}

    # Test the health endpoint if provided, otherwise root
    test_url = external_url
    if health_endpoint:
        test_url = external_url.rstrip("/") + health_endpoint

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # staging certs may be self-signed initially
        req = urllib.request.Request(test_url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return {"reachable": True, "status_code": resp.status, "error": None}
    except urllib.error.HTTPError as e:
        # Got a response (even 4xx/5xx means route works)
        return {"reachable": True, "status_code": e.code, "error": None}
    except urllib.error.URLError as e:
        return {"reachable": False, "status_code": None, "error": str(e.reason)}
    except Exception as e:
        return {"reachable": False, "status_code": None, "error": str(e)}


def _validate_subdomain(subdomain: str):
    if not subdomain or not _SUBDOMAIN_RE.match(subdomain):
        raise HTTPException(400,
            f"Invalid subdomain '{subdomain}'. Must be 1-63 lowercase alphanumeric + hyphens.")


def _validate_port(port: int):
    if port < 1 or port > 65535:
        raise HTTPException(400, f"Invalid port {port}. Must be 1-65535.")
import caddy
import enrollment
import authorized_keys
import forward
from events import bus as event_bus


# --- Auth Dependencies ---

class AuthContext:
    """Represents the authenticated caller."""
    def __init__(self, token_type: str, owner_id: str, scopes: list[str]):
        self.token_type = token_type  # 'device', 'service', 'agent', 'admin'
        self.owner_id = owner_id
        self.scopes = scopes

    def has_scope(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes

    def is_admin(self) -> bool:
        return self.has_scope("*") or self.has_scope("admin")

    def is_agent(self) -> bool:
        return self.token_type == "agent"

    def owns(self, resource_id: str) -> bool:
        """Check if this caller owns the resource (device or service).

        For service tokens: exact match or prefix match (device_id/service_name).
        For user/agent tokens: checks created_by on the service record.
        """
        # Direct ownership (service tokens, device tokens)
        if (self.owner_id == resource_id
                or resource_id.startswith(f"{self.owner_id}/")):
            return True

        # User/agent ownership — check created_by on the service
        if self.token_type in ("agent", "device"):
            svc = db.query_one("SELECT created_by FROM services WHERE id = ?", (resource_id,))
            if svc:
                # NULL created_by = legacy service, only admin can write
                if svc.get("created_by") is None:
                    return False
                if svc["created_by"] == self.owner_id:
                    return True

        return False

    def can_read(self, resource_id: str) -> bool:
        """Check if this caller can read a resource (broader than owns — includes device/group scope)."""
        if self.is_admin() or self.owns(resource_id):
            return True

        for scope in self.scopes:
            if scope.startswith("device:"):
                device_id = scope.split(":", 1)[1]
                if resource_id.startswith(f"{device_id}/") or resource_id == device_id:
                    return True
            elif scope.startswith("group:"):
                group_id = scope.split(":", 1)[1]
                in_group = db.query_one(
                    "SELECT 1 FROM group_services WHERE group_id = ? AND service_id = ?",
                    (group_id, resource_id),
                )
                if in_group:
                    return True
            elif scope.startswith("services:"):
                allowed = scope.split(":", 1)[1].split(",")
                if resource_id in allowed:
                    return True

        return False

    def can_manage_device(self, device_id: str) -> bool:
        """Check if this caller can create/manage services on a device."""
        if self.is_admin():
            return True
        for scope in self.scopes:
            if scope == f"device:{device_id}":
                return True
            if scope.startswith("group:"):
                group_id = scope.split(":", 1)[1]
                in_group = db.query_one(
                    "SELECT 1 FROM group_devices WHERE group_id = ? AND device_id = ?",
                    (group_id, device_id),
                )
                if in_group and groupscope.can_edit(self, group_id):
                    return True
        return False

    def can_create_device(self) -> bool:
        """Check if this caller can create new devices (admin or group editor)."""
        if self.is_admin():
            return True
        return any(groupscope.can_edit(self, g) for g in self.get_group_scopes())

    def get_group_scopes(self) -> list[str]:
        """Get group IDs from this caller's scopes."""
        return [s.split(":", 1)[1] for s in self.scopes if s.startswith("group:")]

    def identity(self) -> str:
        """Canonical caller identity for audit/source/author fields.

        For user/agent tokens: looks up and returns the username.
        For service/device tokens: returns owner_id directly (already device/service path).
        Falls back to owner_id if the user row is missing for any reason.

        Used by POST /tickets and POST /comments to derive source/author
        from the bearer rather than trusting body-supplied identity.
        """
        if self.owner_id.startswith("user:"):
            row = db.query_one("SELECT username FROM users WHERE id = ?",
                               (int(self.owner_id.split(":")[1]),))
            if row and row.get("username"):
                return row["username"]
        return self.owner_id


def _device_ids_for_scopes(scopes: list[str]):
    """The device ids a scope list can see, or None when it carries `*`/`admin`
    (sees all). One computation for a bearer (`_visible_device_ids`) and for a
    username the gateway asks about (`check_device_access`, `accessible_devices`)
    — before scope v1 train 0 the gateway read the token's scope string alone,
    so a member added through the group had the API and not the door (doc 262)."""
    if "*" in scopes or "admin" in scopes:
        return None
    ids = set()
    for scope in scopes:
        if scope.startswith("device:"):
            ids.add(scope.split(":", 1)[1])
        elif scope.startswith("group:"):
            group_id = scope.split(":", 1)[1]
            for row in db.query("SELECT device_id FROM group_devices WHERE group_id = ?", (group_id,)):
                ids.add(row["device_id"])
    return ids


def _visible_device_ids(auth: AuthContext):
    """Returns set of device IDs visible to caller, or None for admin (sees all)."""
    if auth.is_admin():
        return None
    return _device_ids_for_scopes(auth.scopes)


def _effective_user_scopes(user: dict) -> list[str]:
    """A user row's scopes as the API applies them: the stored scope string,
    `admin` for the admin role, and `group:<g>` for every membership row —
    the same union `get_auth` builds for a bearer."""
    try:
        scopes = json.loads(user.get("scopes") or "[]")
    except (TypeError, ValueError):
        scopes = []
    if isinstance(scopes, dict):
        scopes = list(scopes.keys())
    scopes = [s for s in scopes if isinstance(s, str)]
    if user.get("role") == "admin" and "admin" not in scopes:
        scopes.append("admin")
    for row in db.query("SELECT group_id FROM group_members WHERE user_id = ?", (user["id"],)):
        s = f"group:{row['group_id']}"
        if s not in scopes:
            scopes.append(s)
    return scopes


def _validate_device_identity(device_id: str):
    # Device IDs also appear as token owners and service-path prefixes. They
    # must not be interpreted as a user:<id> principal or as a service path.
    if not device_id or ":" in device_id or "/" in device_id:
        raise HTTPException(422, "Device IDs must be nonempty and contain neither ':' nor '/'")
    if db.query_one("SELECT 1 FROM users WHERE username = ?", (device_id,)):
        raise HTTPException(409, "Device ID conflicts with a username; choose a distinct device ID to enroll or re-enroll")


def _validate_user_identity(username: str):
    # Also prevent the opposite creation order (device first, then user).
    if db.query_one("SELECT 1 FROM devices WHERE id = ?", (username,)):
        raise HTTPException(409, "Username conflicts with a device ID; choose a distinct username")


def _require_visible_device(auth: AuthContext, device_id: str) -> dict:
    """The device row, or 404 — a device outside the caller's scope is hidden,
    not refused (doc 261, decision 4: it does not exist from the outside)."""
    device = db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,))
    visible = _visible_device_ids(auth)
    if not device or (visible is not None and device_id not in visible):
        raise HTTPException(404, f"Device {device_id} not found")
    return device


def _require_managed_device(auth: AuthContext, device_id: str) -> dict:
    """The device row for a write: hidden (404) outside scope, 403 for a
    reader who is not its manager."""
    device = _require_visible_device(auth, device_id)
    if not auth.can_manage_device(device_id):
        raise HTTPException(403, f"You can read {device_id} but not manage it")
    return device


def _require_visible_service(auth: AuthContext, service_id: str) -> dict:
    """The service row, or 404 — visible when its device is, or when the
    caller can read the service itself (owner, `services:` scope)."""
    svc = db.query_one("SELECT * FROM services WHERE id = ?", (service_id,))
    if not svc:
        raise HTTPException(404, f"Service {service_id} not found")
    visible = _visible_device_ids(auth)
    if visible is not None and svc.get("device_id") not in visible and not auth.can_read(service_id):
        raise HTTPException(404, f"Service {service_id} not found")
    return svc


def _require_service_manager(auth: AuthContext, service_id: str) -> dict:
    """The service row for a write: hidden outside scope (404); 403 unless the
    caller is the admin, owns the service (its own token, or created it), or
    manages its device (an editor of a group holding the device — doc 261:
    an editor changes a service's status and properties)."""
    svc = _require_visible_service(auth, service_id)
    if auth.is_admin() or auth.owns(service_id) or auth.can_manage_device(svc.get("device_id") or ""):
        return svc
    raise HTTPException(403, f"You can read {service_id} but not change it")


def _require_visible_resource(auth: AuthContext, resource_id: str, manage: bool = False) -> dict:
    """The resource row, hidden (404) when its device is outside the caller's
    scope; with `manage`, 403 for a reader who is not the device's manager.
    Scope v1 train 0 — resource writes checked nothing beyond a bearer."""
    res = db.query_one("SELECT * FROM resources WHERE id = ?", (resource_id,))
    if not res:
        raise HTTPException(404, f"Resource {resource_id} not found")
    visible = _visible_device_ids(auth)
    if visible is not None and res.get("device_id") not in visible:
        raise HTTPException(404, f"Resource {resource_id} not found")
    if manage and not auth.can_manage_device(res.get("device_id") or ""):
        raise HTTPException(403, f"You can read {resource_id} but not manage its device")
    return res


def _visible_resource_ids(auth: Optional[AuthContext]):
    """Set of resource IDs visible to caller, or None for admin (sees all).

    A resource is visible iff its parent device is. An unauthenticated caller
    (auth is None) collapses to the empty set — fail closed, like /status.
    """
    visible_devices = _visible_device_ids(auth) if auth is not None else set()
    if visible_devices is None:
        return None  # admin
    return {r["id"] for r in db.query("SELECT id, device_id FROM resources")
            if r["device_id"] in visible_devices}


def _get_group_role(auth: AuthContext, group_id: str) -> str:
    """Get the caller's role in a group. Returns 'owner', 'editor', 'viewer', or None."""
    if auth.is_admin():
        return "owner"
    if not auth.owner_id.startswith("user:"):
        return None
    user_id = int(auth.owner_id.split(":")[1])
    row = db.query_one(
        "SELECT role FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )
    return row["role"] if row else None


ROLE_HIERARCHY = groupscope.ROLE_RANK


def get_auth(authorization: Optional[str] = Header(None)) -> AuthContext:
    """Extract and validate auth token. Returns AuthContext or raises 401."""
    if not authorization:
        raise HTTPException(401, "Missing Authorization header. Include: Authorization: Bearer <token>. Get a token via POST /api/v1/auth/login with username and password.")

    token_str = enrollment.extract_bearer_token(authorization)
    if not token_str:
        raise HTTPException(401, "Invalid Authorization format (expected: Bearer <token>)")

    token_record = enrollment.verify_token(token_str)
    if not token_record:
        raise HTTPException(401, "Invalid or expired token. Login again: POST /api/v1/auth/login with {\"username\":\"...\",\"password\":\"...\"} to get a new token.")

    scopes = json.loads(token_record.get("scopes", "[]"))
    if isinstance(scopes, dict):
        scopes = list(scopes.keys())

    # Expand scopes with group memberships from group_members table.
    # This ensures users added to groups after token creation still get access.
    owner_id = token_record["owner_id"]
    if (owner_id.startswith("user:") and not token_record["id"].startswith("session:")
            and token_record["type"] != "agent" and db.query_one(
            "SELECT 1 FROM devices WHERE id = ?", (owner_id.split("/", 1)[0],))):
        # Old device IDs may occupy the reserved user namespace. Their token
        # rows are indistinguishable from human tokens: fail closed until the
        # conflicting device is removed/renamed. Password-backed sessions and
        # agent:user:<id> tokens have distinct token IDs and stay unambiguous.
        raise HTTPException(401, "Ambiguous token owner; an administrator must resolve the conflicting device ID")
    if (token_record["type"] == "device" and ":" not in owner_id and "/" not in owner_id
            and db.query_one("SELECT 1 FROM users WHERE username = ?", (owner_id,))):
        # Besides group membership, a bare identity addresses inbox delivery.
        # Never let an old device key impersonate that human/agent username.
        raise HTTPException(401, "Device token owner conflicts with a username")
    if owner_id.startswith("user:"):
        user_id = groupscope._user_id(owner_id)
        if user_id is None:
            raise HTTPException(401, "Invalid user token owner")
        group_rows = db.query(
            "SELECT group_id FROM group_members WHERE user_id = ?", (user_id,)
        )
        existing = set(scopes)
        for row in group_rows:
            scope = f"group:{row['group_id']}"
            if scope not in existing:
                scopes.append(scope)
                existing.add(scope)

    return AuthContext(
        token_type=token_record["type"],
        owner_id=owner_id,
        scopes=scopes,
    )


def get_auth_optional(authorization: Optional[str] = Header(None)) -> Optional[AuthContext]:
    """Like get_auth but returns None if no token (for public-with-optional-auth endpoints)."""
    if not authorization:
        return None
    try:
        return get_auth(authorization)
    except HTTPException:
        return None


def require_admin(auth: AuthContext = Depends(get_auth)) -> AuthContext:
    """Require admin scope."""
    if not auth.is_admin():
        raise HTTPException(403, "Admin access required")
    return auth


def require_owner_or_admin(resource_id: str, auth: AuthContext = Depends(get_auth)) -> AuthContext:
    """Require caller to own the resource or be admin."""
    if not auth.is_admin() and not auth.owns(resource_id):
        raise HTTPException(403, f"Access denied to {resource_id}")
    return auth


# --- Lifespan ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Auto-generate gateway keypair on first boot
    gw_key = Path(os.environ.get("GATEWAY_KEY", "/var/lib/samcloud/gateway"))
    if not gw_key.exists():
        gw_key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(gw_key), "-N", "", "-C", "samcloud-gateway"],
            capture_output=True, check=True,
        )
        os.chmod(str(gw_key), 0o600)
        print(f"[registry] Gateway keypair auto-generated at {gw_key}")
    # Auto-generate gateway token for authorized_keys ForceCommand
    gw_token_path = Path(os.environ.get("GATEWAY_TOKEN_PATH",
        str(Path.home() / ".local" / "samcloud" / "staging" / "keys" / "gateway_token")))
    if not gw_token_path.exists():
        gw_token_path.parent.mkdir(parents=True, exist_ok=True)
        token = f"sc_device_{secrets.token_hex(16)}"
        # Store as a real token in DB so the gateway can authenticate
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        db.execute(
            "INSERT OR IGNORE INTO tokens (token_hash, type, owner_id, scopes) VALUES (?, 'device', 'system:gateway', ?)",
            (token_hash, '["*"]'),
        )
        gw_token_path.write_text(token)
        os.chmod(str(gw_token_path), 0o600)
        print(f"[registry] Gateway token auto-generated at {gw_token_path}")
    # Write authorized_keys at every start. sshd mounts it from this volume by
    # subpath (#901), which needs the file to exist on a fresh volume (a test
    # stack, 2026-10-11). And an existing file must not keep lines in an older
    # shape until the next access key or activation happens to call sync(): sshd
    # waits for this release's keys-policy stamp before it starts (#136).
    authorized_keys.sync()
    print(f"[registry] authorized_keys written at {authorized_keys.AUTHORIZED_KEYS_PATH} "
          f"(keys-policy={authorized_keys.KEYS_POLICY})")

    # Reconcile port forwards in the background, on a periodic loop — nested
    # ssh -fN processes inside sshd die when its container rebuilds, and remote
    # reverse tunnels reconnect on their own schedule (often minutes after
    # startup), so a one-shot pass at boot leaves every bridge down until the
    # next rebuild. Done off the lifespan path so we don't block /health, which
    # sshd's depends_on waits for (would deadlock against sshd's own readiness).
    async def _reconcile_forwards_bg():
        sshd_host = os.environ.get("FORWARD_SSH_HOST", "localhost")
        # Wait up to 5 minutes for sshd to become reachable.
        for _ in range(150):
            try:
                socket.gethostbyname(sshd_host)
                # Readiness probe for the same container forward.py talks to, so it
                # verifies against the same anchor — the sshd-keys volume (#822).
                probe = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                     *forward.probe_hostkey_args(),
                     "-o", "IdentitiesOnly=yes",
                     "-i", os.environ.get("FORWARD_SSH_KEY", "/data/keys/gateway"),
                     f"{os.environ.get('FORWARD_SSH_USER', 'samcloud')}@{sshd_host}",
                     "true"],
                    capture_output=True, timeout=5,
                )
                if probe.returncode == 0:
                    break
            except (socket.gaierror, subprocess.TimeoutExpired):
                pass
            await asyncio.sleep(2)
        else:
            print("[registry] sshd never became reachable; skipping forward reconcile")
            return

        # Periodic reconcile loop. reconcile_all() skips forwards already
        # listening, so steady state is cheap (down-tunnel services fail fast
        # with connection-refused, not a timeout). Stay quiet unless the
        # started/failed set changes, so a permanently-offline edge doesn't
        # spam the log every cycle.
        interval = int(os.environ.get("FORWARD_RECONCILE_INTERVAL", "120"))
        last_sig = None
        first = True
        while True:
            try:
                rows = db.query("""
                    SELECT s.id AS service_id, s.port AS service_port, s.slice_port,
                           d.tunnel_port, d.outbound_ssh_user AS device_ssh_user
                    FROM services s
                    JOIN devices d ON d.id = s.device_id
                    WHERE s.slice_port IS NOT NULL
                      AND d.tunnel_port IS NOT NULL
                """)
                if rows:
                    summary = forward.reconcile_all([dict(r) for r in rows])
                    sig = (
                        tuple(sorted(x["service_id"] for x in summary["started"])),
                        tuple(sorted(x["service_id"] for x in summary["failed"])),
                    )
                    if first or sig != last_sig:
                        print(f"[registry] Forward reconcile: "
                              f"started={len(summary['started'])} "
                              f"skipped={len(summary['skipped'])} "
                              f"failed={len(summary['failed'])}")
                        for x in summary["started"]:
                            print(f"[registry]   UP {x['service_id']}: {x['msg']}")
                        for x in summary["failed"]:
                            print(f"[registry]   FAILED {x['service_id']}: {x['msg']}")
                        last_sig = sig
                        first = False
            except Exception as e:
                print(f"[registry] Forward reconcile error: {e}")
            await asyncio.sleep(interval)

    asyncio.create_task(_reconcile_forwards_bg())

    # #503: dark is computed truth, but it was pull-only — during the 08-16
    # fleet 401 window the flag was true for hours and nobody read the
    # endpoint. This loop makes it push: evaluate every few minutes, diff
    # against wake_dark_state, and notify the admin identities on transitions
    # through the same inbox that carries everything else.
    async def _dark_watch_bg():
        interval = int(os.environ.get("DARK_WATCH_INTERVAL", "300"))
        await asyncio.sleep(60)   # let the fleet reconnect after a deploy first
        while True:
            try:
                _evaluate_dark_transitions()
            except Exception as e:
                print(f"[registry] dark-watch error (non-fatal): {e}")
            await asyncio.sleep(interval)
    asyncio.create_task(_dark_watch_bg())

    # Task scheduler (doc 161 §5, task 596): fires due schedules into
    # assignee inboxes. The plane is the durable timer — survives seat
    # reboots because the registry does; OS schedulers stay for service
    # delivery only, never agent pokes.
    async def _task_scheduler_bg():
        interval = int(os.environ.get("TASK_SCHEDULER_INTERVAL", "30"))
        await asyncio.sleep(15)
        while True:
            try:
                n = _run_task_scheduler_pass()
                if n:
                    print(f"[registry] task scheduler fired {n} schedule(s)")
            except Exception as e:
                print(f"[registry] task scheduler error (non-fatal): {e}")
            await asyncio.sleep(interval)
    asyncio.create_task(_task_scheduler_bg())

    yield


app = FastAPI(title="samcloud Registry", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def _stamp_auth_refusals(request, call_next):
    """#503 shape (c): a locked-out listener retries the SSE stream every ~30s
    and every retry 401s HERE — the registry is the one party that always
    witnesses the outage. Attribute refused stream connects by token-hash
    resolution (expired/rotated tokens still name their owner) and stamp
    wake_auth_refusals, which the dark logic and the dark-watch loop consume.
    Unresolvable tokens count under '_unattributed' so a storm is still
    visible."""
    response = await call_next(request)
    try:
        if (response.status_code == 401
                and request.url.path == "/api/v1/events"):
            authz = request.headers.get("authorization", "")
            ident = "_unattributed"
            if authz.lower().startswith("bearer "):
                th = hashlib.sha256(authz[7:].strip().encode()).hexdigest()
                row = db.query_one(
                    "SELECT owner_id FROM tokens WHERE token_hash = ?", (th,))
                if row and row.get("owner_id"):
                    ident = row["owner_id"]
            db.execute(
                "INSERT INTO wake_auth_refusals (identity, count) VALUES (?, 1) "
                "ON CONFLICT(identity) DO UPDATE SET "
                "count = count + 1, last_at = datetime('now')", (ident,))
    except Exception:
        pass    # observation must never break the response path
    return response
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Request Models ---

class StrictModel(BaseModel):
    """Base for all POST/PATCH request models.

    `extra="forbid"` means unknown fields → 422 instead of silently dropped.
    Closes prod #47 (silent assignee drop class of bug). Models that need
    to accept alias fields (e.g. TicketCreate accepting `description` as a
    `body` alias for legacy clients) do so via `model_validator(mode="before")`
    which runs BEFORE the strict-field check.
    """
    model_config = ConfigDict(extra="forbid")


class DeviceRegister(StrictModel):
    id: str
    name: Optional[str] = None          # display name (defaults to id)
    type: str = "device"                # freeform: device, server, phone, bot
    owner_id: Optional[int] = None      # user who owns this device

    # Inbound: how this device reaches the plane host
    inbound_method: Optional[str] = None   # ssh-tunnel, lan, browser-only
    inbound_endpoint: Optional[str] = None # e.g. tunnel.example.org:2222

    # Outbound: how the plane host reaches this device
    outbound_method: Optional[str] = None  # direct-ssh, reverse-tunnel, http
    outbound_ip: Optional[str] = None      # LAN IP for direct access
    outbound_ssh_user: Optional[str] = None
    outbound_ssh_port: int = 22
    tunnel_port: Optional[int] = None      # auto-allocated if reverse-tunnel
    lifeline_port: Optional[int] = None
    transport_owner: Optional[str] = None  # borrow this device's reverse tunnel instead of
                                           # allocating one — a secondary user on an already-
                                           # enrolled machine (differentiated by outbound_ssh_user
                                           # at gateway auth). See ticket #100.

    startup_method: Optional[str] = None
    recovery_method: Optional[str] = None
    monitor_port: Optional[int] = None

    # Compat aliases (old schema)
    hostname: Optional[str] = None      # deprecated, use name
    context: Optional[str] = None       # deprecated, inferred from outbound_method
    ip: Optional[str] = None            # deprecated, use outbound_ip
    ssh_user: Optional[str] = None      # deprecated, use outbound_ssh_user


class ServiceRegister(StrictModel):
    name: str
    device_id: str
    port: int
    description: str                         # required: what this service does
    slice_port: Optional[int] = None
    protocol: str = "http"
    health_endpoint: Optional[str] = None
    docs_endpoint: Optional[str] = None      # e.g. '/service-docs'
    subdomain: Optional[str] = None
    domain: str = Field(default_factory=_service_domain)
    version: Optional[str] = None
    capabilities: Optional[list[str]] = None


class PortAllocate(StrictModel):
    port_type: str
    owner_id: str
    preferred: Optional[int] = None


class RouteCreate(StrictModel):
    subdomain: str
    domain: str = Field(default_factory=_service_domain)
    target: str
    service_id: Optional[str] = None


# Canonical priorities (a review of the v2 skills): the field
# was lenient, so "med"/"hi" variants accumulated as unfilterable stored values.
# Normalize the known aliases, reject the unknown — loudly, not silently.
_PRIORITY_ALIASES = {"med": "medium", "mid": "medium", "normal": "medium",
                     "hi": "high", "urgent": "high", "critical": "high",
                     "lo": "low", "minor": "low"}
_PRIORITIES = ("low", "medium", "high")


def _canon_priority(v):
    if v is None:
        return v
    p = str(v).strip().lower()
    p = _PRIORITY_ALIASES.get(p, p)
    if p not in _PRIORITIES:
        raise ValueError(f"priority must be one of {'/'.join(_PRIORITIES)} (got {v!r})")
    return p


# ── task scheduling (doc 161 §5, task 596) ──────────────────────────────────
# The timer half of #303: AgentWake/schedule.fired delivers, this arms. A
# scheduled task fires into the assignee's inbox at scheduled_at; recurrence
# re-arms it on the operator's calendar (a "daily 07:00" stays 07:00 local
# time across DST, which is why stepping happens in operator tz).

_RECURRENCES = ("daily", "weekdays", "weekly", "monthly")


def _canon_schedule(scheduled_at, recurrence):
    """Normalize (scheduled_at -> UTC ISO, recurrence -> vocab word).
    Naive datetimes are read in the operator's timezone — the operator schedules in
    their own clock, never the host's (#384)."""
    if recurrence is not None:
        r = str(recurrence).strip().lower()
        recurrence = None if r in ("", "none") else r
        if recurrence is not None and recurrence not in _RECURRENCES:
            raise ValueError(
                f"recurrence must be one of {'/'.join(_RECURRENCES)} or none (got {r!r})")
    if scheduled_at is not None and scheduled_at != "":
        try:
            dt = datetime.fromisoformat(str(scheduled_at).strip())
        except ValueError:
            raise ValueError(f"scheduled_at must be ISO datetime (got {scheduled_at!r})")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(OPERATOR_TZ))
        scheduled_at = dt.astimezone(timezone.utc).isoformat()
    return scheduled_at, recurrence


def _advance_schedule(utc_iso: str, recurrence: str, now_utc: datetime) -> str:
    """Next occurrence strictly after now, stepping on the operator calendar."""
    local = datetime.fromisoformat(utc_iso).astimezone(ZoneInfo(OPERATOR_TZ))
    while local.astimezone(timezone.utc) <= now_utc:
        if recurrence == "daily":
            local += timedelta(days=1)
        elif recurrence == "weekly":
            local += timedelta(days=7)
        elif recurrence == "weekdays":
            local += timedelta(days=1)
            while local.weekday() >= 5:
                local += timedelta(days=1)
        elif recurrence == "monthly":
            y, m = (local.year + (local.month // 12), local.month % 12 + 1)
            import calendar
            day = min(local.day, calendar.monthrange(y, m)[1])
            local = local.replace(year=y, month=m, day=day)
        else:
            raise ValueError(f"unknown recurrence {recurrence!r}")
    return local.astimezone(timezone.utc).isoformat()


def _run_task_scheduler_pass(now_utc: Optional[datetime] = None) -> int:
    """Fire every due schedule once; re-arm recurring ones to their next
    future occurrence (a downtime backlog fires ONE late notification, never
    a burst). Callable directly from tests; the background loop just calls
    this on an interval."""
    now_utc = now_utc or datetime.now(timezone.utc)
    due = db.query(
        "SELECT * FROM tasks WHERE scheduled_at IS NOT NULL AND scheduled_at != '' "
        "AND scheduled_at <= ? AND status NOT IN ('done', 'cancelled')",
        (now_utc.isoformat(),))
    for t in due:
        recipient = t.get("assignee") or t.get("source")
        payload = {
            "summary": f"Scheduled: {t['title']}",
            "body": _notif_body(t.get("detail"), f"GET /api/v1/tasks/{t['id']}"),
            "recurrence": t.get("recurrence"),
        }
        db.execute(
            "INSERT INTO notifications (recipient, task_id, event_type, actor, payload) "
            "VALUES (?, ?, 'schedule.fired', 'registry', ?)",
            (recipient, t["id"], json.dumps(payload)))
        user = db.query_one("SELECT id FROM users WHERE username = ?", (recipient,))
        if user:
            event_bus.emit("schedule.fired", f"task:{t['id']}", payload,
                           scope=f"user:{user['id']}")
        nxt = None
        if t.get("recurrence") in _RECURRENCES:
            nxt = _advance_schedule(t["scheduled_at"], t["recurrence"], now_utc)
        db.execute("UPDATE tasks SET scheduled_at = ?, last_fired_at = ? WHERE id = ?",
                   (nxt, now_utc.isoformat(), t["id"]))
    return len(due)


class TicketCreate(StrictModel):
    # Note: `source` is intentionally absent. It's derived from the
    # bearer (see create_ticket → auth.identity()) and not trustable
    # from the body. With StrictModel's extra="forbid", clients sending
    # `source` will get a 422 — that surfaces the deprecation rather
    # than silently dropping. Same shape as `author` on TicketComment.
    type: str = "bug"
    priority: str = "medium"
    summary: str
    body: Optional[str] = None
    service_id: Optional[str] = None
    device_id: Optional[str] = None
    assignee: Optional[str] = None
    group_scope: Optional[str] = None       # #330: explicit group visibility (null = existing rules only)
    tags: Optional[list[str]] = None        # first-class, normalized (#203 views-over-data)

    # Ticket #23: some clients (three seats, at the time)
    # send the long-form text under names other than `body` — usually `description`,
    # but `content` / `text` / `details` / `message` have all been observed in the
    # wild. Normalise into `body` and POP the alias out of the dict so the
    # extra="forbid" check doesn't 422 on it.
    @model_validator(mode="before")
    @classmethod
    def _accept_body_aliases(cls, data):
        if isinstance(data, dict):
            current = (data.get("body") or "").strip()
            for alias in ("description", "content", "text", "details", "message"):
                if alias in data:
                    candidate = (data.pop(alias) or "").strip()
                    if not current and candidate:
                        data["body"] = candidate
                        current = candidate
        return data

    @model_validator(mode="after")
    def _normalize_priority(self):
        self.priority = _canon_priority(self.priority)
        return self


class ResourceRegister(StrictModel):
    name: str                               # e.g. 'gpu-0'
    device_id: str
    type: str                               # gpu, cpu, memory, storage, accelerator
    provider: str                           # nvidia, apple, amd, intel
    model: Optional[str] = None             # e.g. 'RTX 3090', 'M2 Pro'
    allocation_mode: str = "shared"         # shared, exclusive
    specs: Optional[dict] = None            # vram_mb, cuda_cores, unified_memory_mb, etc.


class ResourceStats(StrictModel):
    memory_used_mb: Optional[int] = None
    memory_total_mb: Optional[int] = None
    compute_pct: Optional[float] = None
    temperature_c: Optional[float] = None
    power_draw_w: Optional[float] = None
    processes: Optional[list[dict]] = None


class ResourceUpdate(StrictModel):
    status: Optional[str] = None
    specs: Optional[dict] = None
    model: Optional[str] = None
    expiry_policy: Optional[str] = None      # free (default) | hold — what an expired holder does to the ledger (task 868)


class LeaseRequest(StrictModel):
    service_id: Optional[str] = None
    memory_mb: Optional[int] = Field(default=None, strict=True, ge=0)
    exclusive: bool = False
    priority: str = "normal"
    duration_s: Optional[int] = None        # null = indefinite
    ttl_seconds: Optional[int] = None       # alias for duration_s; either works
    # Two numbers with different jobs (#827). ttl_seconds says "how long before you
    # should assume I died"; max_total_s says "the longest I could legitimately need
    # this". Renewal extends within the second and never past it, so a short TTL buys
    # liveness without capping honest work. Defaults to max(LEASE_MAX_TOTAL_S,
    # effective ttl), which is what every caller that names only a TTL gets.
    max_total_s: Optional[int] = None
    # The queue (0.12.98). With wait_s, a request the resource cannot grant now is
    # queued for that many seconds and promoted in order when the bytes free; the
    # response says `queued` and the position. Without it the refusal is a 409 as
    # before — except a shared request over the bytes, which queued before this
    # field existed and still does, bounded by the registry default wait.
    wait_s: Optional[int] = Field(default=None, strict=True, ge=0)
    # No deadline: no layer defaults to a timeout.
    # `wait: true` without `wait_s` queues the request with no `wait_until`: the row
    # leaves the queue by promotion or by its holder's DELETE, never by the clock. With
    # `wait_s` as well, the deadline stands. The queue reports; holders self-regulate.
    wait: Optional[bool] = Field(default=None, strict=True)

    def effective_duration_s(self) -> Optional[int]:
        """Return the requested lease lifetime, preferring duration_s when both set."""
        return self.duration_s if self.duration_s is not None else self.ttl_seconds


class LeaseRenew(StrictModel):
    # REQUIRED on purpose. Defaulting it would have to mean either the exclusive
    # default (silently shortening a lease whose holder omitted the field) or the
    # span the lease was granted with, which is lost after the first renewal
    # unless it is stored. A holder restating what it wants infers nothing (#827).
    ttl_seconds: int


class LeaseGroupMember(StrictModel):
    resource_id: str = Field(min_length=1, max_length=256)
    service_id: Optional[str] = None
    memory_mb: Optional[int] = Field(default=None, strict=True, ge=0)
    # Explicit renewable member lifetime. No group or assembly deadline.
    ttl_seconds: int = Field(strict=True, gt=0)
    max_total_s: Optional[int] = Field(default=None, strict=True, gt=0)


class LeaseGroupRequest(StrictModel):
    request_key: str = Field(min_length=1, max_length=128)
    members: list[LeaseGroupMember] = Field(min_length=2, max_length=16)


class LeaseGroupCommand(StrictModel):
    generation: int = Field(strict=True, gt=0)


class LeaseGroupAttestation(LeaseGroupCommand):
    # An immutable reference and digest, not raw logs, credentials or prompts.
    evidence_ref: str = Field(min_length=1, max_length=512)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class LeaseGroupFence(LeaseGroupCommand):
    reason: str = Field(min_length=1, max_length=512)


class TicketUpdate(StrictModel):
    status: Optional[str] = None
    assignee: Optional[str] = None
    priority: Optional[str] = None
    service_id: Optional[str] = None
    device_id: Optional[str] = None
    group_scope: Optional[str] = None       # #330: "" clears to commons; a group id scopes it
    tags: Optional[list[str]] = None        # full replacement when present

    @model_validator(mode="after")
    def _normalize_priority(self):
        self.priority = _canon_priority(self.priority)
        return self


class TicketComment(StrictModel):
    # Note: `author` is intentionally absent. Derived from the bearer
    # via auth.identity() in add_comment, not trustable from the body.
    # Sending `author` in the request body → 422 (extra="forbid").
    body: str
    parent_comment_id: Optional[int] = None  # one-level reply threading (#383)


class NotificationAck(StrictModel):
    ids: Optional[list[int]] = None
    all: bool = False
    # Who consumed it (#227): "wake" | "watch" | "subscribe" | "chat" | free
    # text ≤32 chars. Stored as acked_via so "handled async" claims in
    # digests are attribution, not fiction.
    surface: Optional[str] = None


class NotificationSeen(StrictModel):
    # Soft row lease (#227): a live surface SURFACED these rows (prompt
    # digest, subscription) without claiming them handled. While the lease is
    # fresh the cold rail skips them; if the surface dies without acking, the
    # lease lapses and the rail owns them again — no black hole, no display-
    # ack data loss. ttl_seconds=0 clears the lease.
    ids: list[int]
    ttl_seconds: int = 1800


class PresenceUpdate(StrictModel):
    # Foreground presence (doc #17): a live session says "I'm here" so the
    # agent's own wake rail defers to it. ttl_seconds=0 clears.
    ttl_seconds: int = 600


class AgentWake(StrictModel):
    # Schedule-native wake primitive (#303 item 1): a durable notification +
    # live SSE event addressed straight at an agent. It was built when filing
    # a throwaway ticket was the only wake mechanism there was, and it took no
    # object on purpose.
    #
    # The universal thread (doc 239) makes that the wrong shape: work arrives
    # wrapped in a thread, and a wake with none is a message with no work
    # behind it and nowhere to answer. Worse, `event_type` was free text and a
    # body made the row classify as a *message* (`_is_message`'s body arm), so
    # an arbitrary string could birth a holder on nothing. A thread is now
    # required, and the event type must be one the rail has heard of.
    event_type: str = "schedule.fired"
    thread_id: int
    summary: str
    body: Optional[str] = None


class WakeRegister(StrictModel):
    # Wake-rail fleet registration (doc #17; doc #22 step 1). The listener
    # upserts this on every SSE connect; the response carries the served
    # helper versions so a stale listener can self-update.
    #
    # `ended: true` RETIRES a row (#897), mirroring the surface precedent. Until
    # then a registration could be created by anyone and retired by nobody — GET
    # and this POST were the entire surface, for admin as much as for a seat — so
    # the one table feeding the fleet's health view could only ever grow.
    #
    # `identity` names someone else's row and is ADMIN-ONLY, valid only with
    # `ended`. It is the leg the ticket actually needs: a row is stale because the
    # seat is GONE, and a gone seat cannot call anything, so self-retire alone
    # only ever helps a seat shutting down cleanly — the case that was never the
    # problem.
    wake_cmd: str = ""          # not required on a retire: nothing to register
    host: str = ""
    ended: bool = False
    identity: Optional[str] = None
    listener_version: Optional[str] = None
    handler_version: Optional[str] = None   # null for custom (non-default) harnesses
    api_env: Optional[str] = None
    max_conn_age: Optional[int] = None      # seat's MAX_CONN_AGE_SEC — dark threshold input (#472)
    # Interactive-surface observability (#198): versions of the installed
    # hook scripts, best-effort read by the listener. Null = not installed —
    # that surface of the identity is dark and the fleet view says so.
    notif_hook_version: Optional[str] = None
    stop_hook_version: Optional[str] = None


class WakeBeacon(StrictModel):
    # Deaf-seat witness statement (task 609 c1024): what the listener's
    # out-of-band alarm already has in hand when its stream has been dark.
    # Identity comes from the bearer, deliberately never from the payload —
    # a self-declared seat field would be spoofable duplication of what the
    # token proves. Contract authored FROM the client's real fields, not
    # guessed at (the /presence 422 lesson).
    dark_for_s: int                         # since LAST successful stream
    last_error: Optional[str] = None        # verbatim, truncated server-side
    last_event_id: Optional[int] = None     # resume point from the heartbeat
    handler_version: Optional[str] = None   # so a bad rollout shows in the page


class WakeSessionReport(StrictModel):
    # Session-outcome report (#521): the handler already computes this verdict
    # and used to leave it in a local logfile. `outcome` is the ATTEMPT's
    # result, not the work's — reporting every attempt (not only failures) is
    # what lets the registry tell "idle" apart from "failing every launch".
    outcome: str                            # "ok" | "failed"
    rc: int = 0
    cause: Optional[str] = None             # see WAKE_CAUSES; None => "api"
    error: Optional[str] = None             # FIRST error line, never the tail
    host: Optional[str] = None
    unread: Optional[int] = None            # inbox depth that triggered the wake
    note: Optional[str] = None              # short free text


class ClaimBody(StrictModel):
    # Thread claims (#199): ttl a warm surface asks for when engaging a thread.
    ttl_seconds: int = 600


class ClaimRefresh(StrictModel):
    ttl_seconds: int = 600
    # Re-assertion (#227/#229): thread ids this surface KNOWS it is engaged
    # with (its subscription's claims file). Creates-or-extends STICKY claims,
    # so claim state converges to the surface actually listening instead of
    # depending on release ordering across parallel sessions.
    # #392: docs and tasks are claimable threads too — same convergence rule.
    reassert_ticket_ids: Optional[list[int]] = None
    reassert_doc_ids: Optional[list[int]] = None
    reassert_task_ids: Optional[list[int]] = None
    reassert_ttl_seconds: int = 600


class ClaimRelease(StrictModel):
    # Identity-wide releases fire from EVERY wake-session exit; by default
    # they must not clobber another live surface's explicit engagement
    # (#229 twin race). A surface going truly idle sends include_sticky=true.
    include_sticky: bool = False


class ThreadUpsert(StrictModel):
    # Thread binding (doc 174 / rail 3.9): which conversation holds a thread
    # for the calling identity. The rail posts this at birth, resume and
    # close; identity comes from the bearer, never the body. v19 (doc 239):
    # name the universal thread (thread_id) or any member object (kind +
    # object_id); the binding is keyed on the thread either way.
    kind: Optional[str] = None                  # ticket | doc | task
    object_id: Optional[int] = None
    thread_id: Optional[int] = None
    conversation_id: str                        # harness session id
    host: Optional[str] = None
    holder: str = "stream"                      # stream | tmux | oneshot | adapter | conversation
    pid: Optional[int] = None
    state: str = "open"                         # open | closing | closed
    rail_version: Optional[str] = None


class SurfaceRegister(StrictModel):
    # A conversation that can RECEIVE (rail 3.10). The rail creates headless
    # holders, so it knows them; a human's interactive session has to say it
    # exists. Identity comes from the bearer, never the body.
    conversation_id: str                        # harness session id
    surface: str = "interactive"                # interactive | stream | tmux | oneshot
    host: Optional[str] = None
    client: Optional[str] = None                # free text: what is at the other end
    ended: bool = False                         # true = this surface is going away


class ThreadPatch(StrictModel):
    state: Optional[str] = None
    pid: Optional[int] = None


class NotificationDelivered(StrictModel):
    # Delivery receipt (doc 174): the rail wrote these rows into a
    # conversation. Unlike `seen` (a lease) and `ack` (the reader's flag) a
    # delivery is a transport fact and does not expire: a delivered row is
    # never offered again, to anyone (`?undelivered=true` is
    # `delivered_to IS NULL`) — like mail, receipt is delivery.
    ids: list[int]
    conversation_id: str


# ── Docs plane (RFC #105) ────────────────────────────────────────────────
# `source` (owner) and `author` are intentionally absent from these models —
# both are bearer-derived (see create_doc / add_doc_revision), never trusted
# from the body. extra="forbid" surfaces a stray `author`/`source` as a 422.
class DocCreate(StrictModel):
    title: str
    body: str
    owner: Optional[str] = None            # responsible identity; defaults to creator
    slug: Optional[str] = None              # auto-derived from title if omitted
    kind: str = "doc"                       # doc | rfc | runbook | note
    group_scope: Optional[str] = None       # null = shared commons; else a group id
    supersedes: Optional[int] = None        # doc id this one replaces
    parent_doc_id: Optional[int] = None     # doc hierarchy: this doc is a sub-doc of the parent
    note: Optional[str] = None              # edit summary for revision 1

    @model_validator(mode="before")
    @classmethod
    def _accept_body_aliases(cls, data):
        # Same tolerance as TicketCreate (#23): some clients send the long text
        # under content/markdown/text/description. Normalise into `body`.
        if isinstance(data, dict):
            current = (data.get("body") or "").strip()
            for alias in ("content", "markdown", "text", "description", "details"):
                if alias in data:
                    candidate = (data.pop(alias) or "").strip()
                    if not current and candidate:
                        data["body"] = candidate
                        current = candidate
        return data


class DocRevisionCreate(StrictModel):
    # An edit = a new immutable revision (append-only history). Author derived
    # from the bearer. body required; title optional (null keeps current title).
    # base_rev optional (#491): the head rev this rewrite was based on — if the
    # doc has moved past it, the write 409s instead of silently discarding the
    # newer revision. Omit to keep the old last-writer-wins behavior.
    body: str
    title: Optional[str] = None
    note: Optional[str] = None              # edit summary ("why")
    base_rev: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def _accept_body_aliases(cls, data):
        if isinstance(data, dict):
            current = (data.get("body") or "").strip()
            for alias in ("content", "markdown", "text"):
                if alias in data:
                    candidate = (data.pop(alias) or "").strip()
                    if not current and candidate:
                        data["body"] = candidate
                        current = candidate
        return data


class DocUpdate(StrictModel):
    # Metadata-only lifecycle (owner/admin). Content edits (title/body) go
    # through POST /docs/{id}/revisions so every content change is tracked.
    status: Optional[str] = None            # draft | active | superseded | archived
    owner: Optional[str] = None
    kind: Optional[str] = None
    group_scope: Optional[str] = None       # "" clears to commons (like parent_doc_id's 0)
    supersedes: Optional[int] = None
    parent_doc_id: Optional[int] = None     # doc hierarchy; send 0 to clear


class DocLinkCreate(StrictModel):
    target_type: str                        # ticket | service | device | doc
    target_id: str


class DocComment(StrictModel):
    # The discussion layer — distinct from the versioned body. `author` is
    # bearer-derived (extra="forbid" rejects a body-supplied author), same as
    # TicketComment.
    body: str


class DocAppend(StrictModel):
    # Token-saving additive edit: append a block to the current body rather than
    # resubmitting the whole thing. Produces a normal immutable revision.
    block: str
    note: Optional[str] = None
    separator: str = "\n\n"


# ── Task plane (doc #13, ticket #148) ────────────────────────────────────
# The third leg of the trichotomy: docs = canonical records (not necessarily
# actionable), tickets = requests needing another's attention, tasks =
# actionable items with specific direction, for self or other agent(s),
# nested under projects. `source` is bearer-derived, same as tickets/docs.
class ProjectCreate(StrictModel):
    name: str
    description: Optional[str] = None
    slug: Optional[str] = None              # auto-derived from name if omitted
    status: str = "active"                  # planning | active | paused | done | archived
    group_scope: Optional[str] = None       # null = shared commons; else a group id

    # Ticket #189 finding 4: docs/tickets take `title`/`summary`; a client
    # generalising across the trichotomy naturally reaches for `title` here
    # too and gets extra="forbid" 422s. Same tolerance as the body aliases
    # above — accept `title` as an alias for `name`.
    @model_validator(mode="before")
    @classmethod
    def _accept_name_alias(cls, data):
        if isinstance(data, dict):
            if not (data.get("name") or "").strip() and "title" in data:
                candidate = (data.pop("title") or "").strip()
                if candidate:
                    data["name"] = candidate
            data.pop("title", None)
        return data


class ProjectUpdate(StrictModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    group_scope: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _accept_name_alias(cls, data):
        if isinstance(data, dict):
            if not (data.get("name") or "").strip() and "title" in data:
                candidate = (data.pop("title") or "").strip()
                if candidate:
                    data["name"] = candidate
            data.pop("title", None)
        return data


class ProjectTaskCreate(StrictModel):
    title: str
    detail: Optional[str] = None
    parent_task_id: Optional[int] = None    # nesting from day one
    status: str = "todo"
    priority: Optional[str] = None          # low | medium | high (normalized below)
    estimate_min: Optional[int] = None
    assignee: Optional[str] = None          # validated username — self or another agent
    due: Optional[str] = None               # ISO date
    position: Optional[int] = None          # manual ordering among siblings
    source_ref: Optional[str] = None        # provenance: capture span | ticket:# | doc:#
    tags: Optional[list[str]] = None        # first-class, normalized (#203 views-over-data)
    # Plane-native scheduling (doc 161 §5): at scheduled_at the task surfaces
    # in the assignee's inbox as schedule.fired; recurrence re-arms per
    # calendar. Naive datetimes are read in the OPERATOR's timezone.
    scheduled_at: Optional[str] = None      # ISO datetime; "" clears on update
    recurrence: Optional[str] = None        # daily | weekdays | weekly | monthly

    @model_validator(mode="after")
    def _normalize_priority(self):
        self.priority = _canon_priority(self.priority)
        self.scheduled_at, self.recurrence = _canon_schedule(
            self.scheduled_at, self.recurrence)
        return self


class ProjectTaskUpdate(StrictModel):
    title: Optional[str] = None
    detail: Optional[str] = None
    parent_task_id: Optional[int] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    estimate_min: Optional[int] = None
    actual_min: Optional[int] = None
    assignee: Optional[str] = None
    due: Optional[str] = None
    position: Optional[int] = None
    source_ref: Optional[str] = None
    tags: Optional[list[str]] = None        # full replacement when present
    scheduled_at: Optional[str] = None      # ISO datetime; "" clears the schedule
    recurrence: Optional[str] = None        # daily | weekdays | weekly | monthly; "" clears

    @model_validator(mode="after")
    def _normalize_schedule(self):
        if self.scheduled_at not in (None, ""):
            self.scheduled_at, _ = _canon_schedule(self.scheduled_at, None)
        if self.recurrence not in (None, ""):
            _, self.recurrence = _canon_schedule(None, self.recurrence)
        return self


class TaskCommentCreate(StrictModel):
    # author bearer-derived (never body-supplied) — same shape as TicketComment.
    body: str


class ViewCreate(StrictModel):
    # Saved view (#203 v2 / doc #34): a named filter over one entity. Owner
    # is bearer-derived. Everything validated against whitelists — a view can
    # only ever express what the data model supports.
    name: str
    entity: str
    filters: Optional[dict] = None
    sort: Optional[str] = None
    group_by: Optional[str] = None
    limit_per_group: Optional[int] = None
    position: Optional[int] = None


class ViewUpdate(StrictModel):
    name: Optional[str] = None
    filters: Optional[dict] = None
    sort: Optional[str] = None
    group_by: Optional[str] = None
    limit_per_group: Optional[int] = None
    position: Optional[int] = None


class TaskDelegate(StrictModel):
    # The ONE bridge from a personal/project task into the ticket plane —
    # always explicit, never automatic (v0.7.9 coordination integrity).
    assignee: str                           # agent/user to hand the work to
    note: Optional[str] = None
    priority: str = "medium"


class EnrollRequest(StrictModel):
    device_id: str
    name: Optional[str] = None           # display name (defaults to device_id)
    type: str = "device"                 # freeform: phone, laptop, server, gpu, bot

    # Access Route: this device accesses the network (phone, laptop)
    access_route: Optional[str] = None   # ssh-tunnel, lan, browser-only
    # Access Target: this device provides services, reachable via gateway
    access_target: Optional[str] = None  # direct-ssh, reverse-tunnel, http

    # Target connection details (when access_target is set)
    target_ip: Optional[str] = None      # LAN IP for direct-ssh
    target_ssh_user: Optional[str] = None
    target_ssh_port: int = 22

    # Group assignment (explicit — overrides auto-join from caller scope)
    group_ids: Optional[list[str]] = None

    # Compat: old field names still accepted
    hostname: Optional[str] = None       # alias for name
    context: Optional[str] = None        # deprecated, inferred
    inbound_method: Optional[str] = None # alias for access_route
    outbound_method: Optional[str] = None # alias for access_target
    outbound_ip: Optional[str] = None    # alias for target_ip
    outbound_ssh_user: Optional[str] = None # alias for target_ssh_user


class ActivateRequest(StrictModel):
    token: str
    ssh_pubkey: str
    startup_method: Optional[str] = None
    ssh_user: Optional[str] = None  # device reports `whoami` / $env:USERNAME at activation
    ssh_port: Optional[int] = None  # device-detected local sshd port. Default 22 is wrong
                                    # for some images (a WSL distro may run sshd on :2222
                                    # to dodge the Windows host's :22). Persisted as
                                    # outbound_ssh_port so the autossh -R target matches.
    host_pubkey: Optional[str] = None  # the key the device's own sshd presents, read off
                                       # that port. Pins the device before its first hop
                                       # instead of leaving the gateway to learn it (#821).


class AuthServiceRegister(StrictModel):
    name: str
    port: int
    health_endpoint: Optional[str] = None
    subdomain: Optional[str] = None
    domain: str = Field(default_factory=_service_domain)
    protocol: str = "http"
    version: Optional[str] = None
    capabilities: Optional[list[str]] = None


class ServiceSlotCreate(StrictModel):
    name: str                            # service name (e.g. "mediafactory")
    device_id: str                       # which device this runs on
    description: Optional[str] = None    # what this service does
    subdomain: Optional[str] = None      # external subdomain (auto-derived if not set)
    domain: str = Field(default_factory=_service_domain)


class ServiceSlotActivate(StrictModel):
    token: str                           # service enrollment token
    port: int                            # actual port the service runs on
    health_endpoint: Optional[str] = None
    protocol: str = "http"
    version: Optional[str] = None
    capabilities: Optional[list[str]] = None


class AdminInit(StrictModel):
    username: str
    password: str


class UserCreate(StrictModel):
    username: str
    password: str
    role: str = "user"
    scopes: Optional[list[str]] = None
    group_roles: Optional[dict[str, str]] = None  # per-group role for each group: scope, e.g. {"work": "editor"}; default viewer


class UserUpdate(StrictModel):
    role: Optional[str] = None
    scopes: Optional[list[str]] = None
    rotate_token: bool = False   # scope edits are in-place unless rotation is asked for by name (#472)


class UserInvite(StrictModel):
    username: str                            # identity (e.g. "claude-home", "alice")
    scopes: list[str]                        # e.g. ["device:test-host"], ["*"]
    role: str = "agent"                      # agent, user, admin
    label: Optional[str] = None              # human-readable description
    ttl: int = 3600                          # invite token TTL in seconds
    group_roles: Optional[dict[str, str]] = None  # per-group role override, e.g. {"work": "editor"}


class UserActivate(StrictModel):
    token: str                               # invite token (sc_invite_...)
    password: str                            # required — enables console login AND stored for key rotation


class GroupCreate(StrictModel):
    id: str
    name: str
    description: Optional[str] = None       # what the group is for — read by every newcomer's guide
    route_path: Optional[str] = None
    doc_id: Optional[int] = None            # the working doc: the group's knowledge base entry (scope v1)


class GroupUpdate(StrictModel):
    name: Optional[str] = None
    description: Optional[str] = None
    route_path: Optional[str] = None
    doc_id: Optional[int] = None            # 0 clears


class GroupServiceAdd(StrictModel):
    service_id: str


class GroupMemberAdd(StrictModel):
    user_id: int
    role: str = "viewer"


# --- Health ---

_RELEASE_CACHE: dict = {}
SCHEMA_ARM = "v20"     # the last arm of db._migrate — bump with it


def _release() -> dict:
    """What is running: the release the deploy host assigned at build
    (`SAMCLOUD_VERSION` — a merge into main is a release since 0.12.96; a checkout
    says `dev`), the commit baked in beside it (`SAMCLOUD_COMMIT`), the schema arm,
    the served helper and skill stamps. Read once; immutable for a container's
    lifetime. Scope v1 train 2 — codex (doc 260 §5) could not pin an answer to a
    build."""
    if not _RELEASE_CACHE:
        here = Path(__file__).parent
        version = os.environ.get("SAMCLOUD_VERSION", "").strip() or "dev"
        skill = "0"
        try:
            m = _re.search(r"SKILL_VERSION:\s*(\S+)", (here / "skills" / "samcloud-agent" / "SKILL.md").read_text())
            skill = m.group(1) if m else "0"
        except OSError:
            pass
        _RELEASE_CACHE.update({"version": version, "commit": os.environ.get("SAMCLOUD_COMMIT", "unknown"),
                               "schema": SCHEMA_ARM, "helpers": _rail_versions(), "skill": skill})
    return dict(_RELEASE_CACHE)


@app.get("/")
def root():
    return {"service": "samcloud-registry", **_release()}


@app.get("/health")
def health():
    try:
        count = db.query_one("SELECT count(*) as n FROM devices")
        return {"status": "healthy", "devices": count["n"] if count else 0}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


# --- Devices ---

# The known_hosts alias a device files the gateway's host key under. Keyed on a name
# rather than host:port so the pin survives the gateway changing address or port —
# GATEWAY_SSH_HOST/PORT differ per environment. Env-scoped so one machine can hold a
# prod and a staging pin at once.
_ENV_NAME = os.environ.get("SAMCLOUD_ENV", "prod")
GATEWAY_HOST_KEY_ALIAS = ("samcloud-gateway" if _ENV_NAME in ("", "prod")
                          else f"{_ENV_NAME}-samcloud-gateway")

SENSITIVE_DEVICE_FIELDS = {"ssh_pubkey", "outbound_ssh_user", "outbound_ssh_port", "tunnel_port", "outbound_ip"}


def _strip_sensitive(device: dict, auth: Optional[AuthContext]) -> dict:
    """Remove sensitive fields from device data for unauthenticated callers.
    Admin and device/service owners see all fields."""
    if auth is None:
        return {k: v for k, v in device.items() if k not in SENSITIVE_DEVICE_FIELDS}
    # Any authenticated caller sees full details
    return device


@app.get("/api/v1/devices")
def list_devices(context: str = None, auth: AuthContext = Depends(get_auth)):
    if context:
        devices = db.query("SELECT * FROM devices WHERE context = ? ORDER BY id", (_device_context(context),))
    else:
        devices = db.query("SELECT * FROM devices ORDER BY context, id")
    # Scope filtering: a non-admin sees only devices in their scope (scope v1
    # train 0: a bearer is required — the unauthenticated fleet inventory is gone)
    visible = _visible_device_ids(auth)
    if visible is not None:
        devices = [d for d in devices if d["id"] in visible]
    return [_strip_sensitive(d, auth) for d in devices]


def _gateway_ssh_endpoint():
    """Publicly-reachable SSH endpoint for the gateway (ssh-config aliases + guides).

    Defaults to PLANE_HOST:PLANE_SSH_PORT. In environments where the direct sshd host
    port isn't externally forwarded — e.g. staging, where only the sslh mux (:9443) is
    public and :2200 is LAN-only — override via GATEWAY_SSH_HOST / GATEWAY_SSH_PORT.
    """
    host = os.environ.get("GATEWAY_SSH_HOST") or _tunnel_host()
    port = os.environ.get("GATEWAY_SSH_PORT") or _plane_ssh_port()
    return host, port


@app.get("/api/v1/devices/{device_id}/guide")
def device_guide(device_id: str, auth: AuthContext = Depends(get_auth)):
    """Persistent guide for a device — how to access it, what's running on it."""
    device = _require_visible_device(auth, device_id)
    if not device:
        raise HTTPException(404, f"Device {device_id} not found")

    public_url = os.environ.get("PUBLIC_URL", "http://localhost:7778")
    gateway_host, ssh_port = _gateway_ssh_endpoint()

    services = db.query("SELECT id, name, status, port, external_url FROM services WHERE device_id = ?", (device_id,))
    svc_table = "\n".join(
        f"| `{s['name']}` | `{s['status']}` | `{s['port'] or '-'}` | `{s.get('external_url') or '-'}` |"
        for s in services
    ) if services else "| (none) | | | |"

    # SSH access instructions based on outbound method
    outbound = device.get("outbound_method") or "unknown"
    ssh_user = device.get("outbound_ssh_user") or "<ssh-user>"
    # Use `-l "<user>"` form so usernames with shell-special chars copy-paste safely.
    # OpenSSH still rejects names outside [A-Za-z0-9._-] — caught at activation as ssh_user_warning.
    ssh_user_flag = f'-l "{ssh_user}"'
    tunnel_port = device.get("tunnel_port")
    # gateway_host set above from _gateway_ssh_endpoint()

    if outbound == "reverse-tunnel" and tunnel_port:
        ssh_section = f"""## SSH Access

This device has a reverse tunnel on port {tunnel_port}, reachable through the gateway.

> **The gateway SSH user is always `samcloud`** — your identity rides in your access
> key's ForceCommand, *not* in the SSH username. Connecting as `<you>@{gateway_host}`
> fails with `Permission denied (publickey)`.

**Set up access once** (installs your key + an `ssh samcloud` alias — this is the clean path):
```bash
curl -s -X POST {public_url}/api/v1/access-keys/setup-script \\
  -H "Content-Type: application/json" \\
  -H @"$HOME/.samcloud/token.hdr" \\
  -d '{{"username":"<your-username>","device_name":"<your-device>"}}' | bash
```

**Interactive shell on this device:**
```bash
ssh samcloud                 # gateway menu → type: {device_id}
ssh samcloud {device_id}        # or jump straight to the shell
```

**Run one command (non-interactive — the form agents/scripts use):**
```bash
ssh samcloud "{device_id} -- <command>"   # stdout captured, remote exit code propagated
ssh samcloud "devices --json"             # machine-readable target list
```

**Direct via tunnel (bypasses the gateway, uses the gateway key):**
```bash
ssh -i ~/.ssh/samcloud -p {tunnel_port} {ssh_user_flag} localhost
```
"""
    elif outbound == "direct-ssh":
        ip = device.get("outbound_ip") or "<device-ip>"
        port = device.get("outbound_ssh_port") or 22
        ssh_section = f"""## SSH Access

> **The gateway SSH user is always `samcloud`** — identity rides in your access key, not the username.

**Through the gateway** (set up `ssh samcloud` via the setup-script first — see below):
```bash
ssh samcloud {device_id}                  # interactive shell
ssh samcloud "{device_id} -- <command>"   # one-off command, output captured
```

**Direct (on the LAN, bypassing the gateway):**
```bash
ssh -p {port} {ssh_user_flag} {ip}
```
"""
    else:
        ssh_section = f"""## SSH Access

Outbound method not configured. Set it up:
```bash
curl -s -X PATCH {public_url}/api/v1/devices/{device_id} \\
  -H @"$HOME/.samcloud/token.hdr" \\
  -H "Content-Type: application/json" \\
  -d '{{"outbound_method":"reverse-tunnel","outbound_ssh_user":"<user>"}}'
```
"""

    guide = f"""# Device Guide: {device_id}

| Field | Value |
|-------|-------|
| Name | {device.get('name', device_id)} |
| Type | {device.get('type', 'device')} |
| Status | {device.get('status', 'unknown')} |
| Outbound | {outbound} |
| Tunnel Port | {tunnel_port or 'none'} |
| SSH User | {ssh_user} |

{ssh_section}

## Services on this Device

| Service | Status | Port | External URL |
|---------|--------|------|-------------|
{svc_table}

## Granting User Access (Gateway Keys)

To let a user SSH to this device through the gateway, create an access key for them:

```bash
curl -s -X POST {public_url}/api/v1/access-keys \\
  -H "Content-Type: application/json" \\
  -H @"$HOME/.samcloud/token.hdr" \\
  -d '{{"username":"<their-username>","device_name":"{device_id}"}}'
```

This returns a private key (save it — shown once). The public key is auto-added to the gateway's authorized_keys with a ForceCommand binding the user to the gateway menu.

The user then connects (**SSH user is always `samcloud`** — their identity is carried by the key, not the username):
```bash
ssh -i <saved-key> -p {ssh_port} samcloud@<gateway-host>            # interactive menu → {device_id}
ssh -i <saved-key> -p {ssh_port} samcloud@<gateway-host> "{device_id} -- <cmd>"  # one-off command
```

Or install automatically via setup script (writes the key + an `ssh samcloud` alias):
```bash
curl -s -X POST {public_url}/api/v1/access-keys/setup-script \\
  -H "Content-Type: application/json" \\
  -H @"$HOME/.samcloud/token.hdr" \\
  -d '{{"username":"<their-username>","device_name":"{device_id}"}}' | bash
```

For a device that has no samcloud token (a new phone or laptop), mint a single-use pair link where you are signed in, then run its `pair_command` on the device:
```bash
curl -s -X POST {public_url}/api/v1/access-keys/pair \\
  -H "Content-Type: application/json" \\
  -H @"$HOME/.samcloud/token.hdr" \\
  -d '{{"username":"<their-username>","device_name":"<their-device>"}}'
# → {{"pair_command": "curl -fsS {public_url}/api/v1/pair/sc_pair_… | bash", "expires_in": 3600, …}}
```

## Register a Service

```bash
curl -s -X POST {public_url}/api/v1/services \\
  -H @"$HOME/.samcloud/token.hdr" \\
  -H "Content-Type: application/json" \\
  -d '{{"name":"my-service","device_id":"{device_id}","port":8080,"health_endpoint":"/health"}}'
```

## Hearing Async Work (Wake Rail)

Everything above is **network/SSH access only** — the device is reachable, but nothing here makes an agent running on it *hear* tickets, notifications, or assigned work. **Enrolled != reachable**: device-enrolled means the record exists, not that an agent is a participant.

If an agent runs on this device and should participate, give it its own identity + wake rail. Enroll is uniform; the rail *install* is the most env-divergent step (macOS LaunchDaemon/TCC, WSL keepalive, container systemd-user quirks) — see doc #33 (`GET {public_url}/api/v1/docs/33`, the agent-env setup recipe) for your platform. Every call below authenticates with a 0600 header file (`printf 'Authorization: Bearer %s\\n' "$(cat ~/.samcloud/token)" > ~/.samcloud/token.hdr`); the token itself never goes on a command line.
```bash
# 1. Invite an agent identity scoped to this device (admin/owner call, your own header file)
curl -s -X POST {public_url}/api/v1/users/invite \\
  -H @"$HOME/.samcloud/token.hdr" -H "Content-Type: application/json" \\
  -d '{{"username":"{device_id}-agent","role":"agent","scopes":["device:{device_id}"]}}'
# 2. The agent activates the invite (POST {public_url}/api/v1/users/activate), saves its token as
#    ~/.samcloud/token + token.hdr on its seat, then joins the rail (its own header file):
curl -s -H @"$HOME/.samcloud/token.hdr" {public_url}/api/v1/helpers/wake-install | bash
```

You are **not done** until the agent shows up live — an enrolled-but-railless agent is half-provisioned:
```bash
curl -s -H @"$HOME/.samcloud/token.hdr" {public_url}/api/v1/wake/registrations   # lists it with stale:false
```
Its operating guide is the served skill: `GET {public_url}/api/v1/skills/samcloud-agent/SKILL.md` (and `GET {public_url}/api/v1/users/me/guide` for its scope).

## Device Configuration

```bash
# Update device config
curl -s -X PATCH {public_url}/api/v1/devices/{device_id} \\
  -H @"$HOME/.samcloud/token.hdr" \\
  -H "Content-Type: application/json" \\
  -d '{{"outbound_ssh_user":"<user>","outbound_method":"reverse-tunnel","status":"online"}}'

# View this guide
curl -s {public_url}/api/v1/devices/{device_id}/guide \\
  -H @"$HOME/.samcloud/token.hdr"
```
"""

    return PlainTextResponse(guide, media_type="text/markdown")


@app.get("/api/v1/devices/{device_id}")
def get_device(device_id: str, auth: AuthContext = Depends(get_auth)):
    device = _require_visible_device(auth, device_id)
    services = db.query("SELECT * FROM services WHERE device_id = ?", (device_id,))
    device["services"] = services
    return _strip_sensitive(device, auth)


@app.delete("/api/v1/devices/{device_id}")
def delete_device(device_id: str, auth: AuthContext = Depends(get_auth)):
    """Delete a device. Requires admin or group scope."""
    device = _require_managed_device(auth, device_id)

    with db.transaction() as conn:
        # Clean up associated data (order matters — respect FK chains)
        # leases → resources, leases → services
        conn.execute("DELETE FROM leases WHERE resource_id LIKE ? OR service_id LIKE ?",
                     (f"{device_id}/%", f"{device_id}/%"))
        # group_services → services
        conn.execute("DELETE FROM group_services WHERE service_id LIKE ?", (f"{device_id}/%",))
        # routes → services
        conn.execute("DELETE FROM routes WHERE service_id LIKE ?", (f"{device_id}/%",))
        # ticket_comments + thread_claims → tickets
        conn.execute("""DELETE FROM ticket_comments WHERE ticket_id IN
                        (SELECT id FROM tickets WHERE device_id = ? OR service_id LIKE ?)""",
                     (device_id, f"{device_id}/%"))
        conn.execute("""DELETE FROM thread_claims WHERE ticket_id IN
                        (SELECT id FROM tickets WHERE device_id = ? OR service_id LIKE ?)""",
                     (device_id, f"{device_id}/%"))
        conn.execute("DELETE FROM tickets WHERE device_id = ? OR service_id LIKE ?",
                     (device_id, f"{device_id}/%"))
        # services (now safe — no remaining FKs)
        conn.execute("DELETE FROM services WHERE device_id = ?", (device_id,))
        # resources → devices
        conn.execute("DELETE FROM resources WHERE device_id = ?", (device_id,))
        # ports, tokens, group_devices
        conn.execute("DELETE FROM ports WHERE owner_id = ? OR owner_id LIKE ?",
                     (device_id, f"{device_id}/%"))
        conn.execute("DELETE FROM tokens WHERE owner_id = ?", (device_id,))
        conn.execute("DELETE FROM group_devices WHERE device_id = ?", (device_id,))
        # device itself (now safe)
        conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))

    db.audit("system:admin", "device.deleted", device_id,
             {"status": device.get("status"), "type": device.get("type")})

    return {"status": "deleted", "device_id": device_id}


@app.post("/api/v1/devices")
def register_device(req: DeviceRegister, auth: AuthContext = Depends(require_admin)):
    _validate_device_identity(req.id)
    existing = db.query_one("SELECT id FROM devices WHERE id = ?", (req.id,))
    if existing:
        raise HTTPException(409, f"Device {req.id} already registered")

    now = datetime.now(timezone.utc).isoformat()
    name = req.name or req.hostname or req.id
    outbound_ip = req.outbound_ip or req.ip
    outbound_ssh_user = req.outbound_ssh_user or req.ssh_user

    outbound_method = req.outbound_method
    transport_owner = req.transport_owner
    tunnel_port = req.tunnel_port
    # The legacy label alone meant "reached by reverse tunnel" (#1010): with no
    # method named, infer it, so the method-keyed forward port still follows.
    if outbound_method is None and _device_context(req.context) == "tunnel":
        outbound_method = "reverse-tunnel"

    # Shared transport: a secondary user on an already-enrolled machine borrows
    # that machine's reverse tunnel. We MIRROR the owner's tunnel_port (so the
    # gateway routes this device through the same landing port) but allocate NO
    # new port and the bootstrap creates NO tunnel — the device is differentiated
    # purely by outbound_ssh_user at gateway auth. This is the plane host's multi-user
    # pattern generalized to remote machines (ticket #100).
    if transport_owner:
        owner = db.query_one("SELECT id, tunnel_port FROM devices WHERE id = ?", (transport_owner,))
        if not owner:
            raise HTTPException(400, f"transport_owner '{transport_owner}' is not a registered device")
        if not owner.get("tunnel_port"):
            raise HTTPException(400, f"transport_owner '{transport_owner}' has no reverse tunnel to share (its tunnel_port is null)")
        tunnel_port = owner["tunnel_port"]   # mirror owner's landing port for gateway routing
        outbound_method = "reverse-tunnel"   # transport is the owner's tunnel
    # Auto-allocate tunnel port for reverse-tunnel devices
    elif outbound_method == "reverse-tunnel" and tunnel_port is None:
        tunnel_port = port_alloc.allocate("tunnel", req.id)
    # Compat: old context-based allocation
    elif _device_context(req.context) == "tunnel" and tunnel_port is None:
        tunnel_port = port_alloc.allocate("tunnel", req.id)

    context = _device_context(req.context) or ("tunnel" if outbound_method == "reverse-tunnel" else "samcloud")

    with db.transaction() as conn:
        conn.execute("""
            INSERT INTO devices (id, name, hostname, context, type, owner_id,
                inbound_method, inbound_endpoint,
                outbound_method, outbound_ip, outbound_ssh_user, outbound_ssh_port,
                tunnel_port, lifeline_port, transport_owner, monitor_port,
                startup_method, recovery_method,
                enrolled, enrolled_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'online')
        """, (req.id, name, name, context, req.type, req.owner_id,
              req.inbound_method, req.inbound_endpoint,
              outbound_method, outbound_ip, outbound_ssh_user, req.outbound_ssh_port,
              tunnel_port, req.lifeline_port, transport_owner, req.monitor_port,
              req.startup_method, req.recovery_method,
              now))

        # Only claim the port in the ledger if we actually own it. A borrowed
        # (transport_owner) tunnel_port belongs to the owner device — claiming it
        # here would double-book the port and break release/realloc accounting.
        if tunnel_port and not transport_owner:
            conn.execute(
                "INSERT OR IGNORE INTO ports (port, type, owner_id) VALUES (?, 'tunnel', ?)",
                (tunnel_port, req.id),
            )
        if req.lifeline_port:
            conn.execute(
                "INSERT OR IGNORE INTO ports (port, type, owner_id) VALUES (?, 'lifeline', ?)",
                (req.lifeline_port, req.id),
            )
        if req.monitor_port:
            conn.execute(
                "INSERT OR IGNORE INTO ports (port, type, owner_id) VALUES (?, 'monitor', ?)",
                (req.monitor_port, req.id),
            )

    db.audit("system:registry", "device.register", req.id,
             {"hostname": req.hostname, "context": req.context, "tunnel_port": tunnel_port})

    return {"status": "registered", "device_id": req.id, "tunnel_port": tunnel_port}


class DeviceUpdate(StrictModel):
    name: Optional[str] = None
    hostname: Optional[str] = None
    context: Optional[str] = None
    type: Optional[str] = None
    outbound_method: Optional[str] = None
    outbound_ip: Optional[str] = None
    outbound_ssh_user: Optional[str] = None
    outbound_ssh_port: Optional[int] = None
    tunnel_port: Optional[int] = None
    lifeline_port: Optional[int] = None
    transport_owner: Optional[str] = None
    monitor_port: Optional[int] = None
    startup_method: Optional[str] = None
    recovery_method: Optional[str] = None
    status: Optional[str] = None


@app.patch("/api/v1/devices/{device_id}")
def update_device(device_id: str, req: DeviceUpdate, auth: AuthContext = Depends(get_auth)):
    """Update device fields — the device's manager (admin, `device:` scope, or a group holding it)."""
    device = _require_managed_device(auth, device_id)

    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")

    now = datetime.now(timezone.utc).isoformat()
    updates["updated_at"] = now

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [device_id]
    db.execute(f"UPDATE devices SET {set_clause} WHERE id = ?", tuple(values))

    db.audit(auth.owner_id, "device.updated", device_id, updates)
    return db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,))


# --- The gateway's own host key (#825) ---


@app.get("/api/v1/gateway/host-key")
def gateway_host_key():
    """The host keys the gateway's sshd presents, so a device can verify the tunnel
    it dials instead of trusting whatever answers on that address.

    Unauthenticated on purpose. The gateway offers these exact keys to anyone who
    opens a TCP connection to its SSH port, so gating the published copy protects
    nothing — and a device needs them at the one moment its only credential may be a
    single-use enrollment token.

    Read per request from the volume sshd publishes into, so a regenerated host key
    is followed with nothing to clear. 503 rather than an empty list when that is
    unreadable: a caller must be able to tell "not published" from "no keys".
    """
    keys = forward.sshd_host_pubkeys()
    if not keys:
        raise HTTPException(503, {
            "error": "the gateway's host keys are not published",
            "detail": f"no readable .pub in {forward.SSHD_HOST_KEYS_DIR}",
            "remedy": "check that the sshd container has booted and that sshd-hostpub is mounted",
        })
    return {"host_keys": keys, "alias": GATEWAY_HOST_KEY_ALIAS}


# --- Device host keys (#818) ---
#
# `devices.host_pubkey` is the key that device's sshd presents. The gateway pins
# every hop against it (`gateway/main.go`), so a target that answers with a
# different key is refused instead of trusted.

HOST_KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}


def _parse_host_pubkey(raw: str) -> str:
    """Validate an SSH host public key and return it as `<type> <base64>`.

    The gateway writes this value into a known_hosts file, so it is validated
    rather than trusted: exactly one key, a known type, a base64 body that
    decodes, and an algorithm name inside the blob matching the type. Anything
    carrying a newline is refused — a second line in known_hosts would pin a
    host pattern nobody asked for.
    """
    if not isinstance(raw, str):
        raise HTTPException(400, "host_pubkey must be a string")
    if "\n" in raw or "\r" in raw:
        raise HTTPException(400, "host_pubkey must be a single line")
    fields = raw.strip().split()
    # A trailing comment is allowed (ssh-keygen writes one) and discarded.
    if len(fields) < 2:
        raise HTTPException(400, "host_pubkey must be '<type> <base64>'")
    keytype, body = fields[0], fields[1]
    if keytype not in HOST_KEY_TYPES:
        raise HTTPException(400, f"unsupported host key type '{keytype}'")
    try:
        blob = base64.b64decode(body, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(400, "host_pubkey body is not valid base64")
    # SSH wire format: uint32 length, then that many bytes of algorithm name.
    if len(blob) < 4:
        raise HTTPException(400, "host_pubkey body is too short")
    name_len = int.from_bytes(blob[:4], "big")
    if name_len > len(blob) - 4:
        raise HTTPException(400, "host_pubkey body is malformed")
    if blob[4:4 + name_len].decode("utf-8", "replace") != keytype:
        raise HTTPException(400, "host_pubkey type does not match its body")
    return f"{keytype} {body}"


def _host_key_fingerprint(host_pubkey: str) -> str:
    """`SHA256:...` fingerprint, the form ssh itself prints."""
    body = host_pubkey.split()[1]
    digest = hashlib.sha256(base64.b64decode(body)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _require_host_key_writer(auth: AuthContext, device_id: str) -> dict:
    """The device row for a host-key write: the admin (which includes the
    gateway's own `*` token), or that device speaking with its own token.

    Deliberately tighter than `_require_managed_device`. A `device:` scope or a
    group holding the device is enough to *reach* a device, and would therefore
    be enough to pin a key on one that has never been pinned — which moves
    trust-on-first-use out of the network and into the API, where it is worse:
    the gateway would then refuse the real host forever in favour of whoever
    called first.
    """
    if auth.is_admin() or auth.owner_id == device_id:
        device = db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,))
        if not device:
            raise HTTPException(404, f"Device {device_id} not found")
        return device
    # Outside the caller's scope the device is hidden, not refused (doc 261).
    _require_visible_device(auth, device_id)
    raise HTTPException(403, f"You cannot set the host key for {device_id}")


class HostKeyRecord(StrictModel):
    host_pubkey: str


@app.post("/api/v1/devices/{device_id}/host-key")
def record_device_host_key(device_id: str, req: HostKeyRecord,
                           auth: AuthContext = Depends(get_auth)):
    """Pin the host key the gateway checks this device against — only if absent.

    409 when a different key is already pinned. The gateway's learn-on-first-hop
    path makes this exact call, so trust-on-first-use can never overwrite a pin
    that already exists. Changing a pinned key is DELETE, then POST.

    The WHERE clause is the check. Reading the row and then writing it would be
    two steps: this handler is a sync def, so it runs in the threadpool, and two
    first hops to one device can both read an empty pin and the second write
    would silently win — the overwrite the 409 exists to prevent.
    """
    _require_host_key_writer(auth, device_id)
    host_pubkey = _parse_host_pubkey(req.host_pubkey)
    fingerprint = _host_key_fingerprint(host_pubkey)

    now = datetime.now(timezone.utc).isoformat()
    written = db.execute_count(
        "UPDATE devices SET host_pubkey = ?, host_pubkey_recorded_at = ?, updated_at = ? "
        "WHERE id = ? AND (host_pubkey IS NULL OR host_pubkey = '')",
        (host_pubkey, now, now, device_id),
    )
    if written:
        db.audit(auth.owner_id, "device.host_key.pinned", device_id,
                 {"fingerprint": fingerprint})
        return {"device_id": device_id, "host_pubkey": host_pubkey,
                "fingerprint": fingerprint, "recorded": True, "recorded_at": now}

    # Nothing was written: something is already pinned. Re-read to say what.
    device = db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,))
    if not device:
        raise HTTPException(404, f"Device {device_id} not found")
    existing = (device.get("host_pubkey") or "").strip()
    if existing == host_pubkey:
        # Idempotent: the same key is pinned, whoever got there first.
        return {"device_id": device_id, "host_pubkey": host_pubkey,
                "fingerprint": fingerprint, "recorded": False,
                "recorded_at": device.get("host_pubkey_recorded_at")}

    db.audit(auth.owner_id, "device.host_key.conflict", device_id,
             {"pinned": _host_key_fingerprint(existing), "offered": fingerprint})
    raise HTTPException(409, {
        "error": f"{device_id} already has a different host key pinned",
        "pinned_fingerprint": _host_key_fingerprint(existing),
        "offered_fingerprint": fingerprint,
        "remedy": f"DELETE /api/v1/devices/{device_id}/host-key to accept a re-key",
    })


@app.delete("/api/v1/devices/{device_id}/host-key")
def clear_device_host_key(device_id: str, auth: AuthContext = Depends(get_auth)):
    """Clear the pin, so a legitimate re-key is accepted.

    The next hop learns the key the device now presents and pins that. A device
    that re-keys itself calls this with its own token and then POSTs the new
    key — no person in the loop.
    """
    device = _require_host_key_writer(auth, device_id)
    existing = (device.get("host_pubkey") or "").strip()

    # Same shape as the pin: the WHERE clause decides, so `cleared` reports what
    # this call actually did rather than what the read before it saw.
    now = datetime.now(timezone.utc).isoformat()
    cleared = db.execute_count(
        "UPDATE devices SET host_pubkey = NULL, host_pubkey_recorded_at = NULL, updated_at = ? "
        "WHERE id = ? AND host_pubkey IS NOT NULL AND host_pubkey != ''",
        (now, device_id),
    )
    if not cleared:
        return {"device_id": device_id, "cleared": False}

    body = {"device_id": device_id, "cleared": True}
    if existing:
        fingerprint = _host_key_fingerprint(existing)
        body["was_fingerprint"] = fingerprint
        db.audit(auth.owner_id, "device.host_key.cleared", device_id,
                 {"fingerprint": fingerprint})
    else:
        db.audit(auth.owner_id, "device.host_key.cleared", device_id, {})
    return body


# --- Services ---

def _deserialize_service(svc: dict) -> dict:
    """Parse JSON text fields (capabilities) into native types."""
    if svc and isinstance(svc.get("capabilities"), str):
        try:
            svc["capabilities"] = json.loads(svc["capabilities"])
        except (json.JSONDecodeError, TypeError):
            pass
    return svc


@app.get("/api/v1/services")
def list_services(device_id: str = None, status: str = None,
                  auth: AuthContext = Depends(get_auth)):
    clauses, params = [], []
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if status:
        clauses.append("status = ?")
        params.append(status)

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = db.query(f"SELECT * FROM services{where} ORDER BY device_id, name", tuple(params))
    # Scope filtering: a non-admin sees only services on visible devices
    if True:
        visible = _visible_device_ids(auth)
        if visible is not None:
            rows = [r for r in rows if r["device_id"] in visible]
    return [_deserialize_service(r) for r in rows]


@app.get("/api/v1/services/{service_id:path}/guide")
def service_guide(service_id: str, auth: AuthContext = Depends(get_auth)):
    """Persistent operations guide for an activated service.

    Authenticated — use your service token. Always available, never expires.
    This is your ongoing reference for operating within samcloud.
    """
    svc = _require_visible_service(auth, service_id)

    device = db.query_one("SELECT * FROM devices WHERE id = ?", (svc["device_id"],))
    public_url = os.environ.get("PUBLIC_URL", "http://localhost:7778")

    siblings = db.query(
        "SELECT id, name, status, port FROM services WHERE device_id = ? AND id != ?",
        (svc["device_id"], service_id),
    )
    sibling_lines = "\n".join(
        f"| `{s['name']}` | `{s['status']}` | `{s['port']}` |"
        for s in siblings
    ) if siblings else "| (none) | | |"

    guide = f"""# Operations Guide: {svc['name']}

You are **{service_id}** — an activated service in the samcloud registry.
This guide is your permanent reference. It's always available at this URL with your service token.

## Your Identity

| Field | Value |
|-------|-------|
| Service ID | `{service_id}` |
| Device | `{svc['device_id']}` |
| Port | `{svc.get('port', 'unknown')}` |
| Forward port | `{svc.get('slice_port', 'none (local)')}` |
| Subdomain | `{svc.get('subdomain', 'none')}` |
| External URL | `{svc.get('external_url', 'not configured')}` |
| Status | `{svc['status']}` |
| Version | `{svc.get('version', 'not set')}` |

## What Your Token Can Do

Your service token (`sc_service_...`) is scoped to **{service_id}**. Keep it in a 0600 header file and pass the file — the token never goes on a command line (`printf` is a shell builtin, so it never enters a process list):

```bash
umask 077; mkdir -p ~/.samcloud
printf 'Authorization: Bearer %s\\n' "$SERVICE_TOKEN" > ~/.samcloud/service.hdr
```

Here's what you can and cannot do:

### Allowed
- **Report health**: `POST /api/v1/services/{service_id}/health`
- **Update your metadata**: `PATCH /api/v1/services/{service_id}` (version, capabilities, port, health_endpoint, status)
- **File tickets**: `POST /api/v1/tickets`
- **Comment on tickets**: `POST /api/v1/tickets/{{id}}/comments`
- **Read everything**: all GET endpoints are public

### Not allowed
- Modify other services
- Create or delete devices
- Create enrollment tokens
- Delete your own service

## Health Reporting

Report health every **60 seconds** (recommended). The registry tracks your last health time and status.

```bash
curl -s -X POST "{public_url}/api/v1/services/{service_id}/health" \\
  -H @"$HOME/.samcloud/service.hdr"
```

If you stop reporting, your status will go stale. Other services and agents can see when you last reported.

## Updating Your Metadata

Use PATCH (not PUT) to update fields:

```bash
curl -s -X PATCH "{public_url}/api/v1/services/{service_id}" \\
  -H @"$HOME/.samcloud/service.hdr" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "version": "1.2.0",
    "capabilities": ["your", "capabilities"],
    "status": "online"
  }}'
```

Updatable fields: `port`, `health_endpoint`, `protocol`, `version`, `capabilities`, `status`.

## Filing Tickets

When something is wrong or you need attention:

```bash
curl -s -X POST "{public_url}/api/v1/tickets" \\
  -H @"$HOME/.samcloud/service.hdr" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "source": "{service_id}",
    "type": "bug",
    "priority": "medium",
    "summary": "Brief description"
  }}'
```

Types: `bug`, `enhancement`, `question`. Priorities: `low`, `medium`, `high`, `critical`.

To comment on existing tickets: `POST /api/v1/tickets/{{id}}/comments` with `{{"author": "{service_id}", "body": "..."}}`

## Discovering Other Services

```bash
# All services
curl -s {public_url}/api/v1/services

# Services on your device
curl -s "{public_url}/api/v1/services?device_id={svc['device_id']}"

# Full API index
curl -s {public_url}/api/v1/
```

### Other services on {svc['device_id']}

| Name | Status | Port |
|------|--------|------|
{sibling_lines}

## Routes & External Access

{"Your service is routed at `" + svc['subdomain'] + "." + (svc.get('domain') or _service_domain()) + "` → `localhost:" + str(svc.get('slice_port') or svc.get('port')) + "`." if svc.get('subdomain') else "No subdomain configured. Ask an admin to set one up."}

## Getting Help

- **API index**: `GET {public_url}/api/v1/`
- **This guide**: `GET {public_url}/api/v1/services/{service_id}/guide` (with your token)
- **File a ticket**: when in doubt, file a ticket. Humans and other agents monitor them.
"""

    return PlainTextResponse(guide, media_type="text/markdown")


@app.get("/api/v1/services/{service_id:path}")
def get_service(service_id: str, auth: AuthContext = Depends(get_auth)):
    svc = _require_visible_service(auth, service_id)
    return _deserialize_service(svc)


@app.put("/api/v1/services/{service_id:path}")
def put_service_not_allowed(service_id: str):
    """PUT is not supported — use PATCH to update service fields."""
    raise HTTPException(405, "Use PATCH to update services, not PUT")


class ServiceUpdate(StrictModel):
    port: Optional[int] = None
    health_endpoint: Optional[str] = None
    docs_endpoint: Optional[str] = None
    description: Optional[str] = None
    protocol: Optional[str] = None
    version: Optional[str] = None
    capabilities: Optional[list[str]] = None
    status: Optional[str] = None


@app.patch("/api/v1/services/{service_id:path}")
def update_service(service_id: str, req: ServiceUpdate, auth: AuthContext = Depends(get_auth)):
    """Update service metadata — the admin, the service's own token, its creator, or its device's manager."""
    svc = _require_service_manager(auth, service_id)

    if req.status is not None and req.status not in VALID_SERVICE_STATUSES:
        raise HTTPException(422,
            f"Invalid status '{req.status}'. Must be one of: {', '.join(sorted(VALID_SERVICE_STATUSES))}")

    updates = {}
    if req.port is not None: updates["port"] = req.port
    if req.health_endpoint is not None: updates["health_endpoint"] = req.health_endpoint
    if req.docs_endpoint is not None: updates["docs_endpoint"] = req.docs_endpoint
    if req.description is not None: updates["description"] = req.description
    if req.protocol is not None: updates["protocol"] = req.protocol
    if req.version is not None: updates["version"] = req.version
    if req.capabilities is not None: updates["capabilities"] = json.dumps(req.capabilities)
    if req.status is not None: updates["status"] = req.status

    if not updates:
        return svc

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [service_id]

    db.execute(f"UPDATE services SET {set_clause} WHERE id = ?", tuple(values))
    db.audit(f"token:{auth.owner_id}", "service.updated", service_id, updates)

    return db.query_one("SELECT * FROM services WHERE id = ?", (service_id,))


@app.post("/api/v1/services/{service_id:path}/health")
def report_service_health(service_id: str, auth: AuthContext = Depends(get_auth)):
    """Report service health. Called by the service itself using its service token (or its device's manager)."""
    svc = _require_service_manager(auth, service_id)

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE services SET last_health = ?, last_health_status = 200, status = 'online', updated_at = ? WHERE id = ?",
        (now, now, service_id),
    )

    event_bus.emit("health.reported", service_id,
                   {"service_id": service_id, "status": 200},
                   scope=f"device:{svc['device_id']}")

    return {"status": "ok", "service_id": service_id, "last_health": now}


@app.delete("/api/v1/services/{service_id:path}")
def delete_service(service_id: str, auth: AuthContext = Depends(get_auth)):
    """Delete a service — the admin, its own token, its creator, or its device's manager."""
    svc = _require_service_manager(auth, service_id)

    # Stop port forward if running
    if svc.get("slice_port"):
        forward.stop_forward(svc["slice_port"], service_id)

    # Remove Caddy route and DNS record if subdomain was set
    caddy_status = None
    dns_status = None
    if svc.get("subdomain"):
        success, msg = caddy.remove_route(svc["subdomain"], (svc.get("domain") or _service_domain()))
        caddy_status = {"success": success, "message": msg}
        dns_status = _dns_record("remove", svc["subdomain"], svc.get("domain") or _service_domain())

    with db.transaction() as conn:
        conn.execute("DELETE FROM leases WHERE service_id = ?", (service_id,))
        conn.execute("DELETE FROM routes WHERE service_id = ?", (service_id,))
        conn.execute("DELETE FROM ports WHERE owner_id = ?", (service_id,))
        conn.execute("DELETE FROM tokens WHERE owner_id = ?", (service_id,))
        conn.execute("DELETE FROM group_services WHERE service_id = ?", (service_id,))
        conn.execute("DELETE FROM services WHERE id = ?", (service_id,))

    db.audit("system:admin", "service.deleted", service_id,
             {"caddy": caddy_status, "dns": dns_status})
    return {"status": "deleted", "service_id": service_id,
            "route": {"caddy": caddy_status, "dns": dns_status}}


@app.post("/api/v1/services")
def register_service(req: ServiceRegister, auth: AuthContext = Depends(get_auth)):
    """Register a service. Requires admin or device scope."""
    _validate_service_name(req.name)
    if req.port is not None:
        _validate_port(req.port)
    if hasattr(req, 'status') and req.status is not None and req.status not in VALID_SERVICE_STATUSES:
        raise HTTPException(422,
            f"Invalid status '{req.status}'. Must be one of: {', '.join(sorted(VALID_SERVICE_STATUSES))}")
    if req.subdomain:
        _validate_subdomain(req.subdomain)

    # The device must exist, be visible, and be the caller's to manage
    device = _require_managed_device(auth, req.device_id)

    # Check subdomain uniqueness
    if req.subdomain:
        existing_route = db.query_one(
            "SELECT service_id FROM routes WHERE subdomain = ? AND domain = ?",
            (req.subdomain, req.domain))
        if existing_route:
            raise HTTPException(409, f"Subdomain {req.subdomain}.{req.domain} already claimed by {existing_route['service_id']}")

    service_id = f"{req.device_id}/{req.name}"
    existing = db.query_one("SELECT id FROM services WHERE id = ?", (service_id,))
    if existing:
        raise HTTPException(409, f"Service {service_id} already registered")

    # Check port collision on same device
    if req.port:
        port_clash = db.query_one(
            "SELECT id FROM services WHERE device_id = ? AND port = ?",
            (req.device_id, req.port))
        if port_clash:
            raise HTTPException(409, f"Port {req.port} already in use by {port_clash['id']}")

    # Auto-allocate host forward port if needed
    slice_port = req.slice_port
    if slice_port is None and device.get("outbound_method") == "reverse-tunnel":
        slice_port = port_alloc.allocate("forward", service_id)

    external_url = None
    if req.subdomain:
        external_url = _external_url(req.subdomain, req.domain)

    with db.transaction() as conn:
        conn.execute("""
            INSERT INTO services (id, name, device_id, port, slice_port, protocol,
                health_endpoint, docs_endpoint, description, subdomain, domain, external_url, status, version, capabilities, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
        """, (service_id, req.name, req.device_id, req.port, slice_port, req.protocol,
              req.health_endpoint, req.docs_endpoint, req.description,
              req.subdomain, req.domain, external_url,
              req.version, json.dumps(req.capabilities) if req.capabilities else None,
              auth.owner_id))

        # Register port allocations
        if slice_port:
            conn.execute(
                "INSERT OR IGNORE INTO ports (port, type, owner_id) VALUES (?, 'forward', ?)",
                (slice_port, service_id),
            )

        # Create route if subdomain specified. For tunnelled services the
        # caddy target points at the sshd container (where the local-forward
        # listener lives), not the registry/caddy container's loopback.
        if req.subdomain:
            forward_host = os.environ.get("FORWARD_SSH_HOST", "localhost")
            target = f"{forward_host}:{slice_port}" if slice_port else f"{device.get('outbound_ip', 'localhost')}:{req.port}"
            conn.execute("""
                INSERT OR IGNORE INTO routes (subdomain, domain, target, service_id)
                VALUES (?, ?, ?, ?)
            """, (req.subdomain, req.domain, target, service_id))

        # Auto-add to groups that contain the device
        device_groups = conn.execute(
            "SELECT group_id FROM group_devices WHERE device_id = ?", (req.device_id,)
        ).fetchall()
        for g in device_groups:
            conn.execute(
                "INSERT OR IGNORE INTO group_services (group_id, service_id) VALUES (?, ?)",
                (g["group_id"], service_id),
            )

    # Wire port forward through device tunnel
    forward_status = None
    if slice_port and device.get("tunnel_port"):
        success, msg = forward.start_forward(
            slice_port=slice_port,
            service_port=req.port,
            tunnel_port=device["tunnel_port"],
            service_id=service_id,
            device_ssh_user=device.get("outbound_ssh_user"),
        )
        forward_status = {"success": success, "message": msg}

    # Wire Caddy route — for tunnelled services the upstream is the sshd
    # container (where the local-forward listener binds), not localhost.
    caddy_status = None
    if req.subdomain:
        try:
            forward_host = os.environ.get("FORWARD_SSH_HOST", "localhost")
            caddy_target = f"http://{forward_host}:{slice_port}" if slice_port else f"http://{device.get('outbound_ip', 'localhost')}:{req.port}"
            success, msg = caddy.append_route(req.subdomain, req.domain, caddy_target)
            caddy_status = {"success": success, "message": msg}
        except Exception as e:
            caddy_status = {"success": False, "message": str(e)}

    # Wire DNS record
    dns_status = None
    if req.subdomain:
        dns_status = _dns_record("add", req.subdomain, req.domain)

    # Generate service token
    service_token = enrollment.generate_service_token(service_id)
    public_url = os.environ.get("PUBLIC_URL", "http://localhost:7778")

    db.audit("system:registry", "service.register", service_id,
             {"device": req.device_id, "port": req.port, "slice_port": slice_port,
              "subdomain": req.subdomain, "forward": forward_status,
              "caddy": caddy_status, "dns": dns_status})

    # Verify the route is actually reachable end-to-end
    verify_status = None
    if external_url:
        verify_status = _verify_route(external_url, req.health_endpoint)

    event_bus.emit("service.registered", service_id,
                   {"service_id": service_id, "port": req.port, "slice_port": slice_port,
                    "external_url": external_url, "reachable": verify_status.get("reachable") if verify_status else None},
                   scope=f"device:{req.device_id}")

    return {
        "status": "registered",
        "service_id": service_id,
        "port": req.port,
        "slice_port": slice_port,
        "external_url": external_url,
        "service_token": service_token,
        "guide_url": f"{public_url}/api/v1/services/{service_id}/guide",
        "route": {
            "forward": forward_status,
            "caddy": caddy_status,
            "dns": dns_status,
            "verified": verify_status,
        },
    }


# --- Ports ---

@app.get("/api/v1/ports")
def list_ports(type: str = None, auth: AuthContext = Depends(get_auth)):
    return port_alloc.list_all(type)


@app.post("/api/v1/ports/allocate")
def allocate_port(req: PortAllocate, auth: AuthContext = Depends(get_auth)):
    """A port for a device you manage (`owner_id` is the device, or `<device>/<service>`);
    any other owner is the admin's. Scope v1 train 0: this took no bearer before."""
    root = (req.owner_id or "").split("/", 1)[0]
    if db.query_one("SELECT 1 FROM devices WHERE id = ?", (root,)):
        _require_managed_device(auth, root)
    elif not auth.is_admin():
        raise HTTPException(404, f"Device {root} not found")
    try:
        port = port_alloc.allocate(req.port_type, req.owner_id, req.preferred)
        return {"port": port, "type": req.port_type, "owner": req.owner_id}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))


@app.get("/api/v1/ports/check/{port}")
def check_port(port: int, auth: AuthContext = Depends(get_auth)):
    allocated = db.query("SELECT * FROM ports WHERE port = ?", (port,))
    in_use = port_alloc._port_in_use(port)
    return {"port": port, "allocated": allocated, "in_use_lsof": in_use}


@app.delete("/api/v1/ports/{port}")
def release_port(port: int, type: str = None, auth: AuthContext = Depends(require_admin)):
    """Release a port allocation back to the pool (admin). Reclaims orphaned
    reservations — e.g. when a device's tunnel was re-pointed at a shared one
    and its old auto-allocated port is now dangling. `type` optional if the
    port is unambiguous in the ledger."""
    rows = db.query("SELECT port, type, owner_id FROM ports WHERE port = ?", (port,))
    if not rows:
        raise HTTPException(404, f"Port {port} is not allocated")
    if type is None:
        if len(rows) > 1:
            types = sorted(r["type"] for r in rows)
            raise HTTPException(400, f"Port {port} allocated for multiple types {types}; specify ?type=")
        type = rows[0]["type"]
    elif not any(r["type"] == type for r in rows):
        raise HTTPException(404, f"Port {port} is not allocated for type '{type}'")
    released = next(r for r in rows if r["type"] == type)
    port_alloc.release(port, type)
    db.audit(auth.owner_id, "port.released", released.get("owner_id"),
             {"port": port, "type": type, "prev_owner": released.get("owner_id")})
    return {"status": "released", "port": port, "type": type, "prev_owner": released.get("owner_id")}


# --- Routes ---

@app.get("/api/v1/routes")
def list_routes(auth: AuthContext = Depends(get_auth)):
    routes = db.query("SELECT * FROM routes ORDER BY domain, subdomain")
    # Scope filtering: a non-admin sees only routes for visible services
    if True:
        visible = _visible_device_ids(auth)
        if visible is not None:
            # Build set of service_ids on visible devices
            visible_service_ids = set()
            for row in db.query("SELECT id FROM services"):
                device_id = row["id"].split("/")[0] if "/" in row["id"] else None
                if device_id and device_id in visible:
                    visible_service_ids.add(row["id"])
            routes = [r for r in routes if r.get("service_id") is None or r["service_id"] in visible_service_ids]
    return routes


@app.post("/api/v1/routes")
def create_route(req: RouteCreate, auth: AuthContext = Depends(get_auth)):
    """A route for a service you manage (the service's device); a route with no
    service is the admin's. Scope v1 train 0: this took no bearer before."""
    if req.service_id:
        svc = _require_visible_service(auth, req.service_id)
        if not auth.can_manage_device(svc.get("device_id") or ""):
            raise HTTPException(403, f"You can read {req.service_id} but not manage its device")
    elif not auth.is_admin():
        raise HTTPException(403, "A route with no service is the admin's to create")
    existing = db.query_one(
        "SELECT * FROM routes WHERE subdomain = ? AND domain = ?",
        (req.subdomain, req.domain),
    )
    if existing:
        raise HTTPException(409, f"Route {req.subdomain}.{req.domain} already exists")

    db.execute("""
        INSERT INTO routes (subdomain, domain, target, service_id)
        VALUES (?, ?, ?, ?)
    """, (req.subdomain, req.domain, req.target, req.service_id))

    return {"status": "created", "route": f"{req.subdomain}.{req.domain}", "target": req.target}


@app.post("/api/v1/routes/{subdomain}/activate")
def activate_route(subdomain: str, domain: Optional[str] = None, auth: AuthContext = Depends(get_auth)):
    """Activate a route: set up port forward + Caddy route + DNS. Requires admin or service scope."""
    domain = domain or _service_domain()
    route = db.query_one(
        "SELECT * FROM routes WHERE subdomain = ? AND domain = ?",
        (subdomain, domain),
    )
    if not route:
        raise HTTPException(404, f"Route {subdomain}.{domain} not found in registry")

    if route.get("service_id"):
        _require_service_manager(auth, route["service_id"])
    elif not auth.is_admin():
        raise HTTPException(403, "Only an administrator can activate a route with no service")

    result = {}

    # Set up port forward if this route has a service with a tunnel
    if route.get("service_id"):
        svc = db.query_one("SELECT * FROM services WHERE id = ?", (route["service_id"],))
        if svc and svc.get("slice_port"):
            device = db.query_one("SELECT * FROM devices WHERE id = ?", (svc["device_id"],))
            if device and device.get("tunnel_port"):
                ok, msg = forward.start_forward(
                    slice_port=svc["slice_port"], service_port=svc["port"],
                    tunnel_port=device["tunnel_port"], service_id=svc["id"],
                    device_ssh_user=device.get("outbound_ssh_user"))
                result["forward"] = {"success": ok, "message": msg}

    # Push Caddy route
    try:
        success, msg = caddy.append_route(subdomain, domain, route["target"])
        result["caddy"] = {"success": success, "message": msg}
    except Exception as e:
        result["caddy"] = {"success": False, "message": str(e)}

    # Create/retry DNS record
    result["dns"] = _dns_record("add", subdomain, domain)

    return {"status": "active", **result}


@app.post("/api/v1/routes/activate-all")
def activate_all_routes(auth: AuthContext = Depends(require_admin)):
    """Re-activate all routes: bind forwards + push Caddy. Admin only."""
    routes = db.query("SELECT * FROM routes ORDER BY subdomain")
    results = {}
    for route in routes:
        subdomain = route["subdomain"]
        domain = route["domain"]
        entry = {}

        # Forward
        if route.get("service_id"):
            svc = db.query_one("SELECT * FROM services WHERE id = ?", (route["service_id"],))
            if svc and svc.get("slice_port"):
                device = db.query_one("SELECT * FROM devices WHERE id = ?", (svc["device_id"],))
                if device and device.get("tunnel_port"):
                    ok, msg = forward.start_forward(
                        slice_port=svc["slice_port"], service_port=svc["port"],
                        tunnel_port=device["tunnel_port"], service_id=svc["id"],
                        device_ssh_user=device.get("outbound_ssh_user"))
                    entry["forward"] = {"success": ok, "message": msg}

        # Caddy
        try:
            ok, msg = caddy.append_route(subdomain, domain, route["target"])
            entry["caddy"] = {"success": ok, "message": msg}
        except Exception as e:
            entry["caddy"] = {"success": False, "message": str(e)}

        results[f"{subdomain}.{domain}"] = entry

    return {"status": "activated", "routes": results}


# --- Tickets ---

def _validate_ticket_addressing(service_id: Optional[str],
                                device_id: Optional[str],
                                assignee: Optional[str]) -> None:
    """Soft-validate addressing fields before write.

    Returns HTTPException(400) with a useful message instead of letting
    a sqlite FK violation bubble up as a bare 500. assignee has no FK in
    the schema, but we still validate against the users table to catch
    typos symmetrically with service_id/device_id.
    """
    if service_id:
        if not db.query_one("SELECT id FROM services WHERE id = ?", (service_id,)):
            raise HTTPException(400,
                f"Unknown service_id '{service_id}'. Must reference a registered service "
                f"(GET /api/v1/services). Leave null if the affected service isn't in the registry.")
    if device_id:
        if not db.query_one("SELECT id FROM devices WHERE id = ?", (device_id,)):
            raise HTTPException(400,
                f"Unknown device_id '{device_id}'. Must reference a registered device "
                f"(GET /api/v1/devices).")
    if assignee:
        if not db.query_one("SELECT id FROM users WHERE username = ?", (assignee,)):
            raise HTTPException(400,
                f"Unknown assignee '{assignee}'. Must be a username from "
                f"GET /api/v1/users. Leave null if you don't know the owner.")


def _identity_can_see_ticket(identity: str, ticket: dict) -> bool:
    """Row-level mirror of _can_see_ticket for notification fan-out, by
    username (the doc plane has had this since #500): the filer, a member of
    the ticket's group, or an admin. Under rail 3.9 a mention births an
    instance, so a recipient that cannot open the ticket must not receive it
    (prod #632 → #637). Fail closed on unknown identities."""
    return groupscope.identity_object_readable(identity, ticket, owner_cols=("source",))


def _can_see_ticket(auth: AuthContext, ticket: dict) -> bool:
    """Scope v1 (doc 261): the admin, the filer, or a member of the ticket's
    group. A device or service token also sees the tickets of its own device.
    The unions this once carried — the assignee, the assignee's group-mates
    (#255), the service's creator, the unaddressed commons (#339) — are gone:
    an assignee must be a member, so membership covers every one of them, and
    nothing unscoped is shared."""
    if groupscope.object_readable(auth, ticket, owner_cols=("source",)):
        return True
    if not auth.owner_id.startswith("user:"):
        visible = _visible_device_ids(auth) or set()
        dev = ticket.get("device_id") or ((ticket.get("service_id") or "").split("/", 1)[0] if ticket.get("service_id") else None)
        if dev and dev in visible:
            return True
    return False


# What happens next after touching a ticket, in the shape of every other
# message on the plane (doc 177 v2): what happened and where replies go.
# The mechanism is the skill's job (GET /api/v1/skills/samcloud-agent),
# not a paragraph on every write — the 3.8 version of this hint taught
# watch loops and claim releases to agents whose rail had neither.
_WATCH_HINT = ("In the object's conversation audience. Read your inbox: "
               "GET /api/v1/notifications?unread=true. Explicitly subscribe for record changes.")

# Returned instead of _WATCH_HINT when another LIVE conversation of the same
# identity is already on this thread (the thread record / a claim of another
# session). The write succeeded; replies land in that conversation too.
_HELD_HINT = ("Posted. Another conversation of yours is on this thread; replies route there, not here.")

# ── Thread claims (#199 — the full form of live-session routing) ───────────
# A claim marks "a warm surface of this identity is engaged with this thread."
# Writing to a ticket claims it; a held watch refreshes all your claims;
# releasing (or TTL expiry, if the surface died) hands the thread back to the
# cold rail. The rail's wake predicate is per-thread (unclaimed unread only),
# so a live chat owns its threads while cold sessions work everything else
# concurrently — no global deferral in either direction.

CLAIM_TTL_WRITE = 600          # writing to a thread claims it for 10 min

# Operator wall-clock (#384): agents everywhere reason about "morning/EOD/
# overnight" in the HUMAN OPERATOR's local time, never host/UTC. Interim
# single-operator model — one env-set zone; multi-operator = per-user field
# later. Threaded into auth/verify, users/me/guide, and the served helpers
# (__OPERATOR_NOW__/__OPERATOR_TZ__ substitution at serve time).
OPERATOR_TZ = os.environ.get("OPERATOR_TZ", "").strip() or "UTC"


def _operator_now() -> dict:
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(OPERATOR_TZ))
        return {"tz": OPERATOR_TZ, "iso": now.isoformat(),
                "human": now.strftime("%A %H:%M")}
    except Exception:
        return {"tz": OPERATOR_TZ, "iso": None, "human": None}
CLAIM_TTL_MAX = 3600


def _session_of(x_session_id: Optional[str]) -> Optional[str]:
    """Normalize a caller-presented session nonce (#264). Opaque, capped;
    None when the caller doesn't participate (legacy behavior throughout)."""
    sid = (x_session_id or "").strip()
    return sid[:64] or None


def _replay_guard(table: str, thread_col: str, thread_id, body: str) -> None:
    """Comment replay tripwire (v0.7.71, after the #363 fossil: one stale
    scratch-file payload posted 4x under 3 identities across 3 hours). An
    identical LONG body re-posted on the SAME thread within 24h is, in this
    fleet, always a stale-payload replay — never a legitimate comment. Short
    bodies (< 200 chars) stay free: convention phrases ("picked up, working")
    legitimately repeat. 409 names the original so the caller can self-diagnose
    its write path (see field-gotchas doc: stdin pipe, no file in the loop)."""
    if len(body) < 200:
        return
    row = db.query_one(
        f"SELECT id, author FROM {table} WHERE {thread_col} = ? AND body = ? "
        f"AND created_at > datetime('now', '-1 day') ORDER BY id DESC LIMIT 1",
        (thread_id, body))
    if row:
        raise HTTPException(409,
            f"replay-guard: this exact body was already posted on this thread by "
            f"{row['author']} (comment {row['id']}, last 24h). If intentional, "
            f"change the text; if not, your write path re-read a stale payload — "
            f"see the field-gotchas doc (stdin pipe, no file in the write loop).")


def _session_fp(session_id: Optional[str]) -> Optional[str]:
    """Public fingerprint of a session nonce (#264): what reads expose so
    surfaces are tellable apart WITHOUT leaking the raw nonce (which acts
    as the claim key and must not be replayable off a ticket read)."""
    if not session_id:
        return None
    return hashlib.sha256(session_id.encode()).hexdigest()[:8]


def _remember_origin(identity: str, session_id: Optional[str], tid: Optional[int]):
    """First birth/create wins. Cross-thread writes never rename a conversation."""
    if session_id and tid:
        db.execute("INSERT OR IGNORE INTO conversation_origins "
                   "(identity, conversation_id, home_thread_id) VALUES (?, ?, ?)",
                   (identity, session_id[:64], tid))


def _writer_provenance(identity: str, session_id: Optional[str]) -> dict:
    """Snapshot known provenance; no inferred conversation for unstamped writes."""
    sid = (session_id or "")[:64]
    home = None
    if sid:
        row = db.query_one("SELECT home_thread_id FROM conversation_origins "
                           "WHERE identity = ? AND conversation_id = ?", (identity, sid))
        if not row:
            # Existing conversations: use a recorded create-home, or an
            # unambiguous rail birth. Never use the object being commented on.
            row = db.query_one("SELECT thread_id AS home_thread_id FROM conversation_threads "
                               "WHERE identity = ? AND conversation_id = ?", (identity, sid))
            if not row:
                births = db.query("SELECT DISTINCT thread_id FROM threads WHERE identity = ? "
                                  f"AND conversation_id = ? AND holder IN ({_RAIL_HOLDERS_SQL}) "
                                  "AND thread_id IS NOT NULL", (identity, sid))
                if len(births) == 1:
                    row = {"home_thread_id": births[0]["thread_id"]}
            if row:
                _remember_origin(identity, sid, row["home_thread_id"])
        home = row["home_thread_id"] if row else None
    return {"session_fp": _session_fp(sid), "home_thread_id": home}


def _comment_payload(kind: str, payload: dict) -> dict:
    """All notification paths, including mentions, use the stored author stamp."""
    cid = payload.get("comment_id")
    if cid is None:
        return payload
    row = db.query_one(f"SELECT session_fp, home_thread_id FROM {kind}_comments WHERE id = ?", (cid,))
    return {**payload, "actor_session": row["session_fp"],
            "actor_home_thread_id": row["home_thread_id"]} if row else payload


def _sibling_notification(kind: str, oid: int, actor: str, payload: dict) -> Optional[dict]:
    """Only an existing *different* binding may receive an identity's own comment.

    Caller has already established object audience and read access. Pin the
    target at write time so reconnect/replacement cannot turn this into a wake.
    Unknown writer/target means no self notification, never identity broadcast.
    """
    fp = payload.get("actor_session")
    if not fp or payload.get("comment_id") is None:
        return None
    tid = _thread_of(kind, oid)
    rows = db.query("SELECT conversation_id FROM threads WHERE identity = ? AND thread_id = ? "
                    "AND state IN ('open','closing')", (actor, tid))
    targets = {_session_fp(r["conversation_id"]) for r in rows if r["conversation_id"]}
    targets.discard(fp)
    if len(targets) != 1:
        return None
    return {**payload, "recipient_session": targets.pop()}


def _claim_cols(ticket_id=None, doc_id=None, task_id=None) -> tuple[str, int]:
    """Resolve which thread plane a claim addresses (#392): exactly one of
    ticket/doc/task per claim row. Column name comes from this fixed set,
    never caller input."""
    set_ids = [(c, v) for c, v in (("ticket_id", ticket_id), ("doc_id", doc_id),
                                   ("task_id", task_id)) if v is not None]
    if len(set_ids) != 1:
        raise ValueError("claim needs exactly one of ticket_id/doc_id/task_id")
    return set_ids[0]


def _claim_thread(identity: str, ticket_id: Optional[int] = None,
                  ttl: int = CLAIM_TTL_WRITE, sticky: bool = False,
                  session_id: Optional[str] = None,
                  doc_id: Optional[int] = None,
                  task_id: Optional[int] = None) -> dict:
    """Upsert a claim, extend-only (never shorten a longer live claim).

    #392: the thread may be a ticket, doc, or task — same machinery, wider
    predicate. Doc revisions/appends/comments and task PATCHes/comments claim
    their thread exactly like ticket writes claim theirs.

    sticky (#227/#229): explicit engagement (POST /tickets/{id}/claim or a
    subscription re-assert) — survives identity-wide non-sticky releases;
    falls only by TTL, targeted release, or include_sticky=true. Stickiness
    is one-way here (a later auto-claim from a write never demotes it).

    session_id (#264): claims carry WHICH live surface owns the thread, not
    just which identity. First live claimer wins; a different session's
    claim attempt on a LIVE session-owned row is refused (owned=False) —
    it neither steals nor extends. A session-stamped row whose TTL expired
    is taken over outright (a dead surface doesn't keep threads). Rows with
    NULL session (legacy writers) keep today's shared-row semantics and are
    adopted by the first session that presents a nonce.

    Returns {"owned": bool, "holder_session": str|None}."""
    col, thread_id = _claim_cols(ticket_id, doc_id, task_id)
    now = datetime.now(timezone.utc).isoformat()
    until = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
    existing = db.query_one(
        f"SELECT id, claimed_until, session_id, sticky FROM thread_claims "
        f"WHERE identity = ? AND {col} = ?",
        (identity, thread_id))
    if existing:
        holder = existing.get("session_id")
        live = (existing.get("claimed_until") or "") > now
        if live and holder and holder != session_id:
            # Another live session of this identity owns the thread — refuse
            # (this covers session_id=None too: a legacy writer can't dilute
            # a session-owned claim).
            return {"owned": False, "holder_session": holder}
        if not live and holder and holder != session_id:
            # Expired session-owned row: takeover resets the row wholesale —
            # the new surface's stickiness, the new surface's session.
            db.execute("UPDATE thread_claims SET claimed_until = ?, sticky = ?, "
                       "session_id = ?, updated_at = datetime('now') WHERE id = ?",
                       (until, 1 if sticky else 0, session_id, existing["id"]))
            return {"owned": True, "holder_session": session_id}
        new_until = until if (existing.get("claimed_until") or "") < until \
            else existing["claimed_until"]
        db.execute("UPDATE thread_claims SET claimed_until = ?, "
                   "sticky = MAX(COALESCE(sticky, 0), ?), "
                   "session_id = COALESCE(?, session_id), "
                   "updated_at = datetime('now') WHERE id = ?",
                   (new_until, 1 if sticky else 0, session_id, existing["id"]))
        return {"owned": True, "holder_session": session_id or holder}
    db.execute(f"INSERT INTO thread_claims (identity, {col}, claimed_until, sticky, "
               f"session_id) VALUES (?, ?, ?, ?, ?)",
               (identity, thread_id, until, 1 if sticky else 0, session_id))
    return {"owned": True, "holder_session": session_id}


def _refresh_claims(identity: str, ttl: int, session_id: Optional[str] = None):
    """Extend all UNEXPIRED claims (an expired claim needs a fresh write to
    resurrect — a dead surface doesn't get to keep threads by accident).

    #264: a watch only keeps alive the claims its OWN session holds (plus
    legacy NULL-session rows). Another live session's claims decay on their
    own TTL unless that session is itself watching — so a dead surface's
    exclusive claims release naturally."""
    now = datetime.now(timezone.utc).isoformat()
    until = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
    db.execute("UPDATE thread_claims SET claimed_until = ?, updated_at = datetime('now') "
               "WHERE identity = ? AND claimed_until > ? AND claimed_until < ? "
               "AND (session_id IS NULL OR session_id = ?)",
               (until, identity, now, until, session_id))


def _release_claims(identity: str, ticket_id: Optional[int] = None,
                    include_sticky: bool = False,
                    session_id: Optional[str] = None,
                    doc_id: Optional[int] = None,
                    task_id: Optional[int] = None) -> int:
    for col, val in (("ticket_id", ticket_id), ("doc_id", doc_id),
                     ("task_id", task_id)):
        if val is not None:
            # Targeted release is always explicit — sticky or not, any
            # session's or not, it goes. This is also the #264 takeover path.
            return db.execute_count(
                f"DELETE FROM thread_claims WHERE identity = ? AND {col} = ?",
                (identity, val))
    # Identity-wide releases only touch rows this surface can vouch for:
    # its own session's rows + legacy NULL rows (#264). Another live
    # session's stamped claims are never clobbered by someone else's exit.
    scope_sql = "AND (session_id IS NULL OR session_id = ?)"
    if include_sticky:
        return db.execute_count(f"DELETE FROM thread_claims WHERE identity = ? {scope_sql}",
                          (identity, session_id))
    # Identity-wide default spares sticky claims (#229): wake-exit cleanup
    # must not clobber a parallel live surface's asserted engagement.
    return db.execute_count(f"DELETE FROM thread_claims WHERE identity = ? "
                      f"AND COALESCE(sticky, 0) = 0 {scope_sql}",
                      (identity, session_id))


# ── Who else is in this row (#529) ──────────────────────────────────────────
# Claims were single-identity by construction: every predicate carried
# `identity = ?`, so two identities were never compared, never refused and
# never told. Three identities held ticket 526 and two held task 490
# simultaneously and no seat could have known — including from the endpoint
# built to answer exactly that ("?all=true" narrowed silently for non-admins,
# returning a self-view shaped identically to the fleet view it was asked for).
#
# What this is NOT, and the wire says so on every read:
#   - not a lock. Nothing cross-identity is enforced, and concurrent work is
#     frequently legitimate — 3 of doc 31's 4 crossings were. Exclusion here
#     would have blocked work that needed doing.
#   - not an audit log. Rows are CLAIM_TTL_WRITE wide (10 min), extend-only,
#     and a session DELETEs its own on exit. Absence proves nothing.

_CLAIM_ADVISORY = (
    "Advisory, not a lock: cross-identity claims are never enforced, and "
    "concurrent work on one row is often legitimate. Live hint, not an audit "
    "trail — claims are ~10 min wide and a session deletes its own on exit, "
    "so an empty holders list does NOT mean nobody is working this row.")


def _thread_holders(ticket_id: Optional[int] = None, doc_id: Optional[int] = None,
                    task_id: Optional[int] = None,
                    me: Optional[str] = None) -> list[dict]:
    """Every LIVE claim on ONE thread, across all identities (#529).

    The one query the claims table could never answer before. Projection is
    unchanged from what plain seats already receive on GET /wake/claims —
    identity, `session` FINGERPRINT (never the nonce, which is the claim key),
    claimed_until — so this needs no new disclosure decision, only a different
    row set. `self` is marked explicitly: an identity column that always says
    *you* is what made the old self-view read as fleet-wide."""
    col, thread_id = _claim_cols(ticket_id, doc_id, task_id)
    now = datetime.now(timezone.utc).isoformat()
    rows = db.query(
        f"SELECT identity, session_id, claimed_until, sticky, created_at, updated_at "
        f"FROM thread_claims WHERE {col} = ? AND claimed_until > ? "
        f"ORDER BY claimed_until DESC", (thread_id, now))
    return [{"identity": r["identity"],
             "session": _session_fp(r.get("session_id")),
             "claimed_until": r["claimed_until"],
             "sticky": bool(r.get("sticky")),
             "since": r.get("created_at"),
             "last_write": r.get("updated_at"),
             "self": bool(me) and r["identity"] == me}
            for r in rows]


def _claims_view(me: Optional[str], ticket_id: Optional[int] = None,
                 doc_id: Optional[int] = None,
                 task_id: Optional[int] = None) -> dict:
    """The holder hint carried on GET /tickets|/docs|/tasks/{id} (#529) — the
    read a composing seat actually performs, so the answer arrives without
    having to know a second endpoint exists. Always present (a missing key
    would be indistinguishable from an old server); `advisory` and `note`
    travel with it because a reader who sees a claim surfaced will otherwise
    assume it was enforced."""
    return {"advisory": True,
            "holders": _thread_holders(ticket_id=ticket_id, doc_id=doc_id,
                                       task_id=task_id, me=me),
            "note": _CLAIM_ADVISORY}


_THREAD_PLANES = {"ticket": "ticket_id", "doc": "doc_id", "task": "task_id"}
_THREAD_STATES = ("open", "closing", "closed")
# A holder the rail made. Three predicates below turn on exactly this class —
# whose creates join their thread, who may not bind a second one by writing on
# it — and all three used to spell it out in SQL. Adding `adapter` (rail 3.12.0,
# a seat whose runtime is not a Claude harness) meant finding all three: the
# first one missed rejected every binding such a seat posted, with a 400 the
# rail logs as non-fatal and carries on without. One tuple, quoted into the SQL.
_RAIL_HOLDERS = ("stream", "tmux", "oneshot", "adapter")
_RAIL_HOLDERS_SQL = ",".join(f"'{h}'" for h in _RAIL_HOLDERS)
_THREAD_HOLDERS = _RAIL_HOLDERS + ("conversation",)


def _require_thread_visible(plane: str, tid: int, auth: "AuthContext") -> None:
    """404/403 unless the caller can read the object a thread hangs off.
    Learning who holds a row you cannot read would be a new disclosure, and
    neither the claims view (#529) nor the threads record makes one."""
    if plane == "ticket":
        row = db.query_one("SELECT * FROM tickets WHERE id = ?", (tid,))
        if not row:
            raise HTTPException(404, f"Ticket {tid} not found")
        if not _can_see_ticket(auth, row):
            raise HTTPException(404, "Ticket not found")
    elif plane == "doc":
        row = db.query_one("SELECT * FROM docs WHERE id = ?", (tid,))
        if not row:
            raise HTTPException(404, f"Doc {tid} not found")
        if not _can_see_doc(auth, row):
            raise HTTPException(404, "Doc not found")
    else:
        _load_task_and_project(tid, auth)   # raises 404/403 itself


def _upsert_thread(kind: str, object_id: int, identity: str,
                   conversation_id: str, host: Optional[str] = None,
                   holder: str = "stream", pid: Optional[int] = None,
                   state: str = "open", rail_version: Optional[str] = None) -> dict:
    """The thread binding (doc 174): one row per (kind, object, identity).
    Authoritative writer is the rail. The row is the CURRENT state of the
    binding, not its history: `closed` stamps closed_at, a reopen clears it.
    `opened_at` is the conversation's open time and moves only when the
    conversation does — a resume or a one-shot on the same conversation id
    keeps it (staging #100: a one-shot rewrote it twice and the thread's
    real open time survived nowhere on the plane)."""
    now = datetime.now(timezone.utc).isoformat()
    # v19 (doc 239): the binding is per universal thread. The row is keyed on
    # the thread's first member so one identity holds one conversation per
    # thread whatever member the write landed on.
    wt = _thread_of(kind, object_id)
    if wt:
        kind, object_id = _thread_root(wt)
    existing = db.query_one(
        "SELECT * FROM threads WHERE kind = ? AND object_id = ? AND identity = ?",
        (kind, object_id, identity))
    # No twin. A rail holder may not take a thread a live conversation of the
    # same identity is already holding — and the reason this needs saying at
    # all is that the row is UNIQUE(kind, object_id, identity), so the UPDATE
    # below does not fail on the conflict, it RESOLVES it: the person's
    # binding silently becomes the holder's. That is not a lost write, it is
    # the evidence being destroyed. `_bind_conversation` then declines to
    # re-bind while a non-closed row exists, and the rail's own gate
    # (`_held_by_surface`) finds no conversation row to see. One twin makes
    # every later twin possible on that object, permanently: three of them
    # were once resident under the admin seat for 21-23 hours,
    # one of them answering on a thread the person was typing in.
    #
    # The escape hatch is the surface's own: a conversation that has said
    # `{"ended": true}`, or never registered a surface at all, is not a live
    # receiver and holds nothing. Say goodbye and a holder may be born.
    if (existing and holder in _RAIL_HOLDERS
            and existing["holder"] == "conversation"
            and existing["state"] in ("open", "closing")
            and existing["conversation_id"] != conversation_id
            and db.query_one("SELECT 1 FROM surfaces WHERE identity = ? AND "
                             "conversation_id = ? AND ended_at IS NULL",
                             (identity, existing["conversation_id"]))):
        raise HTTPException(409, (
            f"{identity} already holds {kind} {object_id} in conversation "
            f"{_session_fp(existing['conversation_id'])} (a registered surface). "
            f"A second holder for one identity on one thread is the twin this "
            f"refusal exists to prevent — do not birth one. The surface releases "
            f"it with POST /api/v1/wake/surfaces {{\"ended\": true}}, or a person "
            f"closes the thread."))
    # Resolve legacy provenance before changing the binding. A multi-thread
    # legacy holder has no known birth order; a heartbeat must not invent it.
    origin = _writer_provenance(identity, conversation_id) if holder in _RAIL_HOLDERS else None
    prior = db.query_one("SELECT 1 FROM threads WHERE identity = ? AND conversation_id = ?",
                         (identity, conversation_id)) if origin else None
    if existing:
        new_conv = existing["conversation_id"] != conversation_id
        db.execute(
            "UPDATE threads SET conversation_id = ?, host = COALESCE(?, host), "
            "holder = ?, pid = ?, state = ?, rail_version = COALESCE(?, rail_version), "
            "closed_at = CASE WHEN ? = 'closed' THEN COALESCE(closed_at, ?) ELSE NULL END, "
            "opened_at = CASE WHEN ? = 1 THEN ? ELSE opened_at END, updated_at = ?, "
            "thread_id = COALESCE(?, thread_id) "
            "WHERE id = ?",
            (conversation_id, host, holder, pid, state, rail_version,
             state, now, 1 if new_conv else 0, now, now, wt, existing["id"]))
        tid = existing["id"]
    else:
        tid = db.execute(
            "INSERT INTO threads (kind, object_id, identity, conversation_id, host, "
            "holder, pid, state, rail_version, opened_at, closed_at, updated_at, thread_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, object_id, identity, conversation_id, host, holder, pid, state,
             rail_version, now, now if state == "closed" else None, now, wt))
    if origin and (origin["home_thread_id"] is not None or not prior):
        _remember_origin(identity, conversation_id, wt)
    return db.query_one("SELECT * FROM threads WHERE id = ?", (tid,))



_THREAD_TABLES = {"ticket": ("tickets", "summary"), "doc": ("docs", "title"), "task": ("tasks", "title")}
_TERMINAL = {"ticket": ("resolved", "closed"), "task": ("done", "cancelled"), "doc": ("archived", "superseded")}


def _thread_of(kind: str, object_id: int, creator: Optional[str] = None) -> Optional[int]:
    """The universal thread an object belongs to (doc 239). An object with none
    starts one whose first member it is — lazily, so nothing has to be
    backfilled ahead of time and a row that predates v19 gets its thread the
    first time anything touches it."""
    spec = _THREAD_TABLES.get(kind)
    if not spec:
        return None
    tbl, title_col = spec
    row = db.query_one(f"SELECT thread_id, {title_col} AS title FROM {tbl} WHERE id = ?", (object_id,))
    if not row:
        return None
    if row["thread_id"]:
        return row["thread_id"]
    existing = db.query_one("SELECT id FROM work_threads WHERE root_kind = ? AND root_id = ?",
                            (kind, object_id))
    tid = existing["id"] if existing else db.execute(
        "INSERT INTO work_threads (title, root_kind, root_id, created_by) VALUES (?, ?, ?, ?)",
        (row["title"], kind, object_id, creator))
    db.execute(f"UPDATE {tbl} SET thread_id = ? WHERE id = ? AND thread_id IS NULL", (tid, object_id))
    return tid


def _thread_root(tid: int) -> Optional[tuple]:
    r = db.query_one("SELECT root_kind, root_id FROM work_threads WHERE id = ?", (tid,))
    return (r["root_kind"], r["root_id"]) if r else None


def _place_in_thread(kind: str, object_id: int, tid: int) -> None:
    """Make an object a member of a thread (a create inside a scope, or an
    explicit move). Its notification rows follow it from the next write."""
    spec = _THREAD_TABLES.get(kind)
    if spec and db.query_one("SELECT 1 FROM work_threads WHERE id = ?", (tid,)):
        db.execute(f"UPDATE {spec[0]} SET thread_id = ? WHERE id = ?", (tid, object_id))


def _held_thread(identity: str, session_id: Optional[str]) -> Optional[int]:
    """The one thread a rail holder's conversation holds (stream, tmux,
    one-shot, adapter) — a holder has exactly one. A person's surface is bound
    to what it wrote on (`conversation` rows), which is not holding: those
    bindings never decide where its creations go (0.12.1, doc 247 iteration 1:
    the admin's surface held one open binding — the previous round's run
    record — and the next round's first ticket joined that doc's thread)."""
    if not session_id:
        return None
    rows = db.query("SELECT DISTINCT thread_id FROM threads WHERE identity = ? AND conversation_id = ? "
                    f"AND holder IN ({_RAIL_HOLDERS_SQL}) "
                    "AND state IN ('open','closing') AND thread_id IS NOT NULL",
                    (identity, session_id[:64]))
    return rows[0]["thread_id"] if len(rows) == 1 else None


def _home_thread(identity: str, conversation_id: str) -> Optional[int]:
    """The conversation's own thread (#886) — '' is the identity's. A home
    whose thread a person closed is reopened by the next thing made in it: the
    conversation is still working there. A home whose thread is gone is
    forgotten, and the next create starts it again."""
    row = db.query_one("SELECT c.thread_id, w.state FROM conversation_threads c "
                       "LEFT JOIN work_threads w ON w.id = c.thread_id "
                       "WHERE c.identity = ? AND c.conversation_id = ?", (identity, conversation_id))
    if not row and conversation_id:
        # No home recorded, but the conversation may already have started a
        # thread: homes are written from a conversation's first create AFTER
        # 0.12.65, and a conversation live before that started threads under
        # the old rule (each create its own). Its home is the open thread it
        # started last — the one it is currently working — not a new one. On
        # 2026-10-07 the admin's own session, on thread 1775 since 10-05,
        # filed its overview ticket and got thread 1779 (#893): one new
        # thread per create, which is wrong.
        started = db.query_one(
            "SELECT w.id AS thread_id FROM work_threads w JOIN threads t "
            "ON t.kind = w.root_kind AND t.object_id = w.root_id AND t.identity = ? "
            "WHERE t.conversation_id = ? AND t.holder = 'conversation' "
            "AND w.created_by = ? AND w.state = 'open' "
            "ORDER BY w.created_at DESC, w.id DESC LIMIT 1",
            (identity, conversation_id, identity))
        if started:
            db.execute("INSERT OR IGNORE INTO conversation_threads (identity, conversation_id, thread_id) "
                       "VALUES (?, ?, ?)", (identity, conversation_id, started["thread_id"]))
            db.audit(identity, "thread.home", str(started["thread_id"]),
                     {"by": "the thread this conversation started before homes were recorded"})
            return started["thread_id"]
    if not row:
        return None
    if row["state"] is None:
        db.execute("DELETE FROM conversation_threads WHERE identity = ? AND conversation_id = ?",
                   (identity, conversation_id))
        return None
    if row["state"] == "closed":
        db.execute("UPDATE work_threads SET state = 'open', closed_at = NULL WHERE id = ?", (row["thread_id"],))
        db.audit(identity, "thread.open", str(row["thread_id"]), {"by": "a create in its home conversation"})
    return row["thread_id"]


def _create_in_thread(kind: str, object_id: int, identity: str, session_id: Optional[str],
                      explicit: Optional[str] = None, parent_task_id: Optional[int] = None) -> int:
    """Put a new object in a thread (#886). Every create joins one; none
    starts a thread except a conversation's first. In order:

    1. `X-Thread-Id` — an existing thread named on purpose (400 if it is not one).
    2. the parent task's thread, for a subtask.
    3. the thread a rail holder's conversation holds.
    4. the conversation's own thread — `X-Session-Id`'s home.
    5. the identity's own thread, when the write names no conversation.

    When 4 or 5 has no thread yet, this object starts it and becomes its root,
    and it is that conversation's home from then on. There is no flag that
    starts another: a create that strays into a new thread births a holder on
    every seat it touches, which is how one interactive session fanned three
    threads and four residents across two seats on 2026-10-05 (#886).

    Never inferred: a write that names no conversation is the identity's, not
    whatever happened to be running on the seat (doc 239 D12, doc 265 §2)."""
    if explicit is not None and str(explicit).strip():
        raw = str(explicit).strip()
        if not raw.isdigit() or not db.query_one("SELECT 1 FROM work_threads WHERE id = ?", (int(raw),)):
            raise HTTPException(400, f"X-Thread-Id {raw!r} is not a thread id (GET /api/v1/threads)")
        tid = int(raw)
    else:
        tid = _thread_of("task", parent_task_id) if parent_task_id else None
        tid = tid or _held_thread(identity, session_id)
        conv = (session_id or "")[:64]
        tid = tid or _home_thread(identity, conv)
        if not tid:
            tid = _thread_of(kind, object_id, identity)
            db.execute("INSERT OR IGNORE INTO conversation_threads (identity, conversation_id, thread_id) "
                       "VALUES (?, ?, ?)", (identity, conv, tid))
            _remember_origin(identity, session_id, tid)
            return tid
    _place_in_thread(kind, object_id, tid)
    _remember_origin(identity, session_id, _writer_provenance(identity, session_id)["home_thread_id"] or tid)
    return tid


def _reached_for_comment(col: str, object_id: int, actor: str, comment_id) -> set:
    """Who already holds a row for THIS comment, whatever the row's event type.
    One comment is one message: the mention row (`*.mentioned`, with the tail)
    and the comment row (`*.commented`) must never both reach one reader.
    Until 0.12.0 the thread fan-out deduped by event type alone, so an
    identity that was mentioned AND bound to the thread got the comment
    twice — once quoted, once bare — and two instances spent six messages
    measuring the "phantom duplicate" the rail had handed them (doc 246 D1)."""
    if comment_id is None:
        return set()
    return {r["recipient"] for r in db.query(
        f"SELECT recipient FROM notifications WHERE {col} = ? AND actor = ? "
        f"AND json_extract(payload, '$.comment_id') = ? "
        f"AND created_at >= datetime('now', '-60 seconds')", (object_id, actor, comment_id))}


def _root_payload(kind: str, object_id: int) -> Optional[dict]:
    """Introduce the root by title and an authoritative read, not a body copy."""
    tid = _thread_of(kind, object_id)
    root = _thread_root(tid) if tid else None
    if not root:
        return None
    rk, rid = root
    table, title = _THREAD_TABLES[rk]
    row = db.query_one(f"SELECT id, {title} AS title FROM {table} WHERE id = ?", (rid,))
    if not row:
        return None
    return {"kind": rk, "id": rid, "title": row["title"] or "",
            "read": f"GET /api/v1/{_THREAD_TABLES[rk][0]}/{rid}"}


def _object_row(kind: str, object_id: int):
    """The row an event is about — a task's readability is its project's."""
    if kind == "ticket":
        return db.query_one("SELECT * FROM tickets WHERE id = ?", (object_id,))
    if kind == "doc":
        return db.query_one("SELECT * FROM docs WHERE id = ?", (object_id,))
    if kind == "task":
        return db.query_one("SELECT p.* FROM projects p JOIN tasks t ON t.project_id = p.id WHERE t.id = ?",
                            (object_id,))
    return None


def _identity_can_read_object(identity: str, kind: str, row: dict) -> bool:
    if kind == "ticket":
        return _identity_can_see_ticket(identity, row)
    if kind == "doc":
        return _identity_can_see_doc(identity, row)
    return groupscope.identity_object_readable(identity, row, owner_cols=("source",))


# Thread-level delivery is gone (0.12.74, #902). From 0.11.0 to 0.12.73 every
# identity whose conversation was on a thread got a row for every event on every
# member of it — "an instance hears every write in its thread, on any member",
# a sentence the admin wrote into doc 239 about BINDING,
# which never said it. The thread is the wake and identity; delivery is object
# related: x's comment on a ticket doesn't go to z just because z is on the
# thread. A worker woken into an orchestrator's thread on one task was
# being handed the orchestrator's whole domain. So
# the object's own fan-out is the whole rule: ticket watchers (assignee, filer,
# named recipients and explicit/legacy subscribers), doc watchers, a task's assignee and
# the project's opt-in watchers. The thread decides where a create lands and
# who is woken; it delivers nothing by itself. `thread.closed` is the one
# thread-wide row left, written by a person's close (PATCH /threads/{id}).


def _thread_members(tid: int) -> dict:
    out = {}
    for kind, (tbl, title_col) in _THREAD_TABLES.items():
        st = "status"
        out[kind + "s"] = db.query(f"SELECT id, {title_col} AS title, {st} AS status FROM {tbl} "
                                   f"WHERE thread_id = ? ORDER BY id", (tid,))
    return out


def _member_visible(kind: str, object_id: int, auth: "AuthContext") -> bool:
    """Can this caller read one member of a thread? The object's own
    predicate, never the thread's root."""
    try:
        _require_thread_visible(kind, object_id, auth)
        return True
    except HTTPException:
        return False


def _thread_visible_members(tid: int, auth: "AuthContext") -> tuple:
    """(members as the caller may read them, how many are hidden). A thread
    has no scope; its objects do: no one reads threads, they read objects,
    and the thread only decides wake and routing. A member the caller cannot read is a
    bare ref — `{"id": N, "hidden": true}` — its title and status stay behind
    the object's own door."""
    members, hidden = _thread_members(tid), 0
    out = {}
    for key, ms in members.items():
        kind = key[:-1]
        shown = []
        for m in ms:
            if _member_visible(kind, m["id"], auth):
                shown.append(m)
            else:
                hidden += 1
                shown.append({"id": m["id"], "hidden": True})
        out[key] = shown
    return out, hidden


def _thread_public(tid: int, me: Optional[str], auth: "AuthContext" = None) -> Optional[dict]:
    """The thread as the caller may read it. With `auth`, members the caller
    cannot read are bare refs counted in `hidden_members`, and a root it
    cannot read is `root_hidden` with the title `thread N` (0.12.76).
    `open_members` is the thread's own fact and is computed over every
    member, visible or not: an instance's lifecycle must not depend on who
    is looking."""
    t = db.query_one("SELECT * FROM work_threads WHERE id = ?", (tid,))
    if not t:
        return None
    members = _thread_members(tid)
    shown, hidden, root_hidden = members, 0, False
    if auth is not None:
        shown, hidden = _thread_visible_members(tid, auth)
        root_hidden = not _member_visible(t["root_kind"], t["root_id"], auth)
    bindings = db.query("SELECT * FROM threads WHERE thread_id = ? ORDER BY updated_at DESC", (tid,))
    people, owners = set(), set()
    for r in db.query("SELECT assignee AS a, source AS s FROM tickets WHERE thread_id = ?", (tid,)):
        people.update(x for x in (r["a"], r["s"]) if x)
        if r["a"]:
            owners.add(r["a"])
    for r in db.query("SELECT assignee AS a, source AS s FROM tasks WHERE thread_id = ?", (tid,)):
        people.update(x for x in (r["a"], r["s"]) if x)
        if r["a"]:
            owners.add(r["a"])
    for r in db.query("SELECT source AS s, COALESCE(owner, source) AS o FROM docs WHERE thread_id = ?", (tid,)):
        people.update((r["s"], r["o"])); owners.add(r["o"])
    people.update(b["identity"] for b in bindings)
    # An instance lives while its thread has open WORK: tickets and tasks with
    # a non-terminal status. Docs are records, not work — they never hold a
    # thread open (a doc-only thread ends only when a person closes it).
    open_members = any(m["status"] not in _TERMINAL[k[:-1]]
                       for k, ms in members.items() if k != "docs" for m in ms)
    title = f"thread {t['id']}" if root_hidden else t["title"]
    return {"id": t["id"], "title": title, "state": t["state"],
            "root": f"{t['root_kind']}:{t['root_id']}", "root_hidden": root_hidden,
            "created_by": t["created_by"],
            "created_at": t["created_at"], "closed_at": t["closed_at"],
            "open_members": open_members, "members": shown, "hidden_members": hidden,
            "owners": sorted(owners),            # assignees of its tickets and tasks, authors of its docs
            "participants": sorted(people),
            "bindings": [_thread_row_public(b, me) for b in bindings]}


def _bind_conversation(kind: str, object_id: int, identity: str,
                       session_id: Optional[str]) -> None:
    """A write that carries a session nonce binds that conversation to the
    thread — but only when the identity has no OPEN instance on it already.
    The conversation that spoke first owns the thread (doc 174, finding F5:
    a lobby holder that answered a ticket was unroutable for the reply
    because nothing recorded that IT was the one who spoke). The rail's own
    upserts are authoritative and bypass this guard. No nonce, no binding."""
    if not session_id or kind not in _THREAD_PLANES:
        return
    wt = _thread_of(kind, object_id)
    if wt:
        kind, object_id = _thread_root(wt)
    # A holder belongs to one thread (doc 239): a conversation the rail runs
    # (stream/tmux/oneshot) never binds a second thread by writing on it — its
    # write there is a message to that thread's people. A person's session
    # (a `conversation` binding, or none yet) binds what it writes on: that is
    # how a person takes a thread.
    held_elsewhere = db.query_one(
        "SELECT 1 FROM threads WHERE identity = ? AND conversation_id = ? "
        f"AND holder IN ({_RAIL_HOLDERS_SQL}) "
        "AND state IN ('open','closing') AND NOT (kind = ? AND object_id = ?)",
        (identity, session_id[:64], kind, object_id))
    if held_elsewhere:
        return
    row = db.query_one(
        "SELECT state, conversation_id FROM threads WHERE kind = ? AND object_id = ? AND identity = ?",
        (kind, object_id, identity))
    if row and row["state"] != "closed":
        return
    if row and row["conversation_id"] == session_id[:64]:
        # The same conversation writing again (a one-shot's reply on its own
        # closed thread) is not a new binding: keep the row as it is, holder
        # included — `holder` is the record that this identity HELD the thread,
        # which the rail reads to keep former holders from answering each
        # other on a resolved object (staging #108).
        return
    _upsert_thread(kind, object_id, identity, session_id, holder="conversation")


def _thread_row_public(r: dict, me: Optional[str]) -> dict:
    """Fingerprint the conversation id for everyone but its owner. The owning
    identity gets the raw id back: the rail needs it to resume a thread after
    a seat loses its local cache, and under doc 174 claims no longer route,
    so the nonce is no longer a replayable key."""
    out = dict(r)
    raw = out.pop("conversation_id", None)
    out["conversation"] = _session_fp(raw)
    out["home_thread_id"] = _writer_provenance(r["identity"], raw)["home_thread_id"]
    if me and r.get("identity") == me:
        out["conversation_id"] = raw
    return out


def _parse_thread(spec: str) -> tuple[str, int]:
    """`ticket:526` / `doc:78` / `task:490` -> (plane, id). Malformed input is
    a 400, never a silently-ignored query param — the whole finding behind
    #529 is that an unrecognised narrowing returned a confident wrong answer
    (FastAPI drops unknown params, so `?thread=` was accepted and ignored)."""
    plane, _, raw = (spec or "").partition(":")
    if plane not in _THREAD_PLANES or not raw.isdigit():
        raise HTTPException(
            400, f"thread must be one of ticket:<id>, doc:<id>, task:<id> — got {spec!r}")
    return plane, int(raw)


def _subscribe_ticket(ticket_id: int, identity: str, explicit: bool = False,
                      updates: bool = False):
    """explicit=True (a deliberate POST /subscribe, an assignment, an @mention)
    clears any standing mute; creation enrolment respects it. Comments do not
    invoke this helper, so a one-off contribution leaves watch state unchanged."""
    if explicit:
        db.execute("DELETE FROM thread_mutes WHERE identity = ? AND ticket_id = ?",
                   (identity, ticket_id))
    elif db.query_one("SELECT 1 FROM thread_mutes WHERE identity = ? AND ticket_id = ?",
                      (identity, ticket_id)):
        return
    db.execute("INSERT OR IGNORE INTO ticket_watchers (ticket_id, identity) VALUES (?, ?)",
               (ticket_id, identity))
    if updates:
        db.execute("UPDATE ticket_watchers SET updates = 1 WHERE ticket_id = ? AND identity = ?",
                   (ticket_id, identity))


NOTIF_BODY_MAX = 16000  # notification/event payload body cap. 4000 cut two
                        # real messages in the #87 stress test; 16000 makes
                        # truncation "basically never" (#87 c585).


_AT_PATH_RE = _re.compile(r"@(?=[/~])")


def _defang_at_paths(text):
    """Break `@`+path adjacency in text that will be INJECTED into a session as
    a notification payload (#87 c-1249/c-1255). The Claude Code harness parses
    `@/abs/path` in a user message as a file-attachment directive and inlines
    the file's CONTENT — so a comment body or thread_tail containing
    `@<path-to-a-header-file>` caused every subscribed seat to read that file
    (a live bearer) into its own transcript, and the exposure SPREAD as the
    advisory about it quoted the form back through thread tails. A zero-width
    space after the `@` defeats the parse while staying invisible to a human
    reader; the source comment (GET /tickets/N) is untouched, so only the
    auto-injected copy is neutered. Keyed strictly on `@` before `/` or `~`
    (filesystem shapes) — never touches emails or `@mentions`."""
    return _AT_PATH_RE.sub("@​", text or "")


def _notif_body(body, fetch_hint=""):
    """Body for a notification/event payload. Was a hard `body[:200]` slice at
    every fan-out site — which silently clipped comments mid-sentence, and
    since a woken agent reads the payload AS the message, it acted on partial
    text (stranded a real design conversation on #277, a seat asking
    "your message got cut off again", 2026-07-29). Generous cap so ~every real
    comment arrives whole; anything longer is truncated with an EXPLICIT marker
    + fetch pointer, so a long body is a known fetch, never an invisible cut."""
    body = _defang_at_paths(body or "")
    if len(body) <= NOTIF_BODY_MAX:
        return body
    # True length in the marker (610/#87 P3): a pinned cut length carries
    # zero information about how much is missing.
    marker = f"\n\n…[truncated at {NOTIF_BODY_MAX} of {len(body)} chars"
    marker += f" — full text: {fetch_hint}]" if fetch_hint else " — fetch the full comment]"
    return body[:NOTIF_BODY_MAX] + marker


def _notify_ticket_event(ticket_id: int, event_type: str, actor: str, payload: dict,
                         skip: set = None, session_id: Optional[str] = None,
                         notify_self: bool = False):
    """Fan out within ticket watchers. A stamped comment may reach a pinned
    existing sibling; other self events remain suppressed. Mention recipients
    are deduplicated by skip. notify_self is retained for caller compatibility."""
    watchers = db.query("SELECT identity FROM ticket_watchers WHERE ticket_id = ?",
                        (ticket_id,))
    _trow = db.query_one("SELECT * FROM tickets WHERE id = ?", (ticket_id,)) or {}
    if event_type not in ("ticket.commented", "ticket.mentioned"):
        watchers = [{"identity": who} for who in _record_recipients("ticket", ticket_id)]
    watchers = [w for w in watchers if _identity_can_see_ticket(w["identity"], _trow)]   # scope-filtered (#637)
    fp = _session_fp(session_id)
    if fp:
        payload = {**payload, "actor_session": fp}
    payload = _comment_payload("ticket", payload)
    for w in watchers:
        if skip and w["identity"] in skip:
            continue
        addressed = (_sibling_notification("ticket", ticket_id, actor, payload)
                     if w["identity"] == actor else payload)
        if addressed is None:
            continue
        db.execute(
            "INSERT INTO notifications (recipient, ticket_id, event_type, actor, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (w["identity"], ticket_id, event_type, actor, json.dumps(addressed)))


def _thread_tail(ticket_id: int, before_comment_id: int = None, n: int = 3) -> list:
    """Last n comments before the triggering one, oldest-first, bodies capped
    with an EXPLICIT marker. Shipped with mention rows so being pulled into a
    live thread arrives with the conversation, not a doorbell (staging #86:
    "dispatched into a live thread with zero backlog … my first act was
    reconstruction")."""
    q = "SELECT id, author, body, created_at, session_fp, home_thread_id FROM ticket_comments WHERE ticket_id = ? "
    params = [ticket_id]
    if before_comment_id is not None:
        q += "AND id < ? "
        params.append(before_comment_id)
    q += "ORDER BY id DESC LIMIT ?"
    params.append(n)
    out = []
    for r in reversed(db.query(q, tuple(params))):
        body = r["body"] or ""
        body = _defang_at_paths(body)   # neuter @+path before it's injected (#87 c-1255)
        entry = {"comment_id": r["id"], "author": r["author"],
                 "created_at": r["created_at"], "session_fp": r["session_fp"],
                 "home_thread_id": r["home_thread_id"]}
        if len(body) > NOTIF_BODY_MAX:
            # The same cap as a delivered body (0.11.5): a holder born into a
            # live thread gets the last few messages WHOLE (tagged fresh →
            # last few messages, untruncated). The 800-char
            # preview this used to be sent a newcomer off to re-read the
            # ticket by hand, and one cut its own read short (#704).
            entry["truncated"] = True
            entry["true_length"] = len(body)
            entry["body"] = (body[:NOTIF_BODY_MAX] + f"\n…[truncated at {NOTIF_BODY_MAX} "
                             f"of {len(body)} chars — full comment: GET "
                             f"/api/v1/tickets/{ticket_id}#c{r['id']}]")
        else:
            entry["body"] = body
        out.append(entry)
    return out


def _notify_ticket_mentions(ticket_id: int, text: str, actor: str,
                            summary: str, comment_id: int = None, notes: list = None) -> set:
    """@mentions in ticket bodies/comments → subscribe + a dedicated
    ticket.mentioned row for each mentioned user (#342 — parity with
    doc.mentioned / task.mentioned). Unknown handles are ignored by
    _parse_mentions, so stray '@thing' prose never notifies anyone.

    v0.8.6 (staging #86): the mention row carries the triggering text itself
    (payload.body) plus a short thread tail — "being mentioned is the most
    conversational thing that can happen to you, and it was the one delivery
    that told you nothing"."""
    mentioned = _parse_mentions(text, exclude=actor)
    payload = {"summary": summary, "by": actor,
               "body": _notif_body(text, f"GET /api/v1/tickets/{ticket_id}"),
               "thread_tail": _thread_tail(ticket_id, before_comment_id=comment_id)}
    if text and len(text) > NOTIF_BODY_MAX:
        payload["truncated"] = True     # absent means whole (#87 c585)
    if comment_id is not None:
        payload["comment_id"] = comment_id
    else:
        payload["record_mention"] = True
    root = _root_payload("ticket", ticket_id)      # the work itself, for a newcomer (0.12.0)
    if root:
        payload["root"] = root
    blob = json.dumps(_comment_payload("ticket", payload))
    _trow = db.query_one("SELECT * FROM tickets WHERE id = ?", (ticket_id,)) or {}
    # scope-filtered (#637); since scope v1 the writer is told, not left guessing
    # (doc 261, decision 3): `notes` collects one line per outsider.
    inside = [u for u in mentioned if _identity_can_see_ticket(u, _trow)]
    if notes is not None:
        for u in mentioned:
            if u not in inside:
                notes.append(groupscope.note_for_outsider(u, _trow.get("group_scope")))
    mentioned = inside
    for u in mentioned:
        _subscribe_ticket(ticket_id, u, explicit=True)
        db.execute(
            "INSERT INTO notifications (recipient, ticket_id, event_type, actor, payload) "
            "VALUES (?, ?, 'ticket.mentioned', ?, ?)",
            (u, ticket_id, actor, blob))
    return set(mentioned)


_TAG_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")


def _norm_tags(tags) -> Optional[str]:
    """Normalize a tag list into its stored JSON string (or None). Tags are
    the substrate of user-defined views (#203: 'only ever from data — assigned
    to me and tag (x)'): lowercase slugs, deduped, max 8. Invalid input 400s
    loudly rather than storing junk."""
    if tags is None:
        return None
    out = []
    for t in tags:
        t = str(t).strip().lower()
        if not t:
            continue
        if not _TAG_RE.match(t):
            raise HTTPException(400, f"Invalid tag '{t}' — want [a-z0-9_-], max 24 chars")
        if t not in out:
            out.append(t)
    if len(out) > 8:
        raise HTTPException(400, "Max 8 tags per item")
    return json.dumps(out) if out else None


def _ref(kind: str, obj_id, label) -> str:
    """Ready-made markdown reference (H5, doc 69 item 8): serve the clickable
    [label](/path) form as a first-class field so citing bare ids stops being
    the path of least resistance. Square brackets in labels are softened so
    the markdown link never breaks. Console-relative paths render everywhere
    the console is served."""
    label = str(label or kind).replace("[", "(").replace("]", ")")
    return f"[{label}](/{kind}/{obj_id})"


def _row_tags(row):
    """Deserialize the stored tags JSON on a ticket/task row, in place."""
    if isinstance(row, dict) and "tags" in row:
        try:
            row["tags"] = json.loads(row["tags"]) if row["tags"] else []
        except (ValueError, TypeError):
            row["tags"] = []
    return row


def _tag_clause(tag: str):
    """WHERE fragment + param matching one tag inside the stored JSON array.
    Tag is validated first, so the LIKE pattern is injection-safe."""
    t = str(tag).strip().lower()
    if not _TAG_RE.match(t):
        raise HTTPException(400, f"Invalid tag filter '{tag}'")
    return "tags LIKE ?", f'%"{t}"%'


def _thread_of_existing(kind: str, object_id: int):
    """The object's thread, WITHOUT creating one. `_thread_of` is lazy and
    writes; a read path must not."""
    spec = _THREAD_TABLES.get(kind)
    if not spec:
        return None
    row = db.query_one(f"SELECT thread_id FROM {spec[0]} WHERE id = ?", (object_id,))
    return row["thread_id"] if row else None


def _event_for_conversation(auth: AuthContext, event, conv: str) -> bool:
    """Is this bus event worth waking THIS conversation for?

    The stream is a doorbell, not the post. Whoever it wakes then reads
    `?for_conversation=&undelivered=true`, which applies the real delivery
    rule (`_conversation_scope_clause`) — so this only has to be honest about
    "probably yours", never exact. What it must NOT be is a firehose.

    And it is the same rule for everyone. `_can_see_event` opens by returning
    true for any admin, which is correct for *permission* and wrong for
    *delivery*: it meant one identity's stream carried every event on the
    plane while every other seat's carried what was addressed to it — a role
    changing the shape of a feed. Watching somebody else's scope is a
    deliberate act with its own view, not something that leaks into your own.
    """
    me = auth.identity()
    kind, _, oid = (event.target or "").partition(":")
    if kind in _THREAD_PLANES and oid.isdigit():
        wt = _thread_of_existing(kind, int(oid))
        if wt and db.query_one(
                "SELECT 1 FROM threads WHERE identity = ? AND conversation_id = ? "
                "AND thread_id = ? AND state IN ('open','closing')",
                (me, conv[:64], wt)):
            return True                      # (a) a thread this conversation is on
    # (b) addressed at me directly. A wake (`POST /agents/{u}/wake`) emits with
    # target `user:<id>` and no object, so the thread check above cannot see
    # it — found by waking myself on a thread I was on and watching nothing
    # arrive, which is the whole reason to test a doorbell by ringing it.
    # There was a (c) here — "named in the payload (assignee, mention)" — that
    # rang for what opens a thread toward me. Since 0.12.69 that is a cold
    # wake for the rail and never in a live conversation's feed (#705), so the
    # ring would only buy an empty read. (`.data`, not `.payload`: Event has no
    # `payload` field, events.py:15 — the slip that 500'd the stream, 0.12.28.)
    return (event.scope or "") == auth.owner_id


def _can_see_event(auth: AuthContext, event) -> bool:
    """Visibility predicate for the event stream / recent feed.

    Ticket events route through _can_see_ticket (assignee + group-scope
    aware) regardless of event.scope — the old `auth.owns(target)` check
    missed both paths and leaked comment snippets via scope=="".
    Account and compute events follow their user/device, including tombstones.
    Other event kinds retain their explicit global/scoped semantics.
    """
    if auth.is_admin():
        return True
    target = event.target or ""
    if target.startswith("ticket:"):
        try:
            ticket = db.query_one("SELECT * FROM tickets WHERE id = ?",
                                  (int(target.split(":", 1)[1]),))
        except (ValueError, TypeError):
            return False
        return bool(ticket) and _can_see_ticket(auth, ticket)
    if target.startswith("doc:"):
        try:
            doc = db.query_one("SELECT * FROM docs WHERE id = ?",
                               (int(target.split(":", 1)[1]),))
        except (ValueError, TypeError):
            return False
        return bool(doc) and _can_see_doc(auth, doc)
    if target.startswith(("project:", "task:")):
        try:
            ref = int(target.split(":", 1)[1])
            if target.startswith("task:"):
                task = db.query_one("SELECT project_id FROM tasks WHERE id = ?", (ref,))
                ref = task["project_id"] if task else None
            project = db.query_one("SELECT * FROM projects WHERE id = ?", (ref,)) if ref else None
        except (ValueError, TypeError):
            return False
        return bool(project) and _can_see_project(auth, project)
    if event.type.startswith("user."):
        # Activation payloads contain account roles/scopes, not public news.
        if target == auth.owner_id:
            return True
        uid = groupscope._user_id(target)
        row = db.query_one("SELECT username FROM users WHERE id = ?", (uid,)) if uid else None
        return bool(row) and row["username"] == auth.identity()
    if event.type.startswith("lease_group."):
        group = db.query_one("SELECT * FROM lease_groups WHERE id=?",
                             (event.data.get("group_id") or target,))
        return bool(group) and _can_see_lease_group(auth, group)
    if event.type.startswith(("resource.", "lease.")):
        resource_id = event.data.get("resource_id") or target
        resource = db.query_one("SELECT device_id FROM resources WHERE id = ?", (resource_id,))
        # Deleted resources no longer have a row; their event preserves the
        # device scope before deletion so a tombstone cannot become global.
        device_id = resource["device_id"] if resource else (
            event.scope.split(":", 1)[1] if (event.scope or "").startswith("device:") else None)
        visible = _visible_device_ids(auth)
        return bool(device_id) and (auth.owns(resource_id) or visible is None or device_id in visible)
    if not event.scope:
        return True
    return auth.owns(target)



def _check_limit(limit: int, cap: int = 500) -> None:
    """Reject a `limit` outside 1..cap with the same 400 `/tickets` has always
    raised. Every list endpoint that hands `limit` to SQL or to a slice must
    call this: SQLite reads `LIMIT -1` as *no limit*, and `rows[:-1]` /
    `events[-limit:]` read a negative as "everything but one" — so a caller
    who typed -1 got the plane's whole history instead of an error. #690
    (2026-09-07, a `group:work` bearer read 2288 audit rows back to plane
    origin through `/audit?limit=-1`; the visibility predicate still applied,
    the over-fetch cap did not).
    """
    if not 1 <= limit <= cap:
        raise HTTPException(400, f"Invalid limit {limit}. Must be 1-{cap}.")


@app.get("/api/v1/tickets")
def list_tickets(response: Response, status: str = None, service_id: str = None,
                 device_id: str = None, assignee: str = None, tag: str = None,
                 group_scope: str = None, source: str = None, limit: int = 100, offset: int = 0,
                 auth: AuthContext = Depends(get_auth)):
    """List tickets visible to the caller's scope, optionally filtered.

    Pagination (#190): `limit` (default 100, max 500) and `offset` slice the
    *visible* result set, so pages are stable for scoped callers. The response
    stays a bare JSON array (fleet compat); the `X-Total-Count` header carries
    the total visible matches so truncation is detectable — page with
    `?offset=N` until offset+len >= total.
    """
    _check_limit(limit)
    if offset < 0:
        raise HTTPException(400, f"Invalid offset {offset}. Must be >= 0.")

    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if service_id:
        clauses.append("service_id = ?")
        params.append(service_id)
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if assignee:
        clauses.append("assignee = ?")
        params.append(assignee)
    if source:
        clauses.append("source = ?")       # who filed it (codex, doc 260 §4: the parameter was accepted and ignored)
        params.append(source)
    if tag:
        c, p = _tag_clause(tag)
        clauses.append(c)
        params.append(p)
    if group_scope:
        # Scope v1: the focus filter is exact — the ticket's own group, and
        # nothing anchored through its device or service (the pre-v20 union,
        # #330/#380, was for tickets that carried no group of their own; every
        # ticket has one now). Codex's retest (doc 264): `?group_scope=work`
        # answered with services, contributors and core tickets.
        clauses.append("group_scope = ?")
        params.append(group_scope)

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    tickets = db.query(
        f"SELECT * FROM tickets{where} ORDER BY created_at DESC, id DESC",
        tuple(params),
    )
    visible = [_row_tags(t) for t in tickets if _can_see_ticket(auth, t)]
    for t in visible:
        t["ref"] = _ref("tickets", t["id"], t.get("summary"))
    response.headers["X-Total-Count"] = str(len(visible))
    return visible[offset:offset + limit]


@app.get("/api/v1/tickets/{ticket_id}")
def get_ticket(ticket_id: int, auth: AuthContext = Depends(get_auth)):
    """Get ticket if visible to caller's scope."""
    ticket = db.query_one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
    if not ticket:
        raise HTTPException(404, f"Ticket {ticket_id} not found")
    if not _can_see_ticket(auth, ticket):
        raise HTTPException(404, "Ticket not found")
    ticket["ref"] = _ref("tickets", ticket_id, ticket.get("summary"))
    ticket["comments"] = db.query(
        "SELECT * FROM ticket_comments WHERE ticket_id = ? ORDER BY created_at",
        (ticket_id,),
    )
    ticket["claims"] = _claims_view(auth.identity(), ticket_id=ticket_id)
    return _row_tags(ticket)


@app.post("/api/v1/tickets")
def create_ticket(req: TicketCreate, auth: AuthContext = Depends(get_auth),
                  x_session_id: Optional[str] = Header(None),
                  x_thread_id: Optional[str] = Header(None)):
    _validate_ticket_addressing(req.service_id, req.device_id, req.assignee)
    group = _resolve_group(auth, req.group_scope, "ticket", assignee=req.assignee)
    _refuse_outsider_assignee(req.assignee, group)

    # Source is bearer-derived (not body) — closes staging #5 finding 3.
    source = auth.identity()
    ticket_id = db.execute("""
        INSERT INTO tickets (source, type, priority, status, summary, body, service_id, device_id, assignee, group_scope, tags)
        VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)
    """, (source, req.type, req.priority, req.summary, req.body,
          req.service_id, req.device_id, req.assignee, group, _norm_tags(req.tags)))

    # v19 (doc 239), #886: made inside a thread — never one of its own.
    _create_in_thread("ticket", ticket_id, source, _session_of(x_session_id), x_thread_id)
    db.audit(source, "ticket.create", str(ticket_id),
             {"summary": req.summary, "type": req.type, "priority": req.priority})

    # Auto-subscribe filer + assignee; fan out durable notifications.
    # Filing claims the thread for the filer's warm surface (#199),
    # session-stamped when the caller presents a nonce (#264).
    _subscribe_ticket(ticket_id, source)
    claim = _claim_thread(source, ticket_id, session_id=_session_of(x_session_id))
    _bind_conversation("ticket", ticket_id, source, _session_of(x_session_id))
    if req.assignee:
        _subscribe_ticket(ticket_id, req.assignee, explicit=True)
    notes = []
    mentioned = _notify_ticket_mentions(ticket_id, req.body, source, req.summary, notes=notes)
    _notify_ticket_event(ticket_id, "ticket.created", source,
                         {"summary": req.summary, "type": req.type,
                          "priority": req.priority, "assignee": req.assignee,
                          "body": _notif_body(req.body, f"GET /api/v1/tickets/{ticket_id}")},
                         skip=mentioned, session_id=_session_of(x_session_id),
                         notify_self=(req.assignee == source))

    event_bus.emit("ticket.created", f"ticket:{ticket_id}",
                   {"id": ticket_id, "source": source, "summary": req.summary,
                    "type": req.type, "priority": req.priority},
                   scope=f"ticket:{ticket_id}")

    out = {"status": "created", "id": ticket_id, "ticket_id": ticket_id,
           "ref": _ref("tickets", ticket_id, req.summary),
           "thread_id": _thread_of("ticket", ticket_id), "group_scope": group,
           "subscribed": True, "claim_owned": claim["owned"],
           "watch": _WATCH_HINT if claim["owned"] else _HELD_HINT}
    if notes:
        out["notes"] = notes
    return out


@app.patch("/api/v1/tickets/{ticket_id}")
def update_ticket(ticket_id: int, req: TicketUpdate, auth: AuthContext = Depends(get_auth),
                  x_session_id: Optional[str] = Header(None)):
    """Update ticket — only if you filed it or are admin.

    Editing claims the ticket thread, exactly as `update_task` has since #392
    (#529 c3977). This was the ONE mutating route on any of the three planes
    that claimed nothing and did not even accept the nonce header — so triage,
    the main verb on this plane, was invisible to the claims trail and would
    have been invisible to the holder hint above. A seat reassigning,
    reprioritising or rescoping a row is working that row."""
    ticket = db.query_one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
    if not ticket:
        raise HTTPException(404)
    if not _can_see_ticket(auth, ticket):
        raise HTTPException(404, "Ticket not found")

    # Scope v1 (doc 261): the admin, the filer, or an editor of the ticket's
    # group may change anything. The assignee may drive the ticket's lifecycle
    # — status only (#130: handing someone work but not the ability to finish
    # it breaks every async flow) — whatever their role, since an editor
    # addressed them.
    source = ticket.get("source", "")
    caller_username = auth.identity()
    is_filer = source in (auth.owner_id, caller_username)
    if not auth.is_admin() and not is_filer and not groupscope.can_edit(auth, ticket.get("group_scope")):
        is_assignee = ticket.get("assignee") and \
            ticket["assignee"] in (auth.owner_id, caller_username)
        status_only = req.status is not None and all(
            getattr(req, f) is None for f in ("assignee", "priority", "service_id", "device_id", "group_scope", "tags"))
        if not (is_assignee and status_only):
            role = groupscope.role_in(auth, ticket.get("group_scope"))
            raise HTTPException(403, f"You are a {role} in group '{ticket.get('group_scope')}'; changing a "
                                     f"ticket there needs an editor (the assignee may change status).")

    _validate_ticket_addressing(req.service_id, req.device_id, req.assignee)
    if req.group_scope is not None:
        _resolve_group(auth, req.group_scope, "ticket")
    # An assignee must be able to read the work — in the group the ticket is
    # in, or the one it moves to (doc 261, decision 3).
    target_group = req.group_scope or ticket.get("group_scope")
    _refuse_outsider_assignee(req.assignee if req.assignee is not None else
                              (ticket.get("assignee") if req.group_scope else None), target_group)

    updates, params = [], []
    if req.status:
        updates.append("status = ?")
        params.append(req.status)
        if req.status in ("resolved", "closed"):
            updates.append("resolved_at = ?")
            params.append(datetime.now(timezone.utc).isoformat())
        else:
            # a reopen clears it: the field says the ticket IS resolved, not
            # that it once was (staging #100 read open + resolved_at set)
            updates.append("resolved_at = NULL")
    if req.tags is not None:
        updates.append("tags = ?")
        params.append(_norm_tags(req.tags))
    if req.assignee is not None:
        updates.append("assignee = ?")
        params.append(req.assignee)
    if req.priority:
        updates.append("priority = ?")
        params.append(req.priority)
    if req.service_id is not None:
        updates.append("service_id = ?")
        params.append(req.service_id)
    if req.device_id is not None:
        updates.append("device_id = ?")
        params.append(req.device_id)
    if req.group_scope is not None:
        # "" clears to commons (NULL); a group id scopes it (#330).
        updates.append("group_scope = ?")
        params.append(req.group_scope or None)

    if updates:
        updates.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(ticket_id)
        db.execute(f"UPDATE tickets SET {', '.join(updates)} WHERE id = ?", tuple(params))
        # Audit every PATCH (2026-07-29): mutations previously left no audit
        # row — a silent batch device_id retag by a parallel surface went
        # unexplained for an hour because only ticket.create was logged
        # (#252 record). Actor + changed fields, nothing else.
        db.audit(auth.identity(), "ticket.update", str(ticket_id),
                 {k: getattr(req, k) for k in
                  ("status", "assignee", "priority", "service_id",
                   "device_id", "group_scope", "tags")
                  if getattr(req, k, None) is not None})

    # New assignee starts watching; existing watchers hear about the change.
    if req.assignee:
        _subscribe_ticket(ticket_id, req.assignee, explicit=True)
    actor = auth.identity()
    _notify_ticket_event(ticket_id, "ticket.updated", actor,
                         {"status": req.status, "assignee": req.assignee,
                          "priority": req.priority,
                          "summary": ticket.get("summary", ""),
                          # what this PATCH actually changed — the rail reads
                          # "assignee" here as an assignment; without it every
                          # update carried the current assignee and a priority
                          # tweak on your own ticket birthed a holder (3.9.15)
                          "changed": [f for f in ("status", "assignee", "priority", "tags", "service_id", "device_id", "group_scope")
                                      if getattr(req, f) is not None and (f != "assignee" or req.assignee != ticket.get("assignee"))]},
                         session_id=_session_of(x_session_id),
                         notify_self=(req.assignee == actor))

    claim = _claim_thread(actor, ticket_id,
                          session_id=_session_of(x_session_id))
    _bind_conversation("ticket", ticket_id, actor, _session_of(x_session_id))

    event_bus.emit("ticket.updated", f"ticket:{ticket_id}",
                   {"id": ticket_id, "status": req.status, "assignee": req.assignee,
                    "source": source},
                   scope=f"ticket:{ticket_id}")

    return {"status": "updated", "ticket_id": ticket_id,
            "claim_owned": claim["owned"]}


@app.post("/api/v1/tickets/{ticket_id}/comments")
def add_comment(ticket_id: int, req: TicketComment, auth: AuthContext = Depends(get_auth),
                x_session_id: Optional[str] = Header(None)):
    """Comment on a ticket — must be visible to caller."""
    ticket = db.query_one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
    if not ticket:
        raise HTTPException(404)
    if not _can_see_ticket(auth, ticket):
        raise HTTPException(404, "Ticket not found")

    # Author is bearer-derived (not body) — closes staging #5 finding 2.
    author = auth.identity()

    # #87 P9 (c563): a 0-char body was accepted at 200, created a real row,
    # and notified three recipients — whose lenses then rendered the ticket
    # SUMMARY as the message. Empty is always a caller bug; say so.
    if not req.body.strip():
        raise HTTPException(400, "empty comment body")

    _replay_guard("ticket_comments", "ticket_id", ticket_id, req.body)
    # One-level reply threading (#383, iMessage-style): a reply anchors to a
    # TOP-LEVEL comment on the same ticket. Replying to a reply flattens onto
    # its parent — one level max, so threads never become trees to re-walk.
    parent_id = req.parent_comment_id
    if parent_id is not None:
        parent = db.query_one(
            "SELECT id, ticket_id, parent_comment_id FROM ticket_comments WHERE id = ?",
            (parent_id,))
        if not parent or parent["ticket_id"] != ticket_id:
            raise HTTPException(400, f"parent_comment_id {parent_id} is not a "
                                     f"comment on ticket {ticket_id}")
        if parent["parent_comment_id"]:
            parent_id = parent["parent_comment_id"]
    # Session fingerprint on the record (#264): when two surfaces of one
    # identity do land on a thread, the trail shows WHICH wrote what.
    provenance = _writer_provenance(author, _session_of(x_session_id))
    comment_id = db.execute(
        "INSERT INTO ticket_comments (ticket_id, author, body, session_fp, home_thread_id, parent_comment_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ticket_id, author, req.body, provenance["session_fp"], provenance["home_thread_id"], parent_id),
    )
    # A comment IS activity — bump the parent so updated-sorts reorder (a
    # staging pass 2026-08-15 found updated tickets didn't reorder). All planes.
    db.execute("UPDATE tickets SET updated_at = datetime('now') WHERE id = ?", (ticket_id,))

    # A comment records participation, not a subscription. Existing watchers
    # receive it. It still claims the author's warm surface (#199) —
    # unless another LIVE session of this identity already owns it (#264).
    claim = _claim_thread(author, ticket_id, session_id=_session_of(x_session_id))
    _bind_conversation("ticket", ticket_id, author, _session_of(x_session_id))
    _cbody = _notif_body(req.body, f"GET /api/v1/tickets/{ticket_id}")
    # truncated:true rides the payload ONLY when the cut actually happened
    # (#87 c585 / 610/8): lenses skip the read-at-source round-trip on the
    # ~every row where the body arrived whole. Absent means whole.
    _cflag = {"truncated": True} if len(req.body) > NOTIF_BODY_MAX else {}
    notes = []
    mentioned = _notify_ticket_mentions(ticket_id, req.body, author,
                                        ticket.get("summary", ""), comment_id, notes=notes)
    _notify_ticket_event(ticket_id, "ticket.commented", author,
                         {"comment_id": comment_id, "body": _cbody,
                          "summary": ticket.get("summary", ""), **_cflag,
                          "mentions": sorted(mentioned)},      # whom it addresses (0.12.2): a closed thread's rail delivers a late message to them alone
                         skip=mentioned, session_id=_session_of(x_session_id))

    event_bus.emit("ticket.commented", f"ticket:{ticket_id}",
                   {"id": ticket_id, "comment_id": comment_id,
                    "author": author, "body": _cbody, **_cflag, **provenance},
                   scope=f"ticket:{ticket_id}")

    resp = {"status": "created", "comment_id": comment_id, **provenance,
            "subscribed": bool(db.query_one(
                "SELECT 1 FROM ticket_watchers WHERE ticket_id = ? AND identity = ?",
                (ticket_id, author))), "claim_owned": claim["owned"],
            "watch": _WATCH_HINT if claim["owned"] else _HELD_HINT}
    if notes:
        resp["notes"] = notes          # a mentioned outsider was not notified (doc 261)
    # Anti-runaway guard for agent↔agent threads: don't block, but surface
    # thread length so a looping pair has a natural prompt to wrap up.
    n_comments = db.query_one(
        "SELECT COUNT(*) AS c FROM ticket_comments WHERE ticket_id = ?",
        (ticket_id,))["c"]
    if n_comments >= 30:
        resp["thread_notice"] = (
            f"This ticket has {n_comments} comments. A concise next action may be "
            "more useful than another acknowledgement; the shared thread can continue.")
    return resp


@app.post("/api/v1/tickets/{ticket_id}/subscribe")
def subscribe_ticket(ticket_id: int, auth: AuthContext = Depends(get_auth)):
    """Watch a ticket — you'll get a notification row for every event on it."""
    ticket = db.query_one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
    if not ticket:
        raise HTTPException(404, f"Ticket {ticket_id} not found")
    if not _can_see_ticket(auth, ticket):
        raise HTTPException(404, "Ticket not found")
    _subscribe_ticket(ticket_id, auth.identity(), explicit=True, updates=True)
    return {"status": "subscribed", "ticket_id": ticket_id, "watch": _WATCH_HINT}


@app.delete("/api/v1/tickets/{ticket_id}/subscribe")
def unsubscribe_ticket(ticket_id: int, auth: AuthContext = Depends(get_auth)):
    """Stop watching a ticket (works even if you were auto-subscribed).

    Also records a mute (#83): your own later comments on this thread no
    longer re-enroll you. Assignment, an @mention, or an explicit re-subscribe
    clears it."""
    me = auth.identity()
    db.execute("DELETE FROM ticket_watchers WHERE ticket_id = ? AND identity = ?",
               (ticket_id, me))
    db.execute("INSERT OR IGNORE INTO thread_mutes (identity, ticket_id) VALUES (?, ?)",
               (me, ticket_id))
    return {"status": "unsubscribed", "ticket_id": ticket_id, "muted": True}


@app.get("/api/v1/tickets/{ticket_id}/watchers")
def list_watchers(ticket_id: int, auth: AuthContext = Depends(get_auth)):
    """Who is watching this ticket."""
    ticket = db.query_one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
    if not ticket:
        raise HTTPException(404, f"Ticket {ticket_id} not found")
    if not _can_see_ticket(auth, ticket):
        raise HTTPException(404, "Ticket not found")
    rows = db.query("SELECT identity, created_at, updates FROM ticket_watchers "
                    "WHERE ticket_id = ? ORDER BY created_at", (ticket_id,))
    return {"ticket_id": ticket_id, "watchers": rows}


# ══════════════════════════════════════════════════════════════════════════
# Docs plane (RFC #105) — a sibling to tickets: versioned, contributor-directed
# understanding/planning memory. Authorship is bearer-derived + immutable;
# every edit is an append-only revision; visibility reuses the group-scope
# predicate; events ride the #37 durable inbox.
# ══════════════════════════════════════════════════════════════════════════

_DOC_WATCH_HINT = ("In the doc's conversation audience. POST /api/v1/docs/{id}/subscribe "
                   "also subscribes to record changes; the owner receives them by responsibility. "
                   "Read your inbox: GET /api/v1/notifications?unread=true.")

_DOC_LINK_TYPES = ("ticket", "service", "device", "doc")


def _slugify(text: str) -> str:
    s = _re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:64] or "doc"


def _unique_slug(base: str) -> str:
    slug, n = base, 2
    while db.query_one("SELECT id FROM docs WHERE slug = ?", (slug,)):
        slug = f"{base}-{n}"
        n += 1
    return slug


def _resolve_doc(ident: str) -> Optional[dict]:
    """Look up a doc by numeric id or by slug."""
    if ident.isdigit():
        return db.query_one("SELECT * FROM docs WHERE id = ?", (int(ident),))
    return db.query_one("SELECT * FROM docs WHERE slug = ?", (ident,))


def _can_see_doc(auth: AuthContext, doc: dict) -> bool:
    """Scope v1 (doc 261): the admin, the author, or a member of the doc's
    group. The null-scope commons is gone — an unscoped row is the admin's."""
    return groupscope.object_readable(auth, doc, owner_cols=("source", "owner"))


def _doc_is_owner(auth: AuthContext, doc: dict) -> bool:
    """Lifecycle gate (PATCH/DELETE/link-remove): owner or admin only.

    Contributions (new revisions) are open to anyone who can see the doc; only
    the owner/admin may change its metadata or destroy it — so an identity's
    authored record stays safe from others rewriting its lifecycle.
    """
    if auth.is_admin():
        return True
    source = doc.get("owner") or doc.get("source", "")
    return bool(source) and source in (auth.owner_id, auth.identity())

def _require_doc_editor(auth: AuthContext, doc: dict) -> None:
    """A content change to a doc — a revision, an append, a link — is the
    author's, an admin's, or an editor's of the doc's group (scope v1); a
    viewer reads and comments."""
    if not groupscope.object_editable(auth, doc, owner_cols=("source", "owner")):
        role = groupscope.role_in(auth, doc.get("group_scope"))
        raise HTTPException(403, f"You are a {role} in group '{doc.get('group_scope')}'; changing a doc there needs an editor.")



def _identity_can_see_doc(identity: str, doc: dict) -> bool:
    """Row-level mirror of _can_see_doc for notification fan-out (#500) — by
    membership, which the pre-scope-v1 mirror never read (doc 262 §3)."""
    return groupscope.identity_object_readable(identity, doc, owner_cols=("source", "owner"))


def _doc_scope_filter(doc_id: int, recipients):
    """Drop fan-out recipients who cannot see the doc (#500). Order-preserving."""
    doc = db.query_one("SELECT source, owner, group_scope FROM docs WHERE id = ?", (doc_id,))
    if not doc:
        return []
    return [r for r in recipients if _identity_can_see_doc(r, doc)]


def _resolve_group(auth: AuthContext, group_scope: Optional[str], kind: str = "item",
                   assignee: Optional[str] = None, current: Optional[str] = None) -> str:
    """The group an object goes into (scope v1, doc 261). Named: it must exist
    and the caller must be an editor of it (a viewer reads; an outsider is told
    the group is not theirs, not that it does not exist — they named it).
    Unnamed on a create: the writer's first editable group, the assignee's
    shared one first; `core` for the admin; 422 for a writer who edits none.
    Unnamed on an update: the object keeps its group (`current`). Nothing is
    ever unscoped — the `""` clear-to-commons sentinel is refused."""
    if group_scope == "":
        raise HTTPException(422, f"A {kind} is always in a group; name one, it cannot be cleared.")
    if group_scope:
        if not db.query_one("SELECT id FROM groups WHERE id = ?", (group_scope,)):
            raise HTTPException(400, f"Unknown group_scope '{group_scope}'. Must be a group id "
                                     f"(GET /api/v1/groups).")
        role = groupscope.role_in(auth, group_scope)
        if role is None:
            raise HTTPException(403, f"You are not in group '{group_scope}'; a {kind} goes in a "
                                     f"group you edit (GET /api/v1/groups lists yours).")
        if not groupscope.can_edit(auth, group_scope):
            raise HTTPException(403, f"You are a {role} in group '{group_scope}'; creating or "
                                     f"changing a {kind} there needs an editor.")
        return group_scope
    if current:
        return current
    g = groupscope.default_group(auth, assignee)
    if not g:
        raise HTTPException(422, f"Name a group_scope for the {kind}: you edit no group "
                                 f"(GET /api/v1/groups shows your role in each).")
    if not db.query_one("SELECT id FROM groups WHERE id = ?", (g,)):
        raise HTTPException(422, f"Name a group_scope for the {kind}: the default group '{g}' does not exist.")
    return g


def _validate_group_scope(auth: AuthContext, group_scope: Optional[str], kind: str = "item"):
    """Kept for the PATCH sites: a named group must exist and be one the
    caller edits (delegates to `_resolve_group`)."""
    if group_scope:
        _resolve_group(auth, group_scope, kind)


def _addressee_note(username: Optional[str], group_id: Optional[str], what: str = "assignee") -> Optional[str]:
    """The system note when an assignee is not a member of the object's group
    (doc 261, decision 3); None when they are, or when nobody is named."""
    if not username or groupscope.user_can_read(username, group_id):
        return None
    return groupscope.note_for_outsider(username, group_id)


def _refuse_outsider_assignee(username: Optional[str], group_id: Optional[str], kind: str = "ticket"):
    """An assignment to someone who cannot read the group is refused with the
    note — an assignee must be able to read the work."""
    note = _addressee_note(username, group_id)
    if note:
        raise HTTPException(422, f"Cannot assign this {kind}: {note}")


def _validate_doc_scope(auth: AuthContext, group_scope):
    """Write-side twin of the read predicate — one resolver for every kind (scope v1)."""
    if group_scope:
        _resolve_group(auth, group_scope, "doc")


def _validate_doc_owner(owner: str, group: str, creator: str):
    """Named owners must already be in scope. Existing non-user creators stay valid."""
    if not owner or (owner != creator and not db.query_one(
            "SELECT id FROM users WHERE username = ?", (owner,))):
        raise HTTPException(422, "owner must name an existing identity")
    if owner != creator:
        _refuse_outsider_assignee(owner, group, "doc")


def _subscribe_doc(doc_id: int, identity: str, explicit: bool = False,
                   updates: bool = False):
    """Mirror of _subscribe_ticket's mute semantics (#83): explicit acts clear
    a mute; creation/revision enrolment respects it. Comments do not enrol."""
    if explicit:
        db.execute("DELETE FROM thread_mutes WHERE identity = ? AND doc_id = ?",
                   (identity, doc_id))
    elif db.query_one("SELECT 1 FROM thread_mutes WHERE identity = ? AND doc_id = ?",
                      (identity, doc_id)):
        return
    db.execute("INSERT OR IGNORE INTO doc_watchers (doc_id, identity) VALUES (?, ?)",
               (doc_id, identity))
    if updates:
        db.execute("UPDATE doc_watchers SET updates = 1 WHERE doc_id = ? AND identity = ?",
                   (doc_id, identity))


def _record_recipients(kind: str, oid: int) -> set:
    """Responsible recipient and explicit update subscribers, never participants alone."""
    row = db.query_one(f"SELECT * FROM {kind}s WHERE id = ?", (oid,)) or {}
    responsible = ((row.get("owner") or row.get("source")) if kind == "doc"
                   else (row.get("assignee") or row.get("source")))
    recipients = {w["identity"] for w in db.query(
        f"SELECT identity FROM {kind}_watchers WHERE {kind}_id = ? AND updates = 1", (oid,))}
    if responsible:
        recipients.add(responsible)
    return recipients


def _notify_doc_event(doc_id: int, event_type: str, actor: str, payload: dict,
                      session_id: Optional[str] = None, skip=()):
    """Fan a doc event out to its watchers (durable inbox rows, skipping the actor)."""
    watchers = db.query("SELECT identity FROM doc_watchers WHERE doc_id = ?", (doc_id,))
    doc = db.query_one("SELECT owner, source FROM docs WHERE id = ?", (doc_id,)) or {}
    watchers.append({"identity": doc.get("owner") or doc.get("source")})
    if event_type not in ("doc.commented", "doc.mentioned"):
        watchers = [{"identity": who} for who in _record_recipients("doc", doc_id)]
    _notify_doc_to(doc_id, [w["identity"] for w in watchers if w["identity"] not in skip], event_type, actor, payload,
                   session_id=session_id)


def _notify_doc_to(doc_id: int, recipients, event_type: str, actor: str, payload: dict,
                   session_id: Optional[str] = None):
    """Fan out within scope-filtered doc recipients, once per comment.
    Stamped comments may reach a pinned existing sibling, never their writer."""
    fp = _session_fp(session_id)
    if fp:
        # the writer's fingerprint, as on ticket rows: a holder tells another
        # conversation of its identity from its own echo by it (staging #109:
        # the author's interactive comment reached the row and the rail read
        # it as the doc holder's echo for want of this)
        payload = {**payload, "actor_session": fp}
    payload = _comment_payload("doc", payload)
    reached = _reached_for_comment("doc_id", doc_id, actor, payload.get("comment_id")) \
        if event_type != "doc.mentioned" else set()         # one comment, one row per reader (0.12.0)
    for r in _doc_scope_filter(doc_id, dict.fromkeys(recipients)):
        if not r:
            continue
        if r in reached:
            continue
        addressed = _sibling_notification("doc", doc_id, actor, payload) if r == actor else payload
        if addressed is None:
            continue
        db.execute("INSERT INTO notifications (recipient, doc_id, event_type, actor, payload) "
                   "VALUES (?, ?, ?, ?, ?)", (r, doc_id, event_type, actor, json.dumps(addressed)))




# Reach (#123 follow-up): deliver docs to the agents who care about them even
# when they're not already a watcher — via @mention in copy, group scope, and
# doc→ticket links. These are *delivery* improvements; waking a cold agent is a
# separate problem (#95).
_MENTION_RE = _re.compile(r"(?<![A-Za-z0-9_/])@([A-Za-z0-9][A-Za-z0-9._-]{0,63})")
_FENCE_RE = _re.compile(r"```.*?```", _re.S)
_INLINE_CODE_RE = _re.compile(r"`[^`\n]*`")
_QUOTE_LINE_RE = _re.compile(r"^[ \t]*>.*$", _re.M)


def _mention_text(text: str) -> str:
    """The part of a body that can address someone: quoted text does not.
    A mention inside a code fence, an inline code span or a blockquote is
    someone quoting, not someone asking — a design note that quoted the
    addressing form as an example birthed a holder onto the ticket, and the
    record of that birth, quoting it as evidence, birthed another (doc 238,
    2026-09-08). Same rule for the count-based diff on doc revisions."""
    t = _FENCE_RE.sub(" ", text or "")
    t = _INLINE_CODE_RE.sub(" ", t)
    return _QUOTE_LINE_RE.sub(" ", t)


def _parse_mentions(text: str, exclude: str = None) -> list:
    """Known usernames @mentioned in `text` (minus `exclude`). Unknown handles
    are ignored, so a stray '@thing' in prose doesn't notify anyone. Quoted
    text (code, blockquotes) never addresses — see _mention_text."""
    if not text:
        return []
    out = []
    for u in dict.fromkeys(_MENTION_RE.findall(_mention_text(text))):
        if u != exclude and db.query_one("SELECT id FROM users WHERE username = ?", (u,)):
            out.append(u)
    return out


def _mention_count(text: str) -> dict:
    """Occurrence count per @handle — NOT deduped, unlike _parse_mentions.

    This is the diff basis for "did this revision ADD a mention", and set
    membership cannot answer that question: on a long-lived doc a name already
    appears, so an identity-set difference reports "already mentioned" and the
    new ask never reaches anyone. Counting occurrences distinguishes 'their
    name is in untouched prose' (count unchanged) from 'someone just wrote a
    new @them' (count up) — the first is noise, the second is a routed request.
    """
    counts: dict = {}
    for u in _MENTION_RE.findall(_mention_text(text)):
        counts[u] = counts.get(u, 0) + 1
    return counts


def _notify_doc_mentions(doc_id: int, text: str, actor: str, slug: str, title: str,
                         previous_text: Optional[str] = None, comment_id=None, notes: list = None,
                         revision: Optional[int] = None) -> list:
    """@mentions → subscribe + notify each mentioned user (the direct reach path
    for docs not linked to a ticket).

    When `previous_text` is given (the revision path), only mentions NEWLY
    introduced by this text fire. A name sitting in unchanged prose is not a
    mention event: on a hot overwrite-in-place doc, whole-body re-parsing sent
    every named agent a doc.mentioned per revision AND re-enrolled them as
    watchers — a self-amplifying loop (#414: 29 revisions in a day recruited
    2 watchers into 7, 50-row pileups on agents with no stake). Creates and
    comments pass no previous_text: there, all text is new by definition."""
    mentioned = _parse_mentions(text, exclude=actor)
    if previous_text is not None:
        was, now = _mention_count(previous_text), _mention_count(text)
        mentioned = [u for u in mentioned if now.get(u, 0) > was.get(u, 0)]
    # Scope v1 (doc 261, decision 3): an outsider is neither subscribed nor
    # notified, and the writer is told in `notes`.
    _drow = db.query_one("SELECT source, group_scope FROM docs WHERE id = ?", (doc_id,)) or {}
    inside = [u for u in mentioned if _identity_can_see_doc(u, _drow)]
    if notes is not None:
        for u in mentioned:
            if u not in inside:
                notes.append(groupscope.note_for_outsider(u, _drow.get("group_scope")))
    mentioned = inside
    for u in mentioned:
        _subscribe_doc(doc_id, u, explicit=True)
    if mentioned:
        # The row carries the text that mentioned you (0.12.0): a doc mention
        # used to be the one delivery with no body at all — a holder born from
        # it had a slug and a name and nothing to answer (doc 246 D2).
        payload = {"slug": slug, "title": title, "by": actor,
                   "body": _notif_body(text, f"GET /api/v1/docs/{doc_id}")}
        if comment_id is not None:
            payload["comment_id"] = comment_id
        else:
            payload["record_mention"] = True
            if revision is not None:
                payload["rev"] = revision
        root = _root_payload("doc", doc_id)
        if root:
            payload["root"] = root
        _notify_doc_to(doc_id, mentioned, "doc.mentioned", actor, payload)
    return mentioned


def _group_member_usernames(group_id: str) -> list:
    return [r["username"] for r in db.query(
        "SELECT u.username FROM group_members gm JOIN users u ON u.id = gm.user_id "
        "WHERE gm.group_id = ?", (group_id,))]


def _doc_links(doc_id: int) -> list[dict]:
    return db.query("SELECT id, target_type, target_id, created_by, created_at "
                    "FROM doc_links WHERE doc_id = ? ORDER BY id", (doc_id,))


def _doc_view(doc: dict, rev: Optional[int] = None) -> dict:
    """Assemble a full doc response: metadata + the body of `rev` (default head)
    + links + revision count."""
    out = dict(doc)
    target_rev = rev if rev is not None else doc["current_rev"]
    r = db.query_one("SELECT * FROM doc_revisions WHERE doc_id = ? AND rev = ?",
                     (doc["id"], target_rev))
    if r:
        out["rev"] = r["rev"]
        out["title"] = r["title"]          # title snapshot at this revision
        out["body"] = r["body"]
        out["rev_author"] = r["author"]
        out["rev_note"] = r["note"]
        out["rev_created_at"] = r["created_at"]
    out["ref"] = _ref("docs", doc["id"], out.get("title"))
    out["links"] = _doc_links(doc["id"])
    # Doc hierarchy (v0.7.71): children listed on the parent read, so an
    # overview doc is a live index of its sub-docs without manual pointers.
    out["children"] = db.query(
        "SELECT id, slug, title, kind, status FROM docs WHERE parent_doc_id = ? "
        "ORDER BY id", (doc["id"],))
    # H3: the parent surfaces too, so a child renders its breadcrumb without a
    # second fetch. Same shape as a children row.
    out["parent"] = (db.query_one(
        "SELECT id, slug, title, kind, status FROM docs WHERE id = ?",
        (doc["parent_doc_id"],)) if doc.get("parent_doc_id") else None)
    out["revision_count"] = db.query_one(
        "SELECT COUNT(*) AS c FROM doc_revisions WHERE doc_id = ?", (doc["id"],))["c"]
    out["comments"] = db.query(
        "SELECT id, author, body, created_at, session_fp, home_thread_id FROM doc_comments WHERE doc_id = ? "
        "ORDER BY created_at", (doc["id"],))
    return out


@app.get("/api/v1/docs")
def list_docs(kind: str = None, status: str = None, group_scope: str = None,
              linked_ticket: int = None, q: str = None, parent_id: int = None,
              auth: AuthContext = Depends(get_auth)):
    """List docs visible to the caller's scope, optionally filtered. No bodies
    (those come from GET /docs/{id}); each row carries its links for threading."""
    clauses, params = [], []
    if kind:
        clauses.append("kind = ?"); params.append(kind)
    if status:
        clauses.append("status = ?"); params.append(status)
    if group_scope:
        clauses.append("group_scope = ?"); params.append(group_scope)
    if q:
        clauses.append("(title LIKE ? OR slug LIKE ?)"); params += [f"%{q}%", f"%{q}%"]
    if linked_ticket is not None:
        clauses.append("id IN (SELECT doc_id FROM doc_links "
                       "WHERE target_type = 'ticket' AND target_id = ?)")
        params.append(str(linked_ticket))
    if parent_id is not None:
        clauses.append("parent_doc_id = ?"); params.append(parent_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    docs = db.query(f"SELECT * FROM docs{where} ORDER BY updated_at DESC LIMIT 200",
                    tuple(params))
    visible = [d for d in docs if _can_see_doc(auth, d)]
    for d in visible:
        d["ref"] = _ref("docs", d["id"], d.get("title"))
        d["links"] = db.query("SELECT target_type, target_id FROM doc_links WHERE doc_id = ?",
                              (d["id"],))
    return {"docs": visible}


@app.post("/api/v1/docs")
def create_doc(req: DocCreate, auth: AuthContext = Depends(get_auth),
               x_session_id: Optional[str] = Header(None),
               x_thread_id: Optional[str] = Header(None)):
    """Create a doc — opens revision 1. Source is bearer-derived and immutable.
    A create carrying X-Session-Id binds the author's conversation (doc 174:
    "threads you start are yours"), as ticket and task creation do — staging
    #109 found a peer's comment on a doc an instance had just written birthed
    a second instance of the author instead of landing in the first."""
    source = auth.identity()
    group = _resolve_group(auth, req.group_scope, "doc")
    owner = req.owner if req.owner is not None else source
    _validate_doc_owner(owner, group, source)
    if req.supersedes is not None and not db.query_one("SELECT id FROM docs WHERE id = ?", (req.supersedes,)):
        raise HTTPException(400, f"Unknown supersedes doc id {req.supersedes}.")

    if req.parent_doc_id is not None:
        parent = db.query_one("SELECT * FROM docs WHERE id = ?", (req.parent_doc_id,))
        if not parent:
            raise HTTPException(400, f"Unknown parent_doc_id {req.parent_doc_id}.")
        if not _can_see_doc(auth, parent):
            raise HTTPException(400, f"Unknown parent_doc_id {req.parent_doc_id}.")

    slug = _unique_slug(_slugify(req.slug or req.title))
    doc_id = db.execute(
        "INSERT INTO docs (slug, title, kind, status, group_scope, source, owner, supersedes, parent_doc_id, current_rev) "
        "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, 1)",
        (slug, req.title, req.kind, group, source, owner, req.supersedes, req.parent_doc_id))
    db.execute(
        "INSERT INTO doc_revisions (doc_id, rev, title, body, author, note) VALUES (?, 1, ?, ?, ?, ?)",
        (doc_id, req.title, req.body, source, req.note or "initial revision"))
    _create_in_thread("doc", doc_id, source, _session_of(x_session_id), x_thread_id)   # #886

    if req.supersedes is not None:
        db.execute("UPDATE docs SET status = 'superseded', updated_at = ? WHERE id = ?",
                   (datetime.now(timezone.utc).isoformat(), req.supersedes))
        db.execute("INSERT OR IGNORE INTO doc_links (doc_id, target_type, target_id, created_by) "
                   "VALUES (?, 'doc', ?, ?)", (doc_id, str(req.supersedes), source))

    db.audit(source, "doc.create", str(doc_id),
             {"slug": slug, "title": req.title, "kind": req.kind})
    _subscribe_doc(doc_id, source)
    _bind_conversation("doc", doc_id, source, _session_of(x_session_id))
    notes = []
    mentioned = _notify_doc_mentions(doc_id, req.body, source, slug, req.title, notes=notes, revision=1)
    # Group membership is access, never a publication subscription.
    if owner != source and owner not in mentioned:
        _notify_doc_to(doc_id, [owner], "doc.assigned", source,
                       {"slug": slug, "title": req.title, "owner": owner})
    event_bus.emit("doc.created", f"doc:{doc_id}",
                   {"id": doc_id, "slug": slug, "title": req.title, "source": source},
                   scope=f"doc:{doc_id}")
    out = {"status": "created", "id": doc_id, "doc_id": doc_id, "slug": slug, "rev": 1,
           "ref": _ref("docs", doc_id, req.title), "group_scope": group, "owner": owner,
           "thread_id": _thread_of("doc", doc_id),
           "subscribed": True, "watch": _DOC_WATCH_HINT}
    if notes:
        out["notes"] = notes
    return out


@app.get("/api/v1/docs/{ident}")
def get_doc(ident: str, rev: int = None, auth: AuthContext = Depends(get_auth)):
    """Get a doc by slug or id. `?rev=N` returns a past revision's body."""
    doc = _resolve_doc(ident)
    if not doc:
        raise HTTPException(404, f"Doc '{ident}' not found")
    if not _can_see_doc(auth, doc):
        raise HTTPException(404, "Doc not found")
    if rev is not None and not db.query_one(
            "SELECT 1 FROM doc_revisions WHERE doc_id = ? AND rev = ?", (doc["id"], rev)):
        raise HTTPException(404, f"Revision {rev} not found for doc {doc['id']}")
    out = _doc_view(doc, rev=rev)
    out["claims"] = _claims_view(auth.identity(), doc_id=doc["id"])
    return out


@app.patch("/api/v1/docs/{doc_id}")
def update_doc(doc_id: int, req: DocUpdate,
               x_session_id: Optional[str] = Header(None),
               auth: AuthContext = Depends(get_auth)):
    """Change a doc's metadata/lifecycle (owner or admin). Content edits go
    through POST /docs/{id}/revisions so every content change is tracked."""
    doc = db.query_one("SELECT * FROM docs WHERE id = ?", (doc_id,))
    if not doc:
        raise HTTPException(404, f"Doc {doc_id} not found")
    if not _can_see_doc(auth, doc):
        raise HTTPException(404, "Doc not found")
    if not _doc_is_owner(auth, doc) and not groupscope.can_manage(auth, doc.get("group_scope")):
        raise HTTPException(403, "Only the doc's owner or an admin can change its metadata")
    # Scope v1: a doc is always in a group — it moves to one the caller edits,
    # it is never cleared (the old "" clear-to-commons sentinel is refused).
    if req.group_scope is not None:
        _resolve_group(auth, req.group_scope, "doc")
    owner = req.owner if req.owner is not None else (doc.get("owner") or doc["source"])
    _validate_doc_owner(owner, req.group_scope or doc.get("group_scope"), doc["source"])
    if req.supersedes is not None and not db.query_one("SELECT id FROM docs WHERE id = ?", (req.supersedes,)):
        raise HTTPException(400, f"Unknown supersedes doc id {req.supersedes}.")

    updates, params = [], []
    for field in ("status", "kind", "group_scope", "supersedes", "owner"):
        val = getattr(req, field)
        if val is not None:
            updates.append(f"{field} = ?")
            params.append(val)
    if req.parent_doc_id is not None:
        # 0 clears the parent; otherwise validate existence + cycle-guard the chain
        if req.parent_doc_id == 0:
            updates.append("parent_doc_id = NULL")
        else:
            if req.parent_doc_id == doc_id:
                raise HTTPException(400, "A doc cannot be its own parent.")
            cursor, seen = req.parent_doc_id, set()
            while cursor is not None and cursor not in seen:
                seen.add(cursor)
                if cursor == doc_id:
                    raise HTTPException(400, "parent_doc_id would create a cycle.")
                row = db.query_one("SELECT parent_doc_id FROM docs WHERE id = ?", (cursor,))
                if row is None:
                    raise HTTPException(400, f"Unknown parent_doc_id {req.parent_doc_id}.")
                cursor = row["parent_doc_id"]
            updates.append("parent_doc_id = ?")
            params.append(req.parent_doc_id)
    if not updates:
        return {"status": "noop", "doc_id": doc_id}
    updates.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(doc_id)
    db.execute(f"UPDATE docs SET {', '.join(updates)} WHERE id = ?", tuple(params))

    actor = auth.identity()
    changed = {f: getattr(req, f) for f in ("status", "kind", "group_scope", "supersedes", "owner", "parent_doc_id")
               if getattr(req, f) is not None}
    db.audit(actor, "doc.update", str(doc_id), changed)
    _notify_doc_event(doc_id, "doc.updated", actor,
                      {"slug": doc["slug"], "title": doc["title"], **changed},
                      session_id=_session_of(x_session_id),
                      skip={owner} if owner != (doc.get("owner") or doc["source"]) else ())
    if owner != (doc.get("owner") or doc["source"]):
        _notify_doc_to(doc_id, [owner], "doc.assigned", actor,
                       {"slug": doc["slug"], "title": doc["title"], "owner": owner})
    _bind_conversation("doc", doc_id, actor, _session_of(x_session_id))
    event_bus.emit("doc.updated", f"doc:{doc_id}",
                   {"id": doc_id, "status": req.status}, scope=f"doc:{doc_id}")
    return {"status": "updated", "doc_id": doc_id}


@app.delete("/api/v1/docs/{doc_id}")
def delete_doc(doc_id: int, auth: AuthContext = Depends(get_auth)):
    """Permanently delete a doc + its history (owner or admin). Prefer
    PATCH status=archived to preserve the record."""
    doc = db.query_one("SELECT * FROM docs WHERE id = ?", (doc_id,))
    if not doc:
        raise HTTPException(404, f"Doc {doc_id} not found")
    if not _doc_is_owner(auth, doc):
        raise HTTPException(403, "Only the doc owner or an admin can delete it")
    # Atomic teardown. Everything that FK-references docs(id) — the doc's own
    # notification rows (notifications.doc_id), other docs that supersede it
    # (docs.supersedes), and doc-target links pointing at it — must be cleared
    # in the SAME transaction, or the final DELETE FK-fails halfway and orphans
    # the doc with its history already wiped (dogfood finding, 2026-06-29).
    with db.transaction() as conn:
        conn.execute("DELETE FROM notifications WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM thread_claims WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM doc_comments WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM doc_watchers WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM doc_links WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM doc_links WHERE target_type = 'doc' AND target_id = ?", (str(doc_id),))
        conn.execute("UPDATE docs SET supersedes = NULL WHERE supersedes = ?", (doc_id,))
        conn.execute("DELETE FROM doc_revisions WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
    db.audit(auth.identity(), "doc.delete", str(doc_id), {"slug": doc["slug"]})
    return {"status": "deleted", "doc_id": doc_id,
            "hint": "Delete is permanent — PATCH status=archived preserves history."}


@app.get("/api/v1/docs/{doc_id}/revisions")
def list_doc_revisions(doc_id: int, auth: AuthContext = Depends(get_auth)):
    """Full revision history (metadata only; bodies via GET /docs/{id}?rev=N)."""
    doc = db.query_one("SELECT * FROM docs WHERE id = ?", (doc_id,))
    if not doc:
        raise HTTPException(404, f"Doc {doc_id} not found")
    if not _can_see_doc(auth, doc):
        raise HTTPException(404, "Doc not found")
    rows = db.query("SELECT rev, title, author, note, created_at FROM doc_revisions "
                    "WHERE doc_id = ? ORDER BY rev DESC", (doc_id,))
    return {"doc_id": doc_id, "current_rev": doc["current_rev"], "revisions": rows}


def _commit_doc_revision(doc: dict, new_body: str, new_title: str, author: str, note,
                         session_id: Optional[str] = None) -> dict:
    """Write an immutable revision (head advances), subscribe the author, fan out
    doc.revised. Shared by the full-body edit and the additive append helper.
    Revising claims the doc thread for the author's warm surface (#392)."""
    doc_id = doc["id"]
    prev = db.query_one("SELECT body FROM doc_revisions WHERE doc_id = ? AND rev = ?",
                        (doc_id, doc["current_rev"]))
    prev_body = prev["body"] if prev else ""
    new_rev = doc["current_rev"] + 1
    db.execute("INSERT INTO doc_revisions (doc_id, rev, title, body, author, note) "
               "VALUES (?, ?, ?, ?, ?, ?)",
               (doc_id, new_rev, new_title, new_body, author, note))
    db.execute("UPDATE docs SET current_rev = ?, title = ?, updated_at = ? WHERE id = ?",
               (new_rev, new_title, datetime.now(timezone.utc).isoformat(), doc_id))
    db.audit(author, "doc.revise", str(doc_id), {"rev": new_rev, "note": note})
    _subscribe_doc(doc_id, author)
    claim = _claim_thread(author, doc_id=doc_id, session_id=session_id)
    _bind_conversation("doc", doc_id, author, session_id)
    _notify_doc_event(doc_id, "doc.revised", author,
                      {"rev": new_rev, "title": new_title, "note": note, "slug": doc["slug"]},
                      session_id=session_id)
    # A link is navigation, not a subscription to another object's revisions.
    _notify_doc_mentions(doc_id, new_body, author, doc["slug"], new_title,
                         previous_text=prev_body, revision=new_rev)
    event_bus.emit("doc.revised", f"doc:{doc_id}",
                   {"id": doc_id, "rev": new_rev, "author": author, "slug": doc["slug"]},
                   scope=f"doc:{doc_id}")
    return {"status": "revised", "doc_id": doc_id, "rev": new_rev,
            "subscribed": True, "claim_owned": claim["owned"],
            "watch": _DOC_WATCH_HINT}


@app.post("/api/v1/docs/{doc_id}/revisions")
def add_doc_revision(doc_id: int, req: DocRevisionCreate,
                     auth: AuthContext = Depends(get_auth),
                     x_session_id: Optional[str] = Header(None)):
    """Edit a doc = a new immutable revision (full body). Author bearer-derived.
    Anyone who can see the doc may contribute (contributor-directed)."""
    doc = db.query_one("SELECT * FROM docs WHERE id = ?", (doc_id,))
    if not doc:
        raise HTTPException(404, f"Doc {doc_id} not found")
    if not _can_see_doc(auth, doc):
        raise HTTPException(404, "Doc not found")
    _require_doc_editor(auth, doc)
    if req.base_rev is not None and req.base_rev != doc["current_rev"]:
        # #491: a full-body rewrite based on a stale head would silently discard
        # every revision after base_rev. Refuse with the current head so the
        # caller can re-read, merge, and resubmit.
        raise HTTPException(409, {
            "detail": f"Doc {doc_id} is at rev {doc['current_rev']}, not {req.base_rev} — "
                      "re-read the head and merge before rewriting.",
            "current_rev": doc["current_rev"], "base_rev": req.base_rev})
    title = req.title if req.title is not None else doc["title"]
    return _commit_doc_revision(doc, req.body, title, auth.identity(), req.note,
                                session_id=_session_of(x_session_id))


@app.post("/api/v1/docs/{doc_id}/append")
def append_doc_block(doc_id: int, req: DocAppend, auth: AuthContext = Depends(get_auth),
                     x_session_id: Optional[str] = Header(None)):
    """Additive edit: append a block to the END of the current body without
    resubmitting the whole thing (saves tokens when an agent is only adding a
    section). Still produces a normal immutable revision. Use /revisions for a
    non-additive rewrite. Finer ops (insert / replace block, replace line) are
    a tracked follow-up (#112)."""
    doc = db.query_one("SELECT * FROM docs WHERE id = ?", (doc_id,))
    if not doc:
        raise HTTPException(404, f"Doc {doc_id} not found")
    if not _can_see_doc(auth, doc):
        raise HTTPException(404, "Doc not found")
    _require_doc_editor(auth, doc)
    head = db.query_one("SELECT body FROM doc_revisions WHERE doc_id = ? AND rev = ?",
                        (doc_id, doc["current_rev"]))
    base = (head["body"] if head else "") or ""
    new_body = (base.rstrip("\n") + req.separator + req.block) if base.strip() else req.block
    return _commit_doc_revision(doc, new_body, doc["title"], auth.identity(), req.note,
                                session_id=_session_of(x_session_id))


@app.post("/api/v1/docs/{doc_id}/comments")
def add_doc_comment(doc_id: int, req: DocComment, auth: AuthContext = Depends(get_auth),
                    x_session_id: Optional[str] = Header(None)):
    """Comment on a doc — the discussion layer, distinct from the versioned body.
    Anyone who can see the doc may comment; author is bearer-derived.
    Commenting claims the doc thread for your warm surface (#392)."""
    doc = db.query_one("SELECT * FROM docs WHERE id = ?", (doc_id,))
    if not doc:
        raise HTTPException(404, f"Doc {doc_id} not found")
    if not _can_see_doc(auth, doc):
        raise HTTPException(404, "Doc not found")
    author = auth.identity()
    if not req.body.strip():
        raise HTTPException(400, "empty comment body")
    _replay_guard("doc_comments", "doc_id", doc_id, req.body)
    provenance = _writer_provenance(author, _session_of(x_session_id))
    comment_id = db.execute(
        "INSERT INTO doc_comments (doc_id, author, body, session_fp, home_thread_id) VALUES (?, ?, ?, ?, ?)",
        (doc_id, author, req.body, provenance["session_fp"], provenance["home_thread_id"]))
    db.execute("UPDATE docs SET updated_at = datetime('now') WHERE id = ?", (doc_id,))
    claim = _claim_thread(author, doc_id=doc_id, session_id=_session_of(x_session_id))
    _bind_conversation("doc", doc_id, author, _session_of(x_session_id))
    _dbody = _notif_body(req.body, f"GET /api/v1/docs/{doc_id}")
    # mentions first: the mention row is the one a newcomer is born from, and
    # the comment fan-out below skips everyone it already reached (0.12.0)
    notes = []
    _dmentioned = _notify_doc_mentions(doc_id, req.body, author, doc["slug"], doc["title"], comment_id=comment_id, notes=notes)
    _notify_doc_event(doc_id, "doc.commented", author,
                      {"comment_id": comment_id, "body": _dbody,
                       "slug": doc["slug"], "title": doc["title"],
                       "mentions": sorted(_dmentioned)},
                      session_id=_session_of(x_session_id))
    event_bus.emit("doc.commented", f"doc:{doc_id}",
                   {"id": doc_id, "comment_id": comment_id, "author": author,
                    "body": _dbody, **provenance}, scope=f"doc:{doc_id}")
    out = {"status": "commented", "doc_id": doc_id, "comment_id": comment_id, **provenance,
           "subscribed": bool(db.query_one(
               "SELECT 1 FROM doc_watchers WHERE doc_id = ? AND identity = ?",
               (doc_id, author))), "claim_owned": claim["owned"],
           "watch": _DOC_WATCH_HINT}
    if notes:
        out["notes"] = notes
    return out


@app.post("/api/v1/docs/{doc_id}/links")
def add_doc_link(doc_id: int, req: DocLinkCreate,
                 x_session_id: Optional[str] = Header(None),
                 auth: AuthContext = Depends(get_auth)):
    """Thread a doc to a ticket / service / device / doc. Any contributor who
    can see the doc may add a link; removal is owner/admin."""
    doc = db.query_one("SELECT * FROM docs WHERE id = ?", (doc_id,))
    if not doc:
        raise HTTPException(404, f"Doc {doc_id} not found")
    if not _can_see_doc(auth, doc):
        raise HTTPException(404, "Doc not found")
    _require_doc_editor(auth, doc)
    tt, tid = req.target_type, req.target_id
    if tt not in _DOC_LINK_TYPES:
        raise HTTPException(400, f"target_type must be one of {', '.join(_DOC_LINK_TYPES)}")
    # The target must exist AND be readable by the linker (scope v1): a link
    # is a read of the target's existence, and a ticket link tells that
    # ticket's watchers the doc's title.
    visible = _visible_device_ids(auth)
    checks = {
        "ticket": lambda: tid.isdigit() and (lambda t: t and _can_see_ticket(auth, t))(
            db.query_one("SELECT * FROM tickets WHERE id = ?", (int(tid),))),
        "service": lambda: (lambda sv: sv and (visible is None or sv.get("device_id") in visible or auth.can_read(tid)))(
            db.query_one("SELECT * FROM services WHERE id = ?", (tid,))),
        "device": lambda: db.query_one("SELECT id FROM devices WHERE id = ?", (tid,)) and (visible is None or tid in visible),
        "doc": lambda: (lambda d: d and _can_see_doc(auth, d))(
            db.query_one("SELECT * FROM docs WHERE id = ?", (int(tid),)) if tid.isdigit()
            else db.query_one("SELECT * FROM docs WHERE slug = ?", (tid,))),
    }
    if not checks[tt]():
        raise HTTPException(400, f"Unknown {tt} '{tid}' — link target must exist.")
    db.execute("INSERT OR IGNORE INTO doc_links (doc_id, target_type, target_id, created_by) "
               "VALUES (?, ?, ?, ?)", (doc_id, tt, tid, auth.identity()))
    _bind_conversation("doc", doc_id, auth.identity(), _session_of(x_session_id))
    # reach: linking a doc to a ticket tells that ticket's watchers — bridges the
    # doc into the ticket notification graph (so doc #1↔#100 would reach #100's followers).
    if tt == "ticket":
        _notify_ticket_event(int(tid), "doc.linked", auth.identity(),
                             {"doc_id": doc_id, "slug": doc["slug"], "title": doc["title"]})
    return {"status": "linked", "doc_id": doc_id, "target_type": tt, "target_id": tid,
            "links": _doc_links(doc_id)}


@app.delete("/api/v1/docs/{doc_id}/links/{link_id}")
def delete_doc_link(doc_id: int, link_id: int, auth: AuthContext = Depends(get_auth)):
    doc = db.query_one("SELECT * FROM docs WHERE id = ?", (doc_id,))
    if not doc:
        raise HTTPException(404, f"Doc {doc_id} not found")
    if not _doc_is_owner(auth, doc) and not groupscope.can_manage(auth, doc.get("group_scope")):
        raise HTTPException(403, "Only the doc's author, an owner of its group, or an admin can remove links")
    db.execute("DELETE FROM doc_links WHERE id = ? AND doc_id = ?", (link_id, doc_id))
    return {"status": "unlinked", "link_id": link_id, "doc_id": doc_id}


@app.post("/api/v1/docs/{doc_id}/subscribe")
def subscribe_doc(doc_id: int, auth: AuthContext = Depends(get_auth)):
    """Watch a doc — a notification row for every revision/update."""
    doc = db.query_one("SELECT * FROM docs WHERE id = ?", (doc_id,))
    if not doc:
        raise HTTPException(404, f"Doc {doc_id} not found")
    if not _can_see_doc(auth, doc):
        raise HTTPException(404, "Doc not found")
    _subscribe_doc(doc_id, auth.identity(), explicit=True, updates=True)
    return {"status": "subscribed", "doc_id": doc_id, "watch": _DOC_WATCH_HINT}


@app.delete("/api/v1/docs/{doc_id}/subscribe")
def unsubscribe_doc(doc_id: int, auth: AuthContext = Depends(get_auth)):
    """Mirror of the ticket unsubscribe: mutes too (#83), so revising or
    commenting the doc later doesn't silently re-enroll you."""
    me = auth.identity()
    db.execute("DELETE FROM doc_watchers WHERE doc_id = ? AND identity = ?",
               (doc_id, me))
    db.execute("INSERT OR IGNORE INTO thread_mutes (identity, doc_id) VALUES (?, ?)",
               (me, doc_id))
    return {"status": "unsubscribed", "doc_id": doc_id, "muted": True}


@app.get("/api/v1/docs/{doc_id}/watchers")
def list_doc_watchers(doc_id: int, auth: AuthContext = Depends(get_auth)):
    doc = db.query_one("SELECT * FROM docs WHERE id = ?", (doc_id,))
    if not doc:
        raise HTTPException(404, f"Doc {doc_id} not found")
    if not _can_see_doc(auth, doc):
        raise HTTPException(404, "Doc not found")
    rows = db.query("SELECT identity, created_at, updates FROM doc_watchers "
                    "WHERE doc_id = ? ORDER BY created_at", (doc_id,))
    return {"doc_id": doc_id, "watchers": rows}


# --- Task plane (doc #13, ticket #148) ---
# Projects group tasks; tasks nest via parent_task_id. Visibility rides the
# same group-scope predicate as docs; rollups are computed on read. Deliberate
# noise posture: no watcher fan-out — the only durable notification a task ever
# emits is to its assignee (assignment is addressed, everything else is quiet).

_PROJECT_STATUSES = {"planning", "active", "paused", "done", "archived"}
_TASK_STATUSES = {"todo", "in_progress", "blocked", "done", "cancelled"}


def _can_see_project(auth: AuthContext, project: dict) -> bool:
    """Scope v1 (doc 261): the admin, the author, or a member of the project's
    group; its tasks ride it."""
    return groupscope.object_readable(auth, project, owner_cols=("source",))

def _identity_can_see_project(identity: str, project: dict) -> bool:
    """Row-level mirror of _can_see_project for the task plane's fan-out
    (scope v1 — before it the task plane had no scope test at all, doc 262 §3)."""
    return groupscope.identity_object_readable(identity, project, owner_cols=("source",))



def _project_is_owner(auth: AuthContext, project: dict) -> bool:
    """Lifecycle gate (PATCH/DELETE): owner or admin. Task writes stay open to
    anyone in scope (agents collaborate on a project's tasks)."""
    if auth.is_admin():
        return True
    source = project.get("source", "")
    return bool(source) and source in (auth.owner_id, auth.identity())

def _require_project_editor(auth: AuthContext, project: dict, what: str = "task") -> None:
    """A task is made or changed by an admin, the project's author, or an
    editor of the project's group (scope v1); a viewer reads and comments."""
    if not groupscope.object_editable(auth, project, owner_cols=("source",)):
        role = groupscope.role_in(auth, project.get("group_scope"))
        raise HTTPException(403, f"You are a {role} in group '{project.get('group_scope')}'; "
                                 f"creating or changing a {what} there needs an editor.")



def _validate_project_scope(auth: AuthContext, group_scope):
    """Write-side twin of the read predicate — one resolver for every kind (scope v1)."""
    if group_scope:
        _resolve_group(auth, group_scope, "project")


def _resolve_project(ident: str) -> Optional[dict]:
    """Fetch a project by numeric id or slug."""
    if str(ident).isdigit():
        return db.query_one("SELECT * FROM projects WHERE id = ?", (int(ident),))
    return db.query_one("SELECT * FROM projects WHERE slug = ?", (ident,))


def _unique_project_slug(base: str) -> str:
    slug, n = base, 1
    while db.query_one("SELECT id FROM projects WHERE slug = ?", (slug,)):
        n += 1
        slug = f"{base}-{n}"
    return slug


def _project_rollup(project_id: int) -> dict:
    """Progress computed on read, never stored. progress_pct is estimate-weighted
    when estimates exist, else a plain done-count ratio. Cancelled tasks are
    excluded from the denominator."""
    rows = db.query("SELECT status, estimate_min FROM tasks WHERE project_id = ?",
                    (project_id,))
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    live = [r for r in rows if r["status"] != "cancelled"]
    total_est = sum(r["estimate_min"] or 0 for r in live)
    if live and total_est > 0:
        done_est = sum(r["estimate_min"] or 0 for r in live if r["status"] == "done")
        pct = round(100 * done_est / total_est)
        basis = "estimate-weighted"
    elif live:
        pct = round(100 * sum(1 for r in live if r["status"] == "done") / len(live))
        basis = "count"
    else:
        pct, basis = 0, "empty"
    return {"task_count": len(rows), "by_status": counts,
            "progress_pct": pct, "progress_basis": basis,
            "estimate_total_min": total_est or None}


def _task_tree(project_id: int) -> list[dict]:
    """All of a project's tasks as a nested tree (children under parents),
    siblings ordered by position (nulls last), then id. The legible UI shape."""
    rows = db.query("SELECT * FROM tasks WHERE project_id = ? "
                    "ORDER BY (position IS NULL), position, id", (project_id,))
    by_id = {r["id"]: dict(r, subtasks=[]) for r in rows}
    roots = []
    for r in rows:
        node = by_id[r["id"]]
        parent = by_id.get(r["parent_task_id"])
        (parent["subtasks"] if parent else roots).append(node)
    return roots


def _notify_task_assignment(task_id: int, assignee: str, actor: str, payload: dict):
    """One durable inbox row to the assignee (skip self-assignment). The only
    notification the task plane emits BY DEFAULT — assignment is addressed,
    the rest is quiet unless someone opted into the project (#232)."""
    if not assignee or assignee == actor:
        return
    project = db.query_one("SELECT p.* FROM projects p JOIN tasks t ON t.project_id = p.id WHERE t.id = ?",
                           (task_id,)) or {}
    if project and not _identity_can_see_project(assignee, project):   # scope v1: refused upstream; never leak here
        return
    db.execute("INSERT INTO notifications (recipient, task_id, event_type, actor, payload) "
               "VALUES (?, ?, 'task.assigned', ?, ?)",
               (assignee, task_id, actor, json.dumps(payload)))


def _notify_project_watchers(project_id: int, task_id: int, event_type: str,
                             actor: str, payload: dict, skip=None):
    """Opt-in fan-out (#232): durable inbox rows for a project's watchers,
    skipping the actor (and `skip` — a username or a set of usernames already
    notified for this same mutation, e.g. an assignee or thread participants).
    No watchers — the default — means no rows: the task plane's quietness is
    preserved."""
    skip_set = {skip} if isinstance(skip, str) else set(skip or ())
    watchers = db.query("SELECT identity FROM project_watchers WHERE project_id = ?",
                        (project_id,))
    project = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,)) or {}
    for w in watchers:
        r = w["identity"]
        if r in skip_set:
            continue
        if not _identity_can_see_project(r, project):    # scope v1: a row follows the project
            continue
        addressed = _sibling_notification("task", task_id, actor, payload) if r == actor else payload
        if addressed is None:
            continue
        db.execute("INSERT INTO notifications (recipient, task_id, event_type, actor, payload) "
                   "VALUES (?, ?, ?, ?, ?)", (r, task_id, event_type, actor, json.dumps(addressed)))


@app.get("/api/v1/projects")
def list_projects(status: str = None, group_scope: str = None,
                  auth: AuthContext = Depends(get_auth)):
    """Projects visible to the caller, each with its computed rollup."""
    clauses, params = [], []
    if status:
        clauses.append("status = ?"); params.append(status)
    if group_scope:
        clauses.append("group_scope = ?"); params.append(group_scope)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    projects = db.query(f"SELECT * FROM projects{where} ORDER BY updated_at DESC LIMIT 100",
                        tuple(params))
    visible = [p for p in projects if _can_see_project(auth, p)]
    for p in visible:
        p["rollup"] = _project_rollup(p["id"])
    return {"projects": visible}


@app.get("/api/v1/projects/{ident}")
def get_project(ident: str, auth: AuthContext = Depends(get_auth)):
    """Project by id or slug — rollup + full nested task tree."""
    project = _resolve_project(ident)
    if not project:
        raise HTTPException(404, f"Project '{ident}' not found")
    if not _can_see_project(auth, project):
        raise HTTPException(404, "Project not found")
    project["rollup"] = _project_rollup(project["id"])
    project["tasks"] = _task_tree(project["id"])
    return project


@app.post("/api/v1/projects")
def create_project(req: ProjectCreate, auth: AuthContext = Depends(get_auth)):
    if req.status not in _PROJECT_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(_PROJECT_STATUSES)}")
    group = _resolve_group(auth, req.group_scope, "project")
    source = auth.identity()
    slug = _unique_project_slug(_slugify(req.slug or req.name))
    project_id = db.execute(
        "INSERT INTO projects (slug, name, description, status, group_scope, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (slug, req.name, req.description, req.status, group, source))
    db.audit(source, "project.create", str(project_id), {"slug": slug, "name": req.name})
    event_bus.emit("project.created", f"project:{project_id}",
                   {"id": project_id, "slug": slug, "name": req.name, "source": source},
                   scope=f"project:{project_id}")
    return {"status": "created", "id": project_id, "project_id": project_id, "slug": slug, "group_scope": group}


@app.post("/api/v1/projects/{ident}/subscribe")
def subscribe_project(ident: str, auth: AuthContext = Depends(get_auth)):
    """Opt into a project's task events (#232): task.created / task.updated /
    task.delegated land in your durable inbox. The task plane stays quiet for
    everyone else — watching is a choice, the producer-surface primitive."""
    project = _resolve_project(ident)
    if not project:
        raise HTTPException(404, f"Project '{ident}' not found")
    if not _can_see_project(auth, project):
        raise HTTPException(404, "Project not found")
    me = auth.identity()
    db.execute("INSERT OR IGNORE INTO project_watchers (project_id, identity) "
               "VALUES (?, ?)", (project["id"], me))
    db.audit(me, "project.subscribe", str(project["id"]), {"slug": project["slug"]})
    return {"status": "subscribed", "project_id": project["id"],
            "slug": project["slug"],
            "hint": "task.created/updated/delegated now land in your inbox; "
                    "DELETE this endpoint to stop."}


@app.delete("/api/v1/projects/{ident}/subscribe")
def unsubscribe_project(ident: str, auth: AuthContext = Depends(get_auth)):
    project = _resolve_project(ident)
    if not project:
        raise HTTPException(404, f"Project '{ident}' not found")
    me = auth.identity()
    db.execute("DELETE FROM project_watchers WHERE project_id = ? AND identity = ?",
               (project["id"], me))
    return {"status": "unsubscribed", "project_id": project["id"]}


@app.patch("/api/v1/projects/{ident}")
def update_project(ident: str, req: ProjectUpdate, auth: AuthContext = Depends(get_auth)):
    """Lifecycle/metadata — owner or admin only."""
    project = _resolve_project(ident)
    if not project:
        raise HTTPException(404, f"Project '{ident}' not found")
    if not _can_see_project(auth, project):
        raise HTTPException(404, "Project not found")
    if not _project_is_owner(auth, project) and not groupscope.can_manage(auth, project.get("group_scope")):
        raise HTTPException(403, "Only the project's author, an owner of its group, or an admin can update a project")
    if req.status and req.status not in _PROJECT_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(_PROJECT_STATUSES)}")
    if req.group_scope is not None:
        _resolve_group(auth, req.group_scope, "project")   # "" is refused: a project is always in a group
    updates, params = [], []
    for field in ("name", "description", "status", "group_scope"):
        val = getattr(req, field)
        if val is not None:
            updates.append(f"{field} = ?"); params.append(val or None)
    if updates:
        updates.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(project["id"])
        db.execute(f"UPDATE projects SET {', '.join(updates)} WHERE id = ?", tuple(params))
    db.audit(auth.identity(), "project.update", str(project["id"]),
             {f: getattr(req, f) for f in ("name", "status", "group_scope")
              if getattr(req, f) is not None})
    return {"status": "updated", "project_id": project["id"]}


@app.delete("/api/v1/projects/{ident}")
def delete_project(ident: str, auth: AuthContext = Depends(get_auth)):
    """Permanent (owner/admin) — cascades tasks. Prefer PATCH status=archived."""
    project = _resolve_project(ident)
    if not project:
        raise HTTPException(404, f"Project '{ident}' not found")
    if not _project_is_owner(auth, project):
        raise HTTPException(403, "Only the project owner or admin can delete a project")
    with db.transaction() as conn:
        conn.execute("UPDATE notifications SET task_id = NULL WHERE task_id IN "
                     "(SELECT id FROM tasks WHERE project_id = ?)", (project["id"],))
        conn.execute("DELETE FROM task_comments WHERE task_id IN "
                     "(SELECT id FROM tasks WHERE project_id = ?)", (project["id"],))
        conn.execute("DELETE FROM project_watchers WHERE project_id = ?", (project["id"],))
        conn.execute("DELETE FROM tasks WHERE project_id = ?", (project["id"],))
        conn.execute("DELETE FROM projects WHERE id = ?", (project["id"],))
        db.audit(auth.identity(), "project.delete", str(project["id"]),
                 {"slug": project["slug"]}, conn=conn)
    return {"status": "deleted", "project_id": project["id"]}


def _load_task_and_project(task_id: int, auth: AuthContext) -> tuple[dict, dict]:
    task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task {task_id} not found")
    project = db.query_one("SELECT * FROM projects WHERE id = ?", (task["project_id"],))
    if not _can_see_project(auth, project):
        raise HTTPException(404, "Task not found")
    return task, project


def _validate_task_fields(status: Optional[str], parent_task_id: Optional[int],
                          project_id: int, assignee: Optional[str],
                          self_id: Optional[int] = None):
    if status and status not in _TASK_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(_TASK_STATUSES)}")
    if parent_task_id is not None:
        if parent_task_id == self_id:
            raise HTTPException(400, "A task cannot be its own parent")
        parent = db.query_one("SELECT project_id FROM tasks WHERE id = ?", (parent_task_id,))
        if not parent:
            raise HTTPException(400, f"parent_task_id {parent_task_id} not found")
        if parent["project_id"] != project_id:
            raise HTTPException(400, "parent_task_id must belong to the same project")
    if assignee:
        _validate_ticket_addressing(None, None, assignee)  # same validated namespace


@app.post("/api/v1/projects/{ident}/tasks")
def create_task(ident: str, req: ProjectTaskCreate, auth: AuthContext = Depends(get_auth),
                x_session_id: Optional[str] = Header(None),
                x_thread_id: Optional[str] = Header(None)):
    """Add a task (or subtask via parent_task_id) — open to anyone in scope.
    A create carrying X-Session-Id binds the creator's conversation (doc 174:
    "threads you start are yours"), as a ticket create does — staging #105
    found that an instance which created a task heard nothing back until it
    wrote on the task itself."""
    project = _resolve_project(ident)
    if not project:
        raise HTTPException(404, f"Project '{ident}' not found")
    if not _can_see_project(auth, project):
        raise HTTPException(404, "Project not found")
    _require_project_editor(auth, project)
    _validate_task_fields(req.status, req.parent_task_id, project["id"], req.assignee)
    _refuse_outsider_assignee(req.assignee, project.get("group_scope"), "task")
    source = auth.identity()
    task_id = db.execute(
        "INSERT INTO tasks (project_id, parent_task_id, title, detail, status, priority, "
        "estimate_min, assignee, due, position, source_ref, source, tags, "
        "scheduled_at, recurrence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project["id"], req.parent_task_id, req.title, req.detail, req.status, req.priority,
         req.estimate_min, req.assignee, req.due, req.position, req.source_ref, source,
         _norm_tags(req.tags), req.scheduled_at, req.recurrence))
    _create_in_thread("task", task_id, source, _session_of(x_session_id), x_thread_id,
                      req.parent_task_id)                                         # #886
    db.audit(source, "task.create", str(task_id),
             {"project": project["slug"], "title": req.title})
    _bind_conversation("task", task_id, source, _session_of(x_session_id))
    _notify_task_assignment(task_id, req.assignee, source,
                            {"title": req.title, "project": project["slug"],
                             "detail": _notif_body(req.detail, f"GET /api/v1/tasks/{task_id}")})
    _notify_project_watchers(project["id"], task_id, "task.created", source,
                             {"title": req.title, "project": project["slug"],
                              "status": req.status, "assignee": req.assignee},
                             skip=req.assignee)
    event_bus.emit("task.created", f"task:{task_id}",
                   {"id": task_id, "project_id": project["id"], "title": req.title,
                    "assignee": req.assignee, "source": source},
                   scope=f"project:{project['id']}")
    return {"status": "created", "id": task_id, "task_id": task_id,
            "project_id": project["id"], "thread_id": _thread_of("task", task_id),
            "ref": _ref("tasks", task_id, req.title)}


@app.get("/api/v1/tasks")
def list_tasks(project: str = None, status: str = None, assignee: str = None,
               parent: int = None, tag: str = None,
               auth: AuthContext = Depends(get_auth)):
    """Flat task list across visible projects (?project= id|slug ?status ?assignee ?parent ?tag)."""
    clauses, params = [], []
    if project:
        p = _resolve_project(project)
        if not p:
            raise HTTPException(404, f"Project '{project}' not found")
        clauses.append("t.project_id = ?"); params.append(p["id"])
    if status:
        clauses.append("t.status = ?"); params.append(status)
    if assignee:
        clauses.append("t.assignee = ?"); params.append(assignee)
    if parent is not None:
        clauses.append("t.parent_task_id = ?"); params.append(parent)
    if tag:
        c, p2 = _tag_clause(tag)
        clauses.append("t." + c); params.append(p2)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = db.query(f"SELECT t.* FROM tasks t{where} "
                    "ORDER BY (t.position IS NULL), t.position, t.id LIMIT 500",
                    tuple(params))
    # Scope-filter through each task's parent project (cached per project).
    seen: dict[int, bool] = {}
    out = []
    for t in rows:
        pid = t["project_id"]
        if pid not in seen:
            proj = db.query_one("SELECT * FROM projects WHERE id = ?", (pid,))
            seen[pid] = bool(proj) and _can_see_project(auth, proj)
        if seen[pid]:
            t["ref"] = _ref("tasks", t["id"], t.get("title"))
            out.append(_row_tags(t))
    return {"tasks": out}


@app.get("/api/v1/tasks/{task_id}")
def get_task(task_id: int, auth: AuthContext = Depends(get_auth)):
    task, project = _load_task_and_project(task_id, auth)
    task["project_slug"] = project["slug"]
    task["subtasks"] = [_row_tags(s) for s in db.query(
        "SELECT * FROM tasks WHERE parent_task_id = ? "
        "ORDER BY (position IS NULL), position, id", (task_id,))]
    task["comments"] = db.query(
        "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at, id",
        (task_id,))
    task["ref"] = _ref("tasks", task_id, task.get("title"))
    task["claims"] = _claims_view(auth.identity(), task_id=task_id)
    return _row_tags(task)


@app.post("/api/v1/tasks/{task_id}/comments")
def add_task_comment(task_id: int, req: TaskCommentCreate,
                     auth: AuthContext = Depends(get_auth),
                     x_session_id: Optional[str] = Header(None)):
    """Comment on a task (#203 riff: tasks get the same conversational layer
    as tickets/docs). Notifies assignee, creator, named recipients and the
    project's opt-in watchers. Commenting alone does not subscribe its writer.
    Commenting claims the task thread for your warm surface (#392)."""
    task, project = _load_task_and_project(task_id, auth)
    author = auth.identity()
    if not req.body.strip():
        raise HTTPException(400, "empty comment body")
    _replay_guard("task_comments", "task_id", task_id, req.body)
    provenance = _writer_provenance(author, _session_of(x_session_id))
    comment_id = db.execute(
        "INSERT INTO task_comments (task_id, author, body, session_fp, home_thread_id) VALUES (?, ?, ?, ?, ?)",
        (task_id, author, req.body, provenance["session_fp"], provenance["home_thread_id"]))
    db.execute("UPDATE tasks SET updated_at = datetime('now') WHERE id = ?", (task_id,))
    claim = _claim_thread(author, task_id=task_id, session_id=_session_of(x_session_id))
    _bind_conversation("task", task_id, author, _session_of(x_session_id))
    payload = {"title": task["title"], "project": project["slug"],
               "comment_id": comment_id,
               "body": _notif_body(req.body, f"GET /api/v1/tasks/{task_id}"),
               "mentions": sorted(_parse_mentions(req.body, exclude=author))}   # whom it addresses (0.12.2)
    payload = _comment_payload("task", payload)
    # Object responsibility and explicit project subscriptions carry replies;
    # a prior comment alone is not a standing audience grant.
    participants = {task.get("assignee"), task.get("source")}
    participants.discard(None)
    # Scope v1 (doc 262 §3): the task plane delivered to anyone; now a row
    # follows the project — a participant or a mention who cannot read the
    # project gets nothing, and an outsider mention is noted to the writer.
    readable = lambda who: _identity_can_see_project(who, project)
    for who in sorted(participants):
        if not readable(who):
            continue
        addressed = _sibling_notification("task", task_id, author, payload) if who == author else payload
        if addressed is None:
            continue
        db.execute("INSERT INTO notifications (recipient, task_id, event_type, actor, payload) "
                   "VALUES (?, ?, 'task.commented', ?, ?)",
                   (who, task_id, author, json.dumps(addressed)))
    # @mentions (v0.7.48, parity with docs): the direct reach path when the
    # person you need is neither participant nor watcher — "flagging to an admin"
    # must be able to actually reach the admin. Distinct event type, never doubled.
    notes = []
    mentioned = []
    for u in _parse_mentions(req.body, exclude=author):
        if u in participants:
            continue
        if readable(u):
            mentioned.append(u)
        else:
            notes.append(groupscope.note_for_outsider(u, project.get("group_scope")))
    mpayload = dict(payload)
    mpayload["mentions"] = sorted(mentioned)
    root = _root_payload("task", task_id)          # the work itself, for a newcomer (0.12.0)
    if root:
        mpayload["root"] = root
    for who in mentioned:
        db.execute("INSERT INTO notifications (recipient, task_id, event_type, actor, payload) "
                   "VALUES (?, ?, 'task.mentioned', ?, ?)",
                   (who, task_id, author, json.dumps(mpayload)))
    _notify_project_watchers(project["id"], task_id, "task.commented", author,
                             payload, skip=participants | set(mentioned))
    event_bus.emit("task.commented", f"task:{task_id}",
                   {"id": task_id, "comment_id": comment_id, "author": author,
                    "body": payload["body"], **provenance}, scope=f"project:{project['id']}")
    out = {"status": "commented", "task_id": task_id, "comment_id": comment_id, **provenance,
           "claim_owned": claim["owned"]}
    if notes:
        out["notes"] = notes
    return out


def _follow_parent(task_id: int, parent_id: int) -> Optional[str]:
    """A subtask lives in its parent's thread (#886): setting a parent moves
    the task there. The exception is a task that roots a thread other work
    has joined — moving it would leave that work in a thread whose root has
    gone elsewhere, so it stays, and the answer says why."""
    ptid, mine = _thread_of("task", parent_id), _thread_of("task", task_id)
    if not ptid or ptid == mine:
        return None
    if mine and _thread_root(mine) == ("task", task_id):
        others = sum(db.query_one(f"SELECT COUNT(*) AS n FROM {tbl} WHERE thread_id = ? "
                                  f"AND NOT (? = 'task' AND id = ?)", (mine, kind, task_id))["n"]
                     for kind, (tbl, _t) in _THREAD_TABLES.items())
        if others:
            return (f"task {task_id} roots thread {mine}, which {others} other object(s) joined, "
                    f"so it stays there; thread {ptid} is its parent's. Move the members with "
                    f"POST /api/v1/threads/{ptid}/members if that work belongs with the parent.")
    _place_in_thread("task", task_id, ptid)
    return f"moved into thread {ptid}, its parent's"


@app.patch("/api/v1/tasks/{task_id}")
def update_task(task_id: int, req: ProjectTaskUpdate, auth: AuthContext = Depends(get_auth),
                x_session_id: Optional[str] = Header(None)):
    """Edit a task — open to anyone in the project's scope (agents collaborate).
    status=done stamps completed_at; a new assignee gets the one inbox row.
    Editing claims the task thread for your warm surface (#392) — the exact
    race this closes: a cold wake and a live session were both structurally
    eligible for the same task's deliverables."""
    task, project = _load_task_and_project(task_id, auth)
    me = auth.identity()
    status_only = req.status is not None and all(
        getattr(req, f) is None for f in ("title", "detail", "parent_task_id", "priority", "estimate_min",
                                          "actual_min", "assignee", "due", "position", "source_ref"))
    if not (status_only and task.get("assignee") in (me, auth.owner_id)):
        _require_project_editor(auth, project)      # the assignee may drive status, as on tickets
    _validate_task_fields(req.status, req.parent_task_id, project["id"], req.assignee,
                          self_id=task_id)
    if req.assignee is not None:
        _refuse_outsider_assignee(req.assignee, project.get("group_scope"), "task")
    changed = [field for field in
               ("title", "detail", "parent_task_id", "status", "priority", "estimate_min",
                "actual_min", "assignee", "due", "position", "source_ref", "tags",
                "scheduled_at", "recurrence")
               if getattr(req, field, None) is not None]
    updates, params = [], []
    for field in ("title", "detail", "parent_task_id", "status", "priority",
                  "estimate_min", "actual_min", "assignee", "due", "position", "source_ref"):
        val = getattr(req, field)
        if val is not None:
            updates.append(f"{field} = ?"); params.append(val)
    if req.tags is not None:
        updates.append("tags = ?"); params.append(_norm_tags(req.tags))
    # Schedule fields: "" clears (the generic loop above treats None as
    # absent, so clearing needs an explicit sentinel — doc 161 §5).
    for field in ("scheduled_at", "recurrence"):
        val = getattr(req, field)
        if val is not None:
            updates.append(f"{field} = ?"); params.append(val or None)
    if req.status == "done" and task["status"] != "done":
        updates.append("completed_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
    if updates:
        updates.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(task_id)
        db.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", tuple(params))
    actor = auth.identity()
    thread_note = None
    if req.parent_task_id is not None and req.parent_task_id != task.get("parent_task_id"):
        thread_note = _follow_parent(task_id, req.parent_task_id)
    if req.assignee and req.assignee != task.get("assignee"):
        _notify_task_assignment(task_id, req.assignee, actor,
                                {"title": task["title"], "project": project["slug"]})
    skip = {req.assignee} if req.assignee and req.assignee != task.get("assignee") else set()
    record_changed = any(((_norm_tags(getattr(req, f)) if f == "tags" else
                           (getattr(req, f) or None) if f in ("scheduled_at", "recurrence") else
                           getattr(req, f)) != task.get(f)) for f in changed)
    if record_changed:
        # Record changes reach the responsible assignee and explicit project
        # subscribers, not everyone who commented. Own writes stay quiet.
        assignee = req.assignee or task.get("assignee")
        sid = _session_of(x_session_id)
        if assignee and assignee not in skip and _identity_can_see_project(assignee, project):
            if assignee != actor:
                payload = {"title": req.title or task["title"], "project": project["slug"],
                           "status": req.status or task["status"], "changed": changed}
                if _session_fp(sid):
                    payload["actor_session"] = _session_fp(sid)
                db.execute("INSERT INTO notifications (recipient, task_id, event_type, actor, payload) "
                           "VALUES (?, ?, 'task.updated', ?, ?)",
                           (assignee, task_id, actor, json.dumps(payload)))
                skip.add(assignee)
    if record_changed:
        _notify_project_watchers(project["id"], task_id, "task.updated", actor,
                                 {"title": task["title"], "project": project["slug"],
                                  "status": req.status or task["status"], "changed": changed},
                                 skip=skip)
    claim = _claim_thread(actor, task_id=task_id,
                          session_id=_session_of(x_session_id))
    _bind_conversation("task", task_id, actor, _session_of(x_session_id))
    event_bus.emit("task.updated", f"task:{task_id}",
                   {"id": task_id, "project_id": project["id"], "status": req.status,
                    "assignee": req.assignee, "actor": actor},
                   scope=f"project:{project['id']}")
    out = {"status": "updated", "task_id": task_id, "claim_owned": claim["owned"],
           "thread_id": _thread_of("task", task_id)}
    if thread_note:
        out["notes"] = [thread_note]
    return out


@app.delete("/api/v1/tasks/{task_id}")
def delete_task(task_id: int, auth: AuthContext = Depends(get_auth)):
    """Creator, project owner, or admin. Subtasks are re-rooted, not destroyed."""
    task, project = _load_task_and_project(task_id, auth)
    creator = task.get("source", "")
    if not (auth.is_admin() or _project_is_owner(auth, project)
            or (creator and creator in (auth.owner_id, auth.identity()))):
        raise HTTPException(403, "Only the task creator, project owner, or admin can delete a task")
    with db.transaction() as conn:
        conn.execute("UPDATE tasks SET parent_task_id = ? WHERE parent_task_id = ?",
                     (task.get("parent_task_id"), task_id))
        conn.execute("UPDATE notifications SET task_id = NULL WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM thread_claims WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_comments WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        db.audit(auth.identity(), "task.delete", str(task_id),
                 {"project": project["slug"], "title": task["title"]}, conn=conn)
    return {"status": "deleted", "task_id": task_id}


@app.post("/api/v1/tasks/{task_id}/delegate")
def delegate_task(task_id: int, req: TaskDelegate,
                  x_session_id: Optional[str] = Header(None),
                  auth: AuthContext = Depends(get_auth)):
    """Hand a task to an agent: files a ticket (the coordination plane) assigned
    to them, links it back, and flips the task to blocked — your waiting handle.
    Always explicit, never automatic.

    It files a whole ticket into the task's thread and fans out to two
    identities, and until 0.12.19 it took no conversation at all — so the
    delegator was not bound to the work it had just created and replies had
    nowhere to come back to (doc 239 D12: `X-Session-Id` on every write)."""
    task, project = _load_task_and_project(task_id, auth)
    if task.get("delegated_ticket_id"):
        raise HTTPException(409, f"Task already delegated as ticket "
                                 f"#{task['delegated_ticket_id']}")
    _validate_ticket_addressing(None, None, req.assignee)
    _refuse_outsider_assignee(req.assignee, project.get("group_scope"))
    source = auth.identity()
    body = (f"Delegated from task #{task_id} in project '{project['slug']}' "
            f"(task plane).\n\n**Task:** {task['title']}\n"
            + (f"\n{task['detail']}\n" if task.get("detail") else "")
            + (f"\n**Note from {source}:** {req.note}\n" if req.note else "")
            + f"\nWhen done, comment here — the task stays blocked as "
              f"{source}'s waiting handle until they mark it.")
    ticket_id = db.execute(
        "INSERT INTO tickets (source, type, priority, status, summary, body, assignee, group_scope) "
        "VALUES (?, 'task', ?, 'open', ?, ?, ?, ?)",
        (source, req.priority, f"[task] {task['title']}", body, req.assignee, project.get("group_scope")))
    _place_in_thread("ticket", ticket_id, _thread_of("task", task_id, source))   # v19: one thread
    _bind_conversation("ticket", ticket_id, source, _session_of(x_session_id))
    _subscribe_ticket(ticket_id, source)
    _subscribe_ticket(ticket_id, req.assignee, explicit=True)
    _notify_ticket_event(ticket_id, "ticket.created", source,
                         {"summary": f"[task] {task['title']}", "type": "task",
                          "priority": req.priority, "assignee": req.assignee,
                          "body": _notif_body(body, f"GET /api/v1/tickets/{ticket_id}")})
    db.execute("UPDATE tasks SET status = 'blocked', assignee = ?, "
               "delegated_ticket_id = ?, updated_at = ? WHERE id = ?",
               (req.assignee, ticket_id, datetime.now(timezone.utc).isoformat(), task_id))
    db.audit(source, "task.delegate", str(task_id),
             {"ticket_id": ticket_id, "assignee": req.assignee})
    _notify_project_watchers(project["id"], task_id, "task.delegated", source,
                             {"title": task["title"], "project": project["slug"],
                              "ticket_id": ticket_id, "assignee": req.assignee},
                             skip=req.assignee)
    event_bus.emit("task.delegated", f"task:{task_id}",
                   {"id": task_id, "ticket_id": ticket_id, "assignee": req.assignee,
                    "source": source},
                   scope=f"project:{project['id']}")
    return {"status": "delegated", "task_id": task_id, "ticket_id": ticket_id,
            "task_status": "blocked", "watch": _WATCH_HINT}


# --- Enrollment ---

@app.post("/api/v1/enroll")
def enroll_device(req: EnrollRequest, auth: AuthContext = Depends(get_auth)):
    """Enroll a new device. Requires admin or a group editor.

    Non-admins may join only groups they can edit; default to those groups.
    Access Route (phone/laptop): creates device + generates access key immediately.
    Access Target (server): creates device + returns bootstrap token + tunnel port.
    Both: does both.
    """
    if not auth.can_create_device():
        raise HTTPException(403, "Admin or group scope required to create devices")
    _validate_device_identity(req.device_id)
    editable_groups = [g for g in auth.get_group_scopes() if groupscope.can_edit(auth, g)]
    if not auth.is_admin() and not editable_groups:
        raise HTTPException(403, "An editor role in a group is required to enroll devices")
    join_groups = req.group_ids if req.group_ids is not None else editable_groups
    for group_id in join_groups:
        if not auth.is_admin() and group_id not in editable_groups:
            raise HTTPException(403, "Can only enroll into groups you can edit")
        if not db.query_one("SELECT 1 FROM groups WHERE id = ?", (group_id,)):
            raise HTTPException(400, "Unknown enrollment group")
    # Resolve compat aliases
    name = req.name or req.hostname or req.device_id
    access_route = req.access_route or req.inbound_method
    access_target = req.access_target or req.outbound_method
    # Validate loudly (#534 post-mortem): "ssh" used to pass silently and
    # produce a device that reports online but cannot be routed to — the
    # looks-enrolled-isn't shape. Compat alias the obvious spelling, 400 the rest.
    if access_target == "ssh":
        access_target = "direct-ssh"
    if access_target and access_target not in ("direct-ssh", "reverse-tunnel", "http"):
        raise HTTPException(400, f"Unknown access_target '{access_target}' — "
                                 f"use direct-ssh, reverse-tunnel, or http.")
    target_ip = req.target_ip or req.outbound_ip
    target_ssh_user = req.target_ssh_user or req.outbound_ssh_user

    # Check if device already exists
    existing = db.query_one("SELECT id, status FROM devices WHERE id = ?", (req.device_id,))
    if existing:
        raise HTTPException(409, f"Device {req.device_id} already exists (status: {existing['status']})")

    now = datetime.now(timezone.utc).isoformat()

    # Auto-allocate tunnel port for reverse-tunnel targets
    tunnel_port = None
    if access_target == "reverse-tunnel":
        try:
            tunnel_port = port_alloc.allocate("tunnel", req.device_id)
        except ValueError:
            raise HTTPException(409, "No tunnel ports available in range. Delete unused devices to free ports, or expand the tunnel range in ports.py.")
    elif _device_context(req.context) == "tunnel" and access_target:
        try:
            tunnel_port = port_alloc.allocate("tunnel", req.device_id)
        except ValueError:
            raise HTTPException(409, "No tunnel ports available in range. Delete unused devices to free ports, or expand the tunnel range in ports.py.")

    # Infer context from access_target if not specified
    context = _device_context(req.context) or ("tunnel" if access_target == "reverse-tunnel" else "samcloud")

    # Determine initial status:
    # - Route-only devices (phones) activate immediately (no bootstrap needed)
    # - Target devices stay pending until they call /activate with bootstrap token
    is_route_only = access_route and not access_target
    initial_status = "online" if is_route_only else "pending"
    initial_enrolled = 1 if is_route_only else 0

    with db.transaction() as conn:
        conn.execute("""
            INSERT INTO devices (id, name, hostname, context, type,
                inbound_method, outbound_method, outbound_ip, outbound_ssh_user, outbound_ssh_port,
                tunnel_port,
                enrolled, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (req.device_id, name, name, context, req.type,
              access_route, access_target, target_ip, target_ssh_user, req.target_ssh_port,
              tunnel_port,
              initial_enrolled, initial_status, now, now))

        if tunnel_port:
            conn.execute(
                "INSERT OR IGNORE INTO ports (port, type, owner_id) VALUES (?, 'tunnel', ?)",
                (tunnel_port, req.device_id),
            )

    public_url = os.environ.get("PUBLIC_URL", "http://localhost:7778")

    # Build response based on enrollment type
    result: dict = {
        "device_id": req.device_id,
        "name": name,
        "status": initial_status,
    }

    # Access targets need a bootstrap token for activation
    if access_target:
        enroll_token = enrollment.generate_enrollment_token(req.device_id, context)
        result["token"] = enroll_token
        result["bootstrap_url"] = f"{public_url}/api/v1/bootstrap/{enroll_token}"
        result["bootstrap_command"] = f"curl -s {public_url}/api/v1/bootstrap/{enroll_token} | bash"
        result["expires_in"] = 3600
        if tunnel_port:
            result["tunnel_port"] = tunnel_port
            # Where the device dials, as the bootstrap script fills it in, so a
            # manual tunnel names this plane's endpoint.
            result["tunnel"] = {"host": _tunnel_host(),
                                "port": int(_plane_ssh_port()),
                                "user": _plane_ssh_user()}

    # Access routes get access key setup info
    if access_route:
        result["access_key_url"] = f"{public_url}/api/v1/access-keys"
        result["pair_link_url"] = f"{public_url}/api/v1/access-keys/pair"
        result["access_key_command"] = f'curl -s -X POST {public_url}/api/v1/access-keys -H "Content-Type: application/json" -H @"$HOME/.samcloud/token.hdr" -d \'{{"username":"<your-username>","device_name":"{req.device_id}"}}\''

    # Join device to groups — explicit group_ids if provided, else caller's groups
    groups_joined = []
    for group_id in join_groups:
        group = db.query_one("SELECT id FROM groups WHERE id = ?", (group_id,))
        if group:
            db.execute(
                "INSERT OR IGNORE INTO group_devices (group_id, device_id) VALUES (?, ?)",
                (group_id, req.device_id),
            )
            groups_joined.append(group_id)

    db.audit(auth.owner_id, "device.enroll.created", req.device_id,
             {"name": name, "access_route": access_route, "access_target": access_target,
              "tunnel_port": tunnel_port, "status": initial_status, "groups": groups_joined})

    if groups_joined:
        result["groups"] = groups_joined

    # Next steps for the API consumer
    result["next_steps"] = []
    if access_target:
        result["next_steps"].append(f"Run bootstrap: {result['bootstrap_command']}")
    if access_route:
        result["next_steps"].append(
            f"Pair it: POST {public_url}/api/v1/access-keys/pair "
            f'{{"username":"<your-username>","device_name":"{req.device_id}"}} '
            f"-> run its pair_command on the device (single use, 1 h; the device needs no samcloud token)")
    result["next_steps"].append(f"Register services: POST {public_url}/api/v1/services")
    result["next_steps"].append(
        f'Enrolled != reachable — the above is network/SSH access only. For an agent on '
        f'this device to hear tickets/notifications/assigned work, give it its own identity '
        f'+ wake rail: POST {public_url}/api/v1/users/invite '
        f'(role "agent", scopes ["device:{req.device_id}"]) -> agent activates via '
        f'POST {public_url}/api/v1/users/activate -> '
        f'GET {public_url}/api/v1/helpers/wake-install | bash. '
        f'Not done until GET {public_url}/api/v1/wake/registrations shows it with stale:false.'
    )
    result["next_steps"].append(f"Full onboarding recipe (incl. wake rail): GET {public_url}/api/v1/devices/{req.device_id}/guide")
    result["next_steps"].append(f"API index: GET {public_url}/api/v1/")

    return result


def _bootstrap_tunnel_port(device: dict) -> str:
    """Value injected for __TUNNEL_PORT__. Empty when the device borrows another
    device's tunnel (transport_owner set) — the bootstrap then skips standing up
    a tunnel (the owner already maintains it) but still installs the key,
    authorized_keys, ssh config and device token. The device record keeps the
    mirrored tunnel_port for gateway routing; only the bootstrap is told 'none'."""
    if device.get("transport_owner"):
        return ""
    return str(device.get("tunnel_port") or "")


@app.get("/api/v1/bootstrap/{token}")
def serve_bootstrap_script(token: str, request: Request):
    """Serve the bootstrap script with template variables filled in.

    The token is looked up (NOT consumed) to find the device, then the
    bootstrap.sh template is rendered with the device's enrollment details.
    Token consumption happens when the script calls /api/v1/devices/activate.
    """
    if not token.startswith("sc_enroll_"):
        raise HTTPException(404, "Invalid bootstrap token")

    # Look up the token without consuming it
    token_hash = enrollment._hash_token(token)
    token_record = db.query_one(
        "SELECT * FROM tokens WHERE token_hash = ? AND type = 'enrollment'",
        (token_hash,),
    )
    if not token_record:
        raise HTTPException(404, "Token not found or expired")

    device_id = token_record["owner_id"]
    device = db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,))
    if not device:
        raise HTTPException(404, "Device not found")

    # Read bootstrap template
    script_path = Path(__file__).parent / "bootstrap.sh"
    if not script_path.exists():
        raise HTTPException(500, "Bootstrap script template not found")

    script = script_path.read_text()

    # Use PUBLIC_URL env if set (for external access), otherwise derive from request
    public_url = os.environ.get("PUBLIC_URL")
    if public_url:
        registry_url = public_url
    else:
        registry_url = f"{request.url.scheme}://{request.headers.get('host', 'localhost:7778')}"

    # SSH endpoint for tunnels — configurable for staging vs production
    plane_host = _tunnel_host()
    plane_ssh_port = _plane_ssh_port()
    plane_ssh_user = _plane_ssh_user()
    # Deploy env (ticket #106): namespaces device-side artifacts so prod + staging
    # can coexist on one machine. Default "prod" keeps the bare names (no migration);
    # the staging stack sets SAMCLOUD_ENV=staging in its env file.
    samcloud_env = os.environ.get("SAMCLOUD_ENV", "prod")

    # Fill template variables
    script = script.replace("__ENROLLMENT_TOKEN__", token)
    script = script.replace("__REGISTRY_URL__", registry_url)
    script = script.replace("__DEVICE_ID__", device_id)
    script = script.replace("__TUNNEL_PORT__", _bootstrap_tunnel_port(device))
    script = script.replace("__CONTEXT__", device.get("context", "samcloud"))
    script = script.replace("__PLANE_HOST__", plane_host)
    script = script.replace("__PLANE_SSH_PORT__", plane_ssh_port)
    script = script.replace("__PLANE_SSH_USER__", plane_ssh_user)
    script = script.replace("__ENV__", samcloud_env)

    return PlainTextResponse(script, media_type="text/x-shellscript")


@app.get("/api/v1/bootstrap/{token}/ps1")
def serve_bootstrap_powershell(token: str, request: Request):
    """Serve the PowerShell bootstrap script for native Windows."""
    if not token.startswith("sc_enroll_"):
        raise HTTPException(404, "Invalid bootstrap token")

    token_hash = enrollment._hash_token(token)
    token_record = db.query_one(
        "SELECT * FROM tokens WHERE token_hash = ? AND type = 'enrollment'",
        (token_hash,),
    )
    if not token_record:
        raise HTTPException(404, "Token not found or expired")

    device_id = token_record["owner_id"]
    device = db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,))
    if not device:
        raise HTTPException(404, "Device not found")

    script_path = Path(__file__).parent / "bootstrap.ps1"
    if not script_path.exists():
        raise HTTPException(500, "PowerShell bootstrap template not found")

    script = script_path.read_text()

    public_url = os.environ.get("PUBLIC_URL")
    if public_url:
        registry_url = public_url
    else:
        registry_url = f"{request.url.scheme}://{request.headers.get('host', 'localhost:7778')}"

    plane_host = _tunnel_host()
    plane_ssh_port = _plane_ssh_port()
    plane_ssh_user = _plane_ssh_user()
    samcloud_env = os.environ.get("SAMCLOUD_ENV", "prod")

    script = script.replace("__ENROLLMENT_TOKEN__", token)
    script = script.replace("__REGISTRY_URL__", registry_url)
    script = script.replace("__DEVICE_ID__", device_id)
    script = script.replace("__TUNNEL_PORT__", _bootstrap_tunnel_port(device))
    script = script.replace("__CONTEXT__", device.get("context", "samcloud"))
    script = script.replace("__PLANE_HOST__", plane_host)
    script = script.replace("__PLANE_SSH_PORT__", plane_ssh_port)
    script = script.replace("__PLANE_SSH_USER__", plane_ssh_user)
    script = script.replace("__ENV__", samcloud_env)

    return PlainTextResponse(script, media_type="text/plain")


# --- Agent skills (portable, registry-served) ---
#
# Skills are the executable form of the docs: an enrolled agent installs them
# into ~/.claude/skills/ and gains standing knowledge of how to operate in
# samcloud (tickets, services, leases, model gateway) using only its token.
# Bundled in the image under registry/skills/ (see Dockerfile.registry).

SKILLS_DIR = Path(__file__).parent / "skills"
# Public, no auth: skills are operational knowledge, not secrets. Mirrors the
# read-is-public stance of the API index and the docs.

def _safe_skill_name(name: str) -> str:
    """Reject path traversal / unexpected names. Skill names are [a-z0-9-]."""
    if not _re.fullmatch(r"[a-z0-9-]{1,64}", name):
        raise HTTPException(404, "Unknown skill")
    return name


@app.get("/api/v1/skills")
def list_skills():
    """List the agent skills the registry serves, with their descriptions."""
    if not SKILLS_DIR.is_dir():
        return {"skills": []}
    out = []
    for child in sorted(SKILLS_DIR.iterdir()):
        skill_md = child / "SKILL.md"
        if not (child.is_dir() and skill_md.exists()):
            continue
        # Pull name + description out of the YAML frontmatter cheaply.
        text = skill_md.read_text()
        name, desc = child.name, ""
        m = _re.search(r"^---\s*\n(.*?)\n---", text, _re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip()
        # Progressive disclosure (#588 follow-up): a skill is an entry point
        # plus optional chunks loaded on demand. List them so an installer can
        # fetch the whole set without guessing filenames.
        chunks = sorted(p.name for p in child.glob("*.md")
                        if p.name != "SKILL.md")
        out.append({"name": name, "dir": child.name, "description": desc,
                    "path": f"/api/v1/skills/{child.name}/SKILL.md",
                    "chunks": chunks})
    return {"skills": out}


@app.get("/api/v1/skills/install.sh")
def serve_skills_installer(request: Request):
    """Serve the skill installer, with the registry URL baked in.

    Onboarding usage:  curl -fsSL <registry>/api/v1/skills/install.sh | bash
    """
    script_path = SKILLS_DIR / "install.sh"
    if not script_path.exists():
        raise HTTPException(500, "Installer not found")
    script = script_path.read_text()

    public_url = os.environ.get("PUBLIC_URL")
    registry_url = public_url or f"{request.url.scheme}://{request.headers.get('host', 'localhost:7778')}"
    # Let the piped-from-curl invocation know where it came from without the
    # caller having to set SAMCLOUD_API first.
    script = script.replace("__REGISTRY_URL__", registry_url)
    # Every page of every served skill, so the installer fetches the chunks
    # beside each entry point, not the entry point alone.
    files = sorted(f"{d.name}/{p.name}" for d in SKILLS_DIR.iterdir() if (d / "SKILL.md").is_file()
                   for p in d.glob("*.md"))
    script = script.replace("__SKILL_FILES__", " ".join(files))
    return PlainTextResponse(script, media_type="text/x-shellscript")


def _admin_seat() -> str:
    """The seat served skill pages tell an agent to file problems to: SAMCLOUD_ADMIN_SEAT,
    else this plane's first admin user (the one /init made), else `admin`."""
    seat = os.environ.get("SAMCLOUD_ADMIN_SEAT", "").strip()
    if seat:
        return seat
    rows = db.query("SELECT username FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
    return rows[0]["username"] if rows else "admin"


def _render_skill(text: str, request: Request) -> str:
    """A skill page names this plane, its brokers and its repo, filled in here (#1010).

    The source carries placeholders, so the repo names no instance. A plane
    with SAMCLOUD_DOMAIN and SAMCLOUD_REPO set renders the URLs it always served.
    """
    public_url = os.environ.get("PUBLIC_URL") or \
        f"{request.url.scheme}://{request.headers.get('host', 'localhost:7776')}"
    for token, value in (("__API__", f"{public_url}/api/v1"),
                         ("__GH_BROKER__", _broker_url("GH_BROKER_URL", "ghb", "")),
                         ("__GOOGLE_BROKER__", _broker_url("GOOGLE_BROKER_URL", "gglb", "")),
                         ("__AWS_BROKER__", _broker_url("AWS_BROKER_URL", "awsb", "")),
                         ("__REPO__", os.environ.get("SAMCLOUD_REPO", "").strip() or "OWNER/samcloud"),
                         ("__ADMIN__", _admin_seat())):
        text = text.replace(token, value)
    return text


@app.get("/api/v1/skills/{name}/SKILL.md")
def serve_skill(name: str, request: Request):
    """Serve a single skill's SKILL.md body."""
    name = _safe_skill_name(name)
    skill_md = SKILLS_DIR / name / "SKILL.md"
    if not skill_md.exists():
        raise HTTPException(404, "Unknown skill")
    return PlainTextResponse(_render_skill(skill_md.read_text(), request), media_type="text/markdown")


@app.get("/api/v1/skills/{name}/{filename}")
def serve_skill_chunk(name: str, filename: str, request: Request):
    """Serve one of a skill's on-demand chunks.

    Progressive disclosure: SKILL.md is the entry point an agent always loads;
    the depth lives in sibling .md files it reads only when it needs them.
    Without this route those references dangle — the entry point would name
    files the fleet cannot fetch, which teaches nothing and looks like a
    working link. Markdown only, no traversal, same directory only.
    """
    name = _safe_skill_name(name)
    if not _re.fullmatch(r"[A-Za-z0-9_-]{1,64}\.md", filename or ""):
        raise HTTPException(404, "Unknown skill file")
    path = SKILLS_DIR / name / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Unknown skill file")
    return PlainTextResponse(_render_skill(path.read_text(), request), media_type="text/markdown")


@app.post("/api/v1/devices/activate")
def activate_device(req: ActivateRequest):
    """Called by bootstrap script with enrollment token.

    Validates token (single-use, not expired), updates device status to 'online',
    stores SSH pubkey, and generates a long-lived device token.
    """
    # Validate enrollment token
    token_record = enrollment.validate_enrollment_token(req.token)
    if not token_record:
        raise HTTPException(401, "Invalid or expired enrollment token")

    device_id = token_record["owner_id"]
    _validate_device_identity(device_id)

    # Verify device exists and is pending
    device = db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,))
    if not device:
        raise HTTPException(404, f"Device {device_id} not found")

    now = datetime.now(timezone.utc).isoformat()

    # Auto-detect outbound_ssh_user from device-reported `whoami` if admin didn't set one.
    # Only persist values OpenSSH will accept ([A-Za-z0-9._-]+); otherwise stash the
    # raw value as a warning so the admin can see why and pick a real account.
    detected_user = (req.ssh_user or "").strip()
    ssh_user_to_set = None
    ssh_user_warning = None
    if detected_user and not device.get("outbound_ssh_user"):
        if SSH_USER_RE.match(detected_user):
            ssh_user_to_set = detected_user
        else:
            ssh_user_warning = (
                f"Device reported login '{detected_user}' which OpenSSH rejects "
                f"(only [A-Za-z0-9._-] allowed). Set target_ssh_user explicitly."
            )

    # Update device: activate
    ssh_port_to_set = req.ssh_port if (req.ssh_port and req.ssh_port != 22) else None
    with db.transaction() as conn:
        conn.execute("""
            UPDATE devices SET status = 'online', enrolled = 1, enrolled_at = ?,
                ssh_pubkey = ?, startup_method = COALESCE(?, startup_method),
                outbound_ssh_user = COALESCE(?, outbound_ssh_user),
                outbound_ssh_port = COALESCE(?, outbound_ssh_port),
                ssh_user_warning = ?,
                last_seen = ?, updated_at = ?
            WHERE id = ?
        """, (now, req.ssh_pubkey, req.startup_method,
              ssh_user_to_set, ssh_port_to_set, ssh_user_warning,
              now, now, device_id))

    # Pin the host key the device reported, so the gateway verifies its FIRST hop
    # instead of learning whatever answers then (#821).
    #
    # Only on a device that has never activated: `status == 'pending'`, set only if
    # absent. Both halves are load-bearing, because activation is authorised by an
    # enrollment token and `/enroll/{id}/retoken` mints one for any *manager* of a
    # device (a `device:` scope, or a group holding it) — not just the admin. A
    # manager able to pin here could point the gateway at a key of their choosing
    # and capture other seats' sessions to a device others already use, which is
    # exactly the escalation POST /host-key refuses.
    #
    # `pending` is the signal that rules that out: it is set only by POST /enroll on
    # a device that has not called activate, retoken never clears it, and
    # POST /devices creates devices already 'online'. So an existing, activated
    # device can never be pinned down this path, whoever minted the token. A device
    # created purely to be enrolled has no pin to steal and no other seat hopping to
    # it. `device` was read before the UPDATE above, so this is the prior status.
    #
    # A re-bootstrap therefore never moves a pin; a real re-key goes through
    # DELETE /devices/{id}/host-key, which is admin-or-that-device.
    host_key_pinned = None
    if req.host_pubkey:
        host_pubkey = _parse_host_pubkey(req.host_pubkey)
        already_pinned = (device.get("host_pubkey") or "").strip()
        if already_pinned or device.get("status") != "pending":
            host_key_pinned = False
        else:
            written = db.execute_count(
                "UPDATE devices SET host_pubkey = ?, host_pubkey_recorded_at = ? "
                "WHERE id = ? AND (host_pubkey IS NULL OR host_pubkey = '')",
                (host_pubkey, now, device_id),
            )
            host_key_pinned = bool(written)
            if written:
                db.audit("system:enrollment", "device.host_key.pinned", device_id,
                         {"fingerprint": _host_key_fingerprint(host_pubkey),
                          "source": "enrollment"})

    # Generate long-lived device token
    device_token = enrollment.generate_device_token(device_id)

    db.audit("system:enrollment", "device.activated", device_id,
             {"startup_method": req.startup_method,
              "ssh_user": ssh_user_to_set,
              "ssh_user_warning": ssh_user_warning,
              "host_key_pinned": host_key_pinned})

    # Auto-sync authorized_keys so the device tunnel key is immediately usable
    sync_result = authorized_keys.sync()

    # Include gateway pubkey so bootstrap can install it on the device
    gateway_pub = None
    gw_pub_path = Path(f"{GATEWAY_KEY_PATH}.pub")
    if gw_pub_path.exists():
        gateway_pub = gw_pub_path.read_text().strip()

    resolved_ssh_user = ssh_user_to_set or device.get("outbound_ssh_user")

    return {
        "status": "enrolled",
        "device_id": device_id,
        "tunnel_port": device.get("tunnel_port"),
        "device_token": device_token,
        "gateway_pubkey": gateway_pub,
        "authorized_keys_synced": sync_result,
        "ssh_user": resolved_ssh_user,
        "ssh_user_warning": ssh_user_warning,
        # The gateway's HOST keys, for the device to verify the tunnel it dials, and
        # the alias to file them under. Distinct from `gateway_pubkey` above, which is
        # the gateway's CLIENT key — what this device puts in its authorized_keys.
        # Opposite directions; the names have to say so (#825).
        "gateway_host_keys": forward.sshd_host_pubkeys(),
        "gateway_host_key_alias": GATEWAY_HOST_KEY_ALIAS,
        # null = no key reported; true = pinned; false = a pin was already set, so
        # this one was not moved (a re-bootstrap never re-keys — see above).
        "host_key_pinned": host_key_pinned,
    }


# --- Device Helpers ---
# Post-enrollment scripts (WSL keepalive, etc.) served per-device with template
# vars filled in. Discoverability via GET /devices/{id}/helpers; each helper
# self-applies based on the device's recorded context/startup_method.

HELPERS = {
    "wsl-keepalive": {
        "filename": "wsl-keepalive.ps1",
        "media_type": "text/plain",
        # Surfaces on the WSL device — that's the thing being kept alive, so
        # that's where the operator looks for the helper. Execution context is
        # the Windows host that owns this distro: copy the URL and run it as
        # Administrator there. Holds the VM alive via a wsl.exe --exec handle,
        # catching shutdown / Defender kill / vmIdleTimeout / NIC crash alike.
        "description": "Holds this WSL distro alive on its Windows host. Copy the URL and run as Administrator on the Windows host that owns this distro (admin PowerShell: irm <url> | iex).",
        "applies": lambda dev: dev.get("startup_method") == "systemd",
    },
    "tunnel-debug": {
        "filename": "tunnel-debug.ps1",
        "media_type": "text/plain",
        "description": "Diagnose why the tunnel scheduled task isn't binding. Run as Administrator on the device (irm <url> | iex). Writes a verbose log of key ACL/owner + a one-shot ssh -v attempt to C:\\Windows\\Temp\\samcloud-tunnel-debug.log and tails the last 60 lines.",
        "applies": lambda dev: dev.get("startup_method") == "scheduled-task",
    },
}


def _render_helper(name: str, device: dict) -> str:
    spec = HELPERS[name]
    script = (Path(__file__).parent / "helpers" / spec["filename"]).read_text()
    script = script.replace("__DEVICE_ID__", device["id"])
    script = script.replace("__WSL_DISTRO__", os.environ.get("WSL_DISTRO_DEFAULT", "Ubuntu"))
    script = script.replace("__PLANE_HOST__", _tunnel_host())
    script = script.replace("__PLANE_SSH_PORT__", _plane_ssh_port())
    script = script.replace("__PLANE_SSH_USER__", _plane_ssh_user())
    return script


@app.get("/api/v1/devices/{device_id}/helpers")
def list_device_helpers(device_id: str, request: Request,
                        auth: AuthContext = Depends(get_auth)):
    """List post-enrollment helper scripts that apply to this device."""
    device = _require_visible_device(auth, device_id)

    public_url = os.environ.get("PUBLIC_URL") or \
        f"{request.url.scheme}://{request.headers.get('host', 'localhost:7778')}"

    return [
        {
            "name": name,
            "description": spec["description"],
            "filename": spec["filename"],
            "url": f"{public_url}/api/v1/devices/{device_id}/helpers/{name}",
        }
        for name, spec in HELPERS.items()
        if spec["applies"](device)
    ]


@app.get("/api/v1/devices/{device_id}/helpers/{name}")
def serve_device_helper(device_id: str, name: str, auth: AuthContext = Depends(get_auth)):
    """Serve a helper script with device-specific template vars filled in."""
    if name not in HELPERS:
        raise HTTPException(404, f"Unknown helper '{name}'")
    device = _require_visible_device(auth, device_id)
    spec = HELPERS[name]
    if not spec["applies"](device):
        raise HTTPException(409, f"Helper '{name}' does not apply to device {device_id}")
    return PlainTextResponse(_render_helper(name, device), media_type=spec["media_type"])


# --- Agent wake rail (RFC doc #17) ---
# Identity-scoped (not device-scoped) helpers: the cold-start wake poller, its
# installer, and the centrally-versioned wake-prompt contract. Served with
# auth so only enrolled identities can pull them; __API__ is filled from the
# serving environment so staging installs point at staging.

_AGENT_HELPERS = {
    "wake": "samcloud-wake.sh",          # default wake handler (contract session)
    "listen": "samcloud-listen.py",      # SSE trigger — the v2 rail itself
    # Rail v3 (doc 161): thread-held sessions — wake/resume/continue, no
    # watchdog. OPT-IN per seat via WAKE_CMD; deliberately a separate helper
    # so the v2 self-update path can never swap a seat onto it unasked.
    "rail": "samcloud-rail.py",
    # The OTHER holder (3.12.0): a seat whose runtime is not a Claude harness
    # cannot be fed on a FIFO, so the rail runs one adapter turn per delivery —
    # the runtime answers, the adapter posts what it said on the thread under
    # the seat's identity. Doc 265's contract, in a file.
    "adapter": "samcloud-adapter.py",
    "wake-install": "wake-install.sh",
    # The write primitive — the pen that matches the rail's read side (#87
    # c585 proto-CLI: body on stdin, bearer on its own fd, never argv).
    # Contributed by a seat, reviewed before serving.
    "say": "samcloud-say",
    # The CREATE primitive — sc-say's sibling. sc-say is comment-only by
    # construction, so every create went out as bare curl with no headers:
    # nothing bound the writer's conversation and nothing named the thread, so
    # each create started its own (18 tickets -> 18 threads, 2026-09-20, and a
    # holder born per thread). This pen sends X-Session-Id AND X-Thread-Id.
    "new": "samcloud-new",
    # The REVISE primitive (#900): change a doc BODY with the guard the route
    # makes optional. Until this, no pen could edit a body — sc-say is
    # comment-only and sc-new is create-only — so every agent revision on the
    # plane was a raw curl against a page that documented the unguarded call.
    # Three seats independently kept `base_rev` in memory to cover that gap and
    # a fourth learned it by overwriting a live doc (doc 375, 2026-10-10, head
    # was `# probe rev, delete me` for 37 seconds). Contributed by
    # an agent seat from the `sc` it had already been using; carries
    # base_rev-from-a-fresh-read, read-after-write verification, and refusal of
    # an empty or no-op body.
    "edit": "samcloud-edit",
    # The CONTRIBUTE primitive: push a branch and open a pull request on the
    # samcloud repo, with a gh-broker token minted per call and handed to git
    # on its credential pipe — never argv, never disk (docs/Contributing.md).
    "pr": "samcloud-pr.py",
    # Scope v1 (doc 263): the seat's own expected-against-actual check — the
    # one thing a seat is asked to run during the per-seat stage.
    "scope-check": "samcloud-scope-check.py",
    # The interactive hooks (notif-hook, stop-hook, hooks-install) were retired
    # with the templates train (0.10.0): a session a person types into hears
    # through `subscribe` — the message arrives, that is the whole mechanism.
    # For a terminal a person types into: prints what arrives for that
    # session, as the rail would, and receipts it.
    "subscribe": "samcloud-subscribe.py",
    # The same feed as a Claude Code channel (#886): an MCP server that pushes
    # each delivery into the session, idle or not — nothing to re-arm.
    "channel": "samcloud-channel.py",
    # WSL persistence primitive (#231): Windows-side boot task that starts the
    # distro, re-syncs portproxy to the current WSL IP, and pins the distro
    # with a foreground wsl.exe client (WSL kills distros after the last
    # client exits — daemons inside don't count). Deployed by wake-install on
    # WSL hosts; content self-updates via the listener (WSL_BOOT_PATH).
    "wsl-boot": "samcloud-wsl-boot.ps1",
    # The compute queue client (0.12.98): acquire a lease and block until it is
    # granted, hold it by renewing, release it after the job's processes have
    # exited. What a runner job hook or a worker runs so a Unity job, a model
    # load and a splat transform queue against the same host in one order.
    "lease": "samcloud-lease",
}

_RAIL_VERSION_CACHE: dict = {}

# name -> (filename, version-stamp regex). One source of truth: the artifact.
_RAIL_VERSION_SPECS = {
    "listen": ("samcloud-listen.py", r'^RAIL_VERSION = "([^"]+)"'),
    "wake": ("samcloud-wake.sh", r'^WAKE_VERSION="([^"]+)"'),
    "rail": ("samcloud-rail.py", r'^RAIL_VERSION = "([^"]+)"'),
    # notif-hook / stop-hook retired 0.10.0 — a session hears through the
    # subscribe helper; `GET /wake/surfaces` is where interactive surfaces live.
    "wsl-boot": ("samcloud-wsl-boot.ps1", r'^# WSL_BOOT_VERSION: (\S+)'),
    "scope-check": ("samcloud-scope-check.py", r'^# SCOPE_CHECK_VERSION: (\S+)'),
    "subscribe": ("samcloud-subscribe.py", r'^# SUBSCRIBE_VERSION: (\S+)'),
    "channel": ("samcloud-channel.py", r'^# CHANNEL_VERSION: (\S+)'),
    "adapter": ("samcloud-adapter.py", r'^# ADAPTER_VERSION: (\S+)'),
    "lease": ("samcloud-lease", r'^# LEASE_VERSION: (\S+)'),
}


def _rail_versions() -> dict:
    """Currently-served rail + hook artifact versions, parsed from the
    artifacts themselves (single source of truth — no second constant to
    drift). Files are immutable for a container's lifetime, so parse once."""
    if not _RAIL_VERSION_CACHE:
        base = Path(__file__).parent / "helpers"
        for key, (fname, pattern) in _RAIL_VERSION_SPECS.items():
            try:
                m = _re.search(pattern, (base / fname).read_text(), _re.M)
                _RAIL_VERSION_CACHE[key] = m.group(1) if m else "0"
            except OSError:
                _RAIL_VERSION_CACHE[key] = "0"
    return _RAIL_VERSION_CACHE


def _planes() -> dict:
    """Named planes a helper's `--plane` can pick, in order: SAMCLOUD_PLANES
    (`name=url,name=url`), else just this one under its SAMCLOUD_ENV name.

    Choosing a plane picks where a write goes and nothing else: the token and
    header a helper reads don't change with it."""
    out = {}
    for part in os.environ.get("SAMCLOUD_PLANES", "").split(","):
        name, _, url = part.strip().partition("=")
        if name.strip() and url.strip():
            out[name.strip()] = url.strip().rstrip("/").removesuffix("/api/v1")
    if not out:
        out[os.environ.get("SAMCLOUD_ENV", "prod") or "prod"] = (
            os.environ.get("PUBLIC_URL") or "http://localhost:7778").rstrip("/")
    return out


def _broker_url(env: str, name: str, path: str = "") -> str:
    """A broker module's URL: its env var, else <name>.<service domain><path>."""
    return os.environ.get(env, "").strip() or f"https://{name}.{_service_domain()}{path}"


@app.get("/api/v1/helpers/{name}")
def serve_agent_helper(name: str, request: Request,
                       auth: AuthContext = Depends(get_auth)):
    """Cold-start wake rail artifacts (RFC doc #17): wake | wake-install."""
    if name not in _AGENT_HELPERS:
        raise HTTPException(404, f"Unknown helper '{name}' (have: {sorted(_AGENT_HELPERS)})")
    public_url = os.environ.get("PUBLIC_URL") or \
        f"{request.url.scheme}://{request.headers.get('host', 'localhost:7776')}"
    text = (Path(__file__).parent / "helpers" / _AGENT_HELPERS[name]).read_text()
    text = text.replace("__API__", f"{public_url}/api/v1")
    # Plane-scoped header filename, so a served stamp never pairs one plane's
    # header with another's API (round-4 #610: the block hardcoded token.hdr and
    # crossed on the staging copy). The staging stack says so in SAMCLOUD_ENV
    # (it matched one deployment's staging hostname before #1010).
    staging = os.environ.get("SAMCLOUD_ENV", "prod") == "staging"
    text = text.replace("__TOKEN_HDR__", "stg-token.hdr" if staging else "token.hdr")
    # Same for the bare token-file path — the v4 stamp still named the prod token
    # (~/.samcloud/token) on a staging seat, three lines above the correct
    # staging header: a residual crossing #610's header-only fix missed (#611).
    # Fixed in GT v5.
    text = text.replace("__TOKEN_FILE__", "stg-token" if staging else "token")
    # The instance's own planes and brokers (#1010 step 3): nothing deployment-specific is
    # baked into a helper, and the reference instance renders exactly what it did.
    planes = _planes()
    width = max(len(n) for n in planes)
    text = text.replace("  # __PLANE_PRESETS__\n", "".join(
        f'  {n}){" " * (width - len(n))} api="{u}/api/v1" ;;\n' for n, u in planes.items()))
    text = text.replace("__PLANE_NAMES__", " or ".join(planes))
    # The rail names its plane from this map (#1010), not from a hostname.
    text = text.replace("__PLANES_JSON__", json.dumps(planes))
    text = text.replace("__ENV_NAME__", os.environ.get("SAMCLOUD_ENV", "prod") or "prod")
    text = text.replace("__GH_BROKER_URL__", _broker_url("GH_BROKER_URL", "ghb"))
    text = text.replace("__RAIL_TOKEN_MINT_URL__",
                        _broker_url("RAIL_TOKEN_MINT_URL", "gglb", "/claude-token"))
    op = _operator_now()
    text = text.replace("__OPERATOR_NOW__",
                        f"{op['human']} ({op['tz']})" if op["human"] else op["tz"])
    text = text.replace("__OPERATOR_TZ__", op["tz"])
    return PlainTextResponse(text)


# --- Service Enrollment (slot-based) ---

@app.post("/api/v1/services/enroll")
def create_service_slot(req: ServiceSlotCreate, auth: AuthContext = Depends(require_admin)):
    """Admin creates a service slot. Admin only.

    Returns a scoped token the agent uses to activate the slot.
    The agent fills in runtime details (port, health, capabilities).
    """
    _validate_service_name(req.name)
    if req.subdomain:
        _validate_subdomain(req.subdomain)

    # Verify device exists
    device = db.query_one("SELECT * FROM devices WHERE id = ?", (req.device_id,))
    if not device:
        raise HTTPException(404, f"Device {req.device_id} not found")

    service_id = f"{req.device_id}/{req.name}"

    # Check if slot already exists
    existing = db.query_one("SELECT id FROM services WHERE id = ?", (service_id,))
    if existing:
        raise HTTPException(409, f"Service {service_id} already exists")

    now = datetime.now(timezone.utc).isoformat()
    subdomain = req.subdomain or f"{req.name}-stg"

    # Create the slot (pending — no port/health yet)
    with db.transaction() as conn:
        conn.execute("""
            INSERT INTO services (id, name, device_id, port, protocol,
                health_endpoint, subdomain, domain, status, created_by, registered_at, updated_at)
            VALUES (?, ?, ?, NULL, 'http', NULL, ?, ?, 'pending', ?, ?, ?)
        """, (service_id, req.name, req.device_id, subdomain, req.domain, auth.owner_id, now, now))

    # Generate scoped service enrollment token
    token = enrollment.generate_enrollment_token(service_id, "service")

    db.audit("system:enrollment", "service.slot.created", service_id,
             {"device_id": req.device_id, "description": req.description})

    public_url = os.environ.get("PUBLIC_URL", "http://localhost:7778")
    brief_url = f"{public_url}/api/v1/enroll/services/brief/{token}"

    return {
        "service_id": service_id,
        "token": token,
        "subdomain": subdomain,
        "brief_url": brief_url,
    }


@app.get("/api/v1/enroll/services/brief/{token}")
def service_briefing(token: str):
    """Serve a briefing document for an agent about to activate a service slot.

    The agent reads this to understand what they're setting up, what info
    they need to provide, and how to activate.
    """
    if not token.startswith("sc_enroll_"):
        raise HTTPException(404, "Invalid token")

    token_hash = enrollment._hash_token(token)
    token_record = db.query_one(
        "SELECT * FROM tokens WHERE token_hash = ? AND type = 'enrollment'",
        (token_hash,),
    )
    if not token_record:
        raise HTTPException(404, "Token not found or expired")

    service_id = token_record["owner_id"]
    service = db.query_one("SELECT * FROM services WHERE id = ?", (service_id,))
    if not service:
        raise HTTPException(404, "Service slot not found")

    device = db.query_one("SELECT * FROM devices WHERE id = ?", (service["device_id"],))
    device_name = device["name"] if device else service["device_id"]

    public_url = os.environ.get("PUBLIC_URL", "http://localhost:7778")

    brief = f"""# Service Briefing: {service['name']}

## Your Mission

You are being asked to set up and manage the **{service['name']}** service on **{device_name}**.

A service slot has been created for you. Your job is to:
1. Find the service running on this device (or start it)
2. Determine what port it runs on
3. Identify its health check endpoint (if any)
4. Activate the slot with these details

## Service Slot

| Field | Value |
|-------|-------|
| Service ID | `{service_id}` |
| Device | `{device_name}` ({service['device_id']}) |
| Subdomain | `{service.get('subdomain', 'none')}` |
| Status | `{service['status']}` (waiting for you) |

## How to Activate

Send a POST request with the runtime details you discover:

```bash
curl -s -X POST "{public_url}/api/v1/services/activate" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "token": "{token}",
    "port": <PORT_THE_SERVICE_RUNS_ON>,
    "health_endpoint": "<HEALTH_CHECK_PATH_OR_NULL>",
    "capabilities": ["<WHAT_THIS_SERVICE_CAN_DO>"]
  }}'
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `token` | Yes | `{token}` (single-use, expires in 1 hour) |
| `port` | Yes | The port this service listens on (e.g. 3003, 8080) |
| `health_endpoint` | No | Path that returns 200 when healthy (e.g. `/health`, `/api/status`) |
| `protocol` | No | Default `http`. Options: `http`, `https`, `tcp`, `websocket` |
| `version` | No | Service version string |
| `capabilities` | No | JSON array of what this service does (e.g. `["media-streaming", "transcoding"]`) |

## What Happens After Activation

1. The service slot moves from `pending` to `active`
2. A forward port is allocated on the control plane (if needed for tunnelled devices)
3. A Caddy route is created at `{service.get('subdomain', 'service')}.{service.get('domain') or _service_domain()}`
4. You receive a **service token** (`sc_service_...`) for ongoing API access
5. The service token can be used to:
   - Report health status
   - Create tickets (`POST /api/v1/tickets`)
   - Update service info

## Tips

- Check what's running: `ss -tlnp` or `netstat -tlnp` or `lsof -i -P`
- Common health endpoints: `/health`, `/api/health`, `/healthz`, `/api/v1/status`
- If the service isn't running, check systemd: `systemctl list-units | grep <name>`
- The service token is permanent — store it at `~/.config/samcloud/service_token`

## Questions?

Check the registry for more context:
- All devices: `GET {public_url}/api/v1/devices`
- All services: `GET {public_url}/api/v1/services`
- This device: `GET {public_url}/api/v1/devices/{service['device_id']}`
"""

    return PlainTextResponse(brief, media_type="text/markdown")


@app.post("/api/v1/services/activate")
def activate_service_slot(req: ServiceSlotActivate):
    """Agent activates a service slot with runtime details.

    The token is scoped to one specific service slot.
    The agent provides: port, health endpoint, capabilities.
    The registry handles: forward port allocation, Caddy route, service token.
    """
    _validate_port(req.port)

    # Validate token
    token_record = enrollment.validate_enrollment_token(req.token)
    if not token_record:
        raise HTTPException(401, "Invalid or expired service token")

    service_id = token_record["owner_id"]

    # Get the slot
    service = db.query_one("SELECT * FROM services WHERE id = ?", (service_id,))
    if not service:
        raise HTTPException(404, f"Service slot {service_id} not found")

    device_id = service["device_id"]
    device = db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,))

    now = datetime.now(timezone.utc).isoformat()

    # Auto-allocate a host forward port for a device reached by reverse tunnel
    slice_port = None
    if device and device.get("outbound_method") == "reverse-tunnel":
        slice_port = port_alloc.allocate("forward", service_id)

    # Compute external URL before DB write
    external_url = _external_url(service["subdomain"], (service.get("domain") or _service_domain())) if service.get("subdomain") else None

    # Update slot with runtime details
    with db.transaction() as conn:
        conn.execute("""
            UPDATE services SET port = ?, slice_port = ?, protocol = ?,
                health_endpoint = ?, version = ?, capabilities = ?,
                external_url = ?, status = 'online', updated_at = ?
            WHERE id = ?
        """, (req.port, slice_port, req.protocol,
              req.health_endpoint, req.version,
              json.dumps(req.capabilities) if req.capabilities else None,
              external_url, now, service_id))

        # Create Caddy route if subdomain was set. Tunnelled services point at
        # the sshd container (where the local-forward listener binds).
        if service.get("subdomain"):
            forward_host = os.environ.get("FORWARD_SSH_HOST", "localhost")
            target = f"http://{forward_host}:{slice_port}" if slice_port else f"http://localhost:{req.port}"
            conn.execute("""
                INSERT OR IGNORE INTO routes (subdomain, domain, target, service_id)
                VALUES (?, ?, ?, ?)
            """, (service["subdomain"], (service.get("domain") or _service_domain()), target, service_id))

    # Wire port forward through device tunnel
    forward_status = None
    if slice_port and device and device.get("tunnel_port"):
        success, msg = forward.start_forward(
            slice_port=slice_port,
            service_port=req.port,
            tunnel_port=device["tunnel_port"],
            service_id=service_id,
            device_ssh_user=device.get("outbound_ssh_user"),
        )
        forward_status = {"success": success, "message": msg}

    # Wire Caddy route (append to Caddyfile, validate, reload). Same target as
    # the routes table row above — tunnelled services go to sshd:slice_port.
    caddy_status = None
    if service.get("subdomain"):
        forward_host = os.environ.get("FORWARD_SSH_HOST", "localhost")
        target = f"http://{forward_host}:{slice_port}" if slice_port else f"http://localhost:{req.port}"
        success, msg = caddy.append_route(service["subdomain"], (service.get("domain") or _service_domain()), target)
        caddy_status = {"success": success, "message": msg}

    # Wire DNS record
    dns_status = None
    if service.get("subdomain"):
        dns_status = _dns_record("add", service["subdomain"], service.get("domain") or _service_domain())

    # Generate service token for ongoing auth
    service_token = enrollment.generate_service_token(service_id)

    db.audit("system:enrollment", "service.slot.activated", service_id,
             {"port": req.port, "health_endpoint": req.health_endpoint,
              "forward": forward_status, "caddy": caddy_status, "dns": dns_status})

    # Verify the route is actually reachable
    verify_status = None
    if external_url:
        verify_status = _verify_route(external_url, req.health_endpoint)

    event_bus.emit("service.activated", service_id,
                   {"service_id": service_id, "port": req.port, "slice_port": slice_port,
                    "external_url": external_url, "reachable": verify_status.get("reachable") if verify_status else None},
                   scope=f"device:{device_id}")

    public_url = os.environ.get("PUBLIC_URL", "http://localhost:7778")

    return {
        "service_id": service_id,
        "status": "active",
        "port": req.port,
        "slice_port": slice_port,
        "external_url": external_url,
        "service_token": service_token,
        "guide_url": f"{public_url}/api/v1/services/{service_id}/guide",
        "route": {
            "forward": forward_status,
            "caddy": caddy_status,
            "dns": dns_status,
            "verified": verify_status,
        },
    }


@app.get("/api/v1/enroll/services/pending")
def list_pending_services(auth: AuthContext = Depends(require_admin)):
    """List service slots awaiting activation."""
    return db.query(
        "SELECT id, name, device_id, subdomain, status, registered_at "
        "FROM services WHERE status = 'pending' ORDER BY registered_at DESC"
    )


@app.post("/api/v1/services/register")
def register_service_authenticated(
    req: AuthServiceRegister,
    authorization: Optional[str] = Header(None),
):
    """Register a service using a device token.

    Requires Bearer token authentication. Auto-allocates forward port,
    creates Caddy route if subdomain specified.
    """
    # Extract and verify token
    token_str = enrollment.extract_bearer_token(authorization)
    if not token_str:
        raise HTTPException(401, "Missing or invalid Authorization header")

    token_record = enrollment.verify_token(token_str)
    if not token_record:
        raise HTTPException(401, "Invalid or expired token. Login again: POST /api/v1/auth/login with {\"username\":\"...\",\"password\":\"...\"} to get a new token.")

    device_id = token_record["owner_id"]

    # Verify token scopes include service registration
    scopes = json.loads(token_record.get("scopes") or "[]")
    if "service:register" not in scopes and "*" not in scopes:
        raise HTTPException(403, "Token does not have service:register scope")

    # Verify device exists
    device = db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,))
    if not device:
        raise HTTPException(404, f"Device {device_id} not found")

    service_id = f"{device_id}/{req.name}"

    # Check for duplicate
    existing = db.query_one("SELECT id FROM services WHERE id = ?", (service_id,))
    if existing:
        raise HTTPException(409, f"Service {service_id} already registered")

    # Auto-allocate a host forward port for a device reached by reverse tunnel,
    # keyed on the tunnel itself, as in the slot path above (it used to key on
    # the `workcloud` context label).
    slice_port = None
    if device.get("outbound_method") == "reverse-tunnel":
        slice_port = port_alloc.allocate("forward", service_id)

    external_url = None
    if req.subdomain:
        external_url = _external_url(req.subdomain, req.domain)

    with db.transaction() as conn:
        conn.execute("""
            INSERT INTO services (id, name, device_id, port, slice_port, protocol,
                health_endpoint, subdomain, domain, external_url, status, version, capabilities, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
        """, (service_id, req.name, device_id, req.port, slice_port, req.protocol,
              req.health_endpoint, req.subdomain, req.domain, external_url,
              req.version, json.dumps(req.capabilities) if req.capabilities else None,
              device_id))

        if slice_port:
            conn.execute(
                "INSERT OR IGNORE INTO ports (port, type, owner_id) VALUES (?, 'forward', ?)",
                (slice_port, service_id),
            )

        if req.subdomain:
            forward_host = os.environ.get("FORWARD_SSH_HOST", "localhost")
            target = f"{forward_host}:{slice_port}" if slice_port else f"localhost:{req.port}"
            conn.execute("""
                INSERT OR IGNORE INTO routes (subdomain, domain, target, service_id)
                VALUES (?, ?, ?, ?)
            """, (req.subdomain, req.domain, target, service_id))

        # Auto-add to groups that contain the device
        device_groups = conn.execute(
            "SELECT group_id FROM group_devices WHERE device_id = ?", (device_id,)
        ).fetchall()
        for g in device_groups:
            conn.execute(
                "INSERT OR IGNORE INTO group_services (group_id, service_id) VALUES (?, ?)",
                (g["group_id"], service_id),
            )

    # Generate scoped service token
    service_token = enrollment.generate_service_token(service_id)

    db.audit("system:enrollment", "service.register.authenticated", service_id,
             {"device": device_id, "port": req.port, "slice_port": slice_port,
              "subdomain": req.subdomain})

    return {
        "service_id": service_id,
        "slice_port": slice_port,
        "external_url": external_url,
        "service_token": service_token,
    }


@app.post("/api/v1/enroll/{device_id}/retoken")
def regenerate_enrollment_token(device_id: str, request: Request, auth: AuthContext = Depends(get_auth)):
    """Generate a fresh enrollment token for an existing device.

    Allowed for pending devices (initial bootstrap retry) and online devices
    (re-bootstrap after key loss / redeploy). Activation will overwrite
    ssh_pubkey and bump enrolled_at. The device's manager or the admin;
    scope v1 train 0 — this minted for anyone who knew a device id before.
    """
    device = _require_managed_device(auth, device_id)

    context = device.get("context", "samcloud")
    token = enrollment.generate_enrollment_token(device_id, context)

    db.audit("system:enrollment", "token.enrollment.regenerated", device_id,
             {"prior_status": device.get("status")})

    public_url = os.environ.get("PUBLIC_URL") or \
        f"{request.url.scheme}://{request.headers.get('host', 'localhost:7778')}"
    bootstrap_url = f"{public_url}/api/v1/bootstrap/{token}"
    return {
        "device_id": device_id,
        "token": token,
        "bootstrap_url": bootstrap_url,
        "bootstrap_command": f"curl -s {bootstrap_url} | bash",
        "bootstrap_command_ps1": f'irm "{bootstrap_url}/ps1" | iex',
        "expires_in": 3600,
        "tunnel_port": device.get("tunnel_port"),
    }


@app.get("/api/v1/enroll/pending")
def list_pending_enrollments(auth: AuthContext = Depends(require_admin)):
    """List all devices with status 'pending' (awaiting activation)."""
    return db.query(
        "SELECT id, name, hostname, context, type, "
        "inbound_method, outbound_method, outbound_ip, tunnel_port, created_at "
        "FROM devices WHERE status = 'pending' ORDER BY created_at DESC"
    )


# --- Status / Dashboard ---

@app.get("/api/v1/events")
async def event_stream(request: Request, for_conversation: Optional[str] = None,
                       auth: AuthContext = Depends(get_auth)):
    """Server-Sent Events stream. Scoped to caller's permissions.

    `?for_conversation=<id>` scopes it to one conversation instead — the same
    thing `/notifications/watch?for_conversation=` does for the read, so a
    surface can hold this open as its doorbell and stop polling. Permission is
    not delivery: with a conversation named, an admin's stream carries what is
    addressed to that conversation and nothing else, exactly like every other
    seat's (0.12.20).

    Frames carry a monotonic `id:`; reconnect with Last-Event-ID for a
    best-effort gap replay (bounded by the in-process history window — clients
    MUST still reconcile /notifications?unread=true on every connect, doc #17).
    Keepalive comment every 15s so half-open sockets are detectable.

    Subscribe: curl -N -H @"$HOME/.samcloud/token.hdr" "$PUBLIC_URL/api/v1/events"
    """
    queue = event_bus.subscribe()
    last_id_hdr = request.headers.get("last-event-id", "")
    last_seq = int(last_id_hdr) if last_id_hdr.isdigit() else None
    _stream_ident = auth.identity()

    async def generate():
        _stream_opened(_stream_ident)   # live-stream ledger (task 609)
        try:
            # Catchup: gap replay from Last-Event-ID when we can, else recent few
            _conv = (for_conversation or "").strip()
            _visible = ((lambda e: _event_for_conversation(auth, e, _conv)) if _conv
                        else (lambda e: _can_see_event(auth, e)))
            catchup = event_bus.since(last_seq) if last_seq is not None \
                else event_bus.recent(limit=10)
            for event in catchup:
                if _visible(event):
                    yield event.to_sse()

            # Stream live events. The 15s keepalive clock is wall-clock time
            # since the last byte actually written to THIS client, not since
            # the last queue.get() — a scoped subscriber can have its
            # queue.get() resolved repeatedly by events invisible to it
            # (_can_see_event False), which would otherwise reset a
            # wait_for(timeout=15) without ever producing output, starving
            # the client of keepalives for as long as the bus stays noisy
            # (ticket #184).
            last_sent = time.monotonic()
            while True:
                remaining = 15 - (time.monotonic() - last_sent)
                if remaining <= 0:
                    yield ": keepalive\n\n"
                    last_sent = time.monotonic()
                    continue
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=remaining)
                    if _visible(event):
                        yield event.to_sse()
                        last_sent = time.monotonic()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    last_sent = time.monotonic()
        except asyncio.CancelledError:
            pass
        finally:
            _stream_closed(_stream_ident)
            event_bus.unsubscribe(queue)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/v1/events/recent")
def recent_events(limit: int = 20, auth: AuthContext = Depends(get_auth)):
    """Get recent events (non-streaming). Useful for polling fallback."""
    _check_limit(limit)
    events = event_bus.recent(limit=limit)
    if not auth.is_admin():
        events = [e for e in events if _can_see_event(auth, e)]
    return [{"type": e.type, "target": e.target, "data": e.data,
             "timestamp": e.timestamp} for e in events]


# --- Notifications (durable inbox, RFC #37) ---

def _notification_row(r: dict) -> dict:
    out = dict(r)
    try:
        out["payload"] = json.loads(r["payload"]) if r.get("payload") else None
    except (json.JSONDecodeError, TypeError):
        out["payload"] = None
    out["read"] = bool(r.get("read"))
    return out


def _delivery_rows(rows: list, me: str, conversation: Optional[str] = None) -> list:
    """Read-time delivery context shared by polling, rail and channel consumers.

    No new binding, receipt, subscription or liveness assertion is made here.
    Sibling state is a snapshot of registered bindings, not a process probe.
    """
    if not rows:
        return []
    # A snapshot, not a park diagnosis: no plane-owned dispatch-park state exists.
    # Exclude this batch so the hint names work not already in this delivery.
    ids = [r["id"] for r in rows]
    pending_where = ("recipient = ? AND read = 0 AND delivered_to IS NULL "
                     "AND event_type != 'doc.published' AND id NOT IN (" +
                     ",".join("?" for _ in ids) + ")")
    pending_args = (me, *ids)
    pending = db.query_one("SELECT COUNT(*) AS count, MIN(created_at) AS since "
                           "FROM notifications WHERE " + pending_where, pending_args)
    pending_refs = []
    for n in db.query("SELECT * FROM notifications WHERE " + pending_where + " ORDER BY id LIMIT 6",
                      pending_args):
        p = _notification_row(n).get("payload") or {}
        kind = next((k for k in _THREAD_TABLES if n.get(k + "_id") is not None), None)
        ref = f"event {n['id']}"
        if kind:
            oid = n[kind + "_id"]
            obj = _object_row(kind, oid)
            if obj and _identity_can_read_object(me, kind, obj):
                ref += f" {kind} #{oid}"
                if type(p.get("comment_id")) is int:
                    ref += f" c{p['comment_id']}"
            else:
                ref += " (hidden)"
        pending_refs.append(ref)
    pending["references"] = pending_refs
    pending_text = (f"Other undelivered inbox events for your identity: {pending['count']} "
                    f"(oldest {pending['since']} UTC) · " + ", ".join(pending_refs) +
                    "; GET /api/v1/notifications?unread=true") if pending["count"] else ""
    bindings = db.query("SELECT * FROM threads WHERE identity = ? "
                        "AND state IN ('open','closing') ORDER BY thread_id, id", (me,))
    surfaces = {s["conversation_id"] for s in db.query(
        "SELECT conversation_id FROM surfaces WHERE identity = ? AND ended_at IS NULL", (me,))}
    grouped = {}
    for b in bindings:
        sid = b["conversation_id"]
        member = {"thread_id": b.get("thread_id"), "kind": b["kind"],
                  "object_id": b["object_id"], "state": b["state"],
                  "presence": ("surface registered" if sid in surfaces else
                               "process recorded" if b.get("pid") else "no process recorded")}
        obj = _object_row(b["kind"], b["object_id"])
        member["hidden"] = not obj or not _identity_can_read_object(me, b["kind"], obj)
        g = grouped.setdefault(sid, {"conversation": _session_fp(sid), "threads": []})
        if member not in g["threads"]:
            g["threads"].append(member)
    result = []
    for raw in rows:
        r = _notification_row(raw)
        r["pending_inbox"] = pending
        r["pending_inbox_text"] = pending_text
        p = dict(r.get("payload") or {})
        receiver = conversation or next((b["conversation_id"] for b in bindings
            if (r.get("thread_id") is not None and b.get("thread_id") == r["thread_id"])
            or (r.get("thread_id") is None and r.get(b["kind"] + "_id") == b["object_id"])), None)
        siblings = [g for sid, g in grouped.items() if sid != receiver]
        r["siblings"] = siblings
        labels = []
        for g in siblings[:6]:
            refs = []
            for t in g["threads"][:6]:
                ref = f"thread {t['thread_id']}" if t["thread_id"] is not None else "thread unknown"
                ref += (" (hidden)" if t["hidden"] else f" {t['kind']} #{t['object_id']}")
                refs.append(f"{ref} ({t['presence']})")
            if len(g["threads"]) > 6:
                refs.append(f"+{len(g['threads']) - 6} threads")
            labels.append(f"[{g['conversation']}] " + ", ".join(refs))
        if len(siblings) > 6:
            labels.append(f"+{len(siblings) - 6} conversations; GET /api/v1/wake/threads")
        elif any(len(g["threads"]) > 6 for g in siblings):
            labels.append("GET /api/v1/wake/threads for all bindings")
        r["siblings_text"] = ("Other open conversations of your identity: " + " · ".join(labels)) if labels else ""
        kind = next((k for k in _THREAD_TABLES if r.get(k + "_id") is not None), None)
        if kind:
            oid = r[kind + "_id"]
            read = f"GET /api/v1/{_THREAD_TABLES[kind][0]}/{oid}"
            if kind == "doc" and type(p.get("rev")) is int:
                read += f"?rev={p['rev']}"
            r["read_object"] = read
            # Root bodies on old queued rows are not re-injected either.
            root = p.get("root")
            if isinstance(root, dict):
                rk, rid = root.get("kind"), root.get("id")
                rr = _object_row(rk, rid) if rk in _THREAD_TABLES else None
                p["root"] = {"kind": rk, "id": rid}
                if rr and _identity_can_read_object(me, rk, rr):
                    p["root"].update(title=root.get("title", ""),
                                      read=f"GET /api/v1/{_THREAD_TABLES[rk][0]}/{rid}")
                else:
                    p["root"]["hidden"] = True
            # Comments/direct asks remain text. Only record bodies become references.
            et = r.get("event_type") or ""
            if et in {"ticket.created", "task.created", "doc.published"}:
                p.pop("body", None)
                p.pop("detail", None)
            if p.get("record_mention"):
                p.pop("body", None)
                p["read_required"] = True
            r["payload"] = p
        result.append(r)
    return result


def _is_present(identity: str, now: str) -> bool:
    """Is a live surface of this identity currently present? (#266 backstop.)"""
    return bool(db.query_one(
        "SELECT 1 FROM users WHERE username = ? AND presence_until > ?",
        (identity, now)))


def _annotate_claims(rows: list, me: str) -> list:
    """Stamp each notification row with claimed=True when a live surface of
    the recipient holds the thread — cold consumers must leave those alone.

    Presence backstops the claim TTL (#266). A write claims a thread for only
    CLAIM_TTL_WRITE (10 min), but an interactive surface has no watch loop to
    refresh it, so a single long turn — or a self-inflicted registry restart
    that forces a reconnect-reconcile mid-turn — used to strip the claim and
    let the identity's OWN cold rail wake a parallel session onto the thread
    the live surface was actively working (the double-wake, #266). So while a
    live surface of `me` is present (the notif hook heartbeats presence each
    prompt), ALL its claim rows stay effective, not just TTL-fresh ones.
    This is per-thread, never a global defer: a thread with no claim row is
    untouched, so genuinely-unclaimed work is still cold-worked in parallel
    while the surface is live (#199 preserved).

    #392: claims cover all three planes — a notification row is claimed when
    a live claim matches whichever of ticket_id/doc_id/task_id it carries."""
    now = datetime.now(timezone.utc).isoformat()
    if _is_present(me, now):
        claim_rows = db.query(
            "SELECT ticket_id, doc_id, task_id FROM thread_claims "
            "WHERE identity = ?", (me,))
    else:
        claim_rows = db.query(
            "SELECT ticket_id, doc_id, task_id FROM thread_claims "
            "WHERE identity = ? AND claimed_until > ?", (me, now))
    claimed = {(col, c[col]) for c in claim_rows
               for col in ("ticket_id", "doc_id", "task_id")
               if c.get(col) is not None}
    for r in rows:
        r["claimed"] = any((col, r.get(col)) in claimed
                           for col in ("ticket_id", "doc_id", "task_id"))
    return rows


@app.get("/api/v1/subscriptions")
def my_subscriptions(auth: AuthContext = Depends(get_auth)):
    """The caller's watch state, finally readable (#414): every ticket, doc,
    and project you're subscribed to — the things whose events land in your
    inbox — plus your standing mutes (#83). Until this, subscription state was
    write-only: agents could subscribe/unsubscribe but never audit what they
    watch, so watcher-list drift was invisible."""
    me = auth.identity()
    tickets = db.query(
        "SELECT t.id, t.summary, t.status, w.updates, w.created_at AS watching_since "
        "FROM ticket_watchers w JOIN tickets t ON t.id = w.ticket_id "
        "WHERE w.identity = ? ORDER BY t.id DESC", (me,))
    docs = db.query(
        "SELECT d.id, d.slug, d.title, d.status, w.updates, w.created_at AS watching_since "
        "FROM doc_watchers w JOIN docs d ON d.id = w.doc_id "
        "WHERE w.identity = ? ORDER BY d.id DESC", (me,))
    projects = db.query(
        "SELECT p.id, p.slug, p.name, w.created_at AS watching_since "
        "FROM project_watchers w JOIN projects p ON p.id = w.project_id "
        "WHERE w.identity = ? ORDER BY p.id", (me,))
    mutes = db.query(
        "SELECT ticket_id, doc_id, created_at FROM thread_mutes "
        "WHERE identity = ? ORDER BY created_at DESC", (me,))
    return {"identity": me,
            "counts": {"tickets": len(tickets), "docs": len(docs),
                       "projects": len(projects), "mutes": len(mutes)},
            "tickets": tickets, "docs": docs, "projects": projects,
            "mutes": mutes}


def _thread_ref_clause(thread: Optional[str], alias: str = "n"):
    """SQL fragment + params for ?thread=ticket:N|doc:N|task:N|lobby (#561
    lifecycle fix: the inbox hands out per thread id).
    lobby = rows carrying no thread reference at all."""
    if not thread:
        return "", ()
    t = thread.strip().lower()
    if t == "lobby":
        return (f"AND {alias}.ticket_id IS NULL AND {alias}.doc_id IS NULL "
                f"AND {alias}.task_id IS NULL ", ())
    plane, _, num = t.partition(":")
    col = {"ticket": "ticket_id", "doc": "doc_id", "task": "task_id"}.get(plane)
    if not col or not num.isdigit():
        raise HTTPException(422, "thread must be ticket:N, doc:N, task:N, or lobby")
    return f"AND {alias}.{col} = ? ", (int(num),)


def _lane_scope_clause(lane_scope: Optional[str], x_session_id: Optional[str],
                       alias: str = "n"):
    """?lane_scope=own — hide rows whose thread is held by ANOTHER stamped
    live session of the same identity (#561: foreign-lane traffic opening
    turns on every surface was killing held sessions' background work; the
    pane should only get turns for its own business). Conservative on
    legacy: nonce-less claims never hide a row, so an unstamped writer's
    threads keep today's visibility — pressure toward universal
    X-Session-Id stamping (doc 163 §1) without ever losing a message."""
    if (lane_scope or "").strip().lower() != "own":
        return "", ()
    now = datetime.now(timezone.utc).isoformat()
    sid = _session_of(x_session_id) or ""
    return (f"AND NOT EXISTS (SELECT 1 FROM thread_claims lc "
            f"WHERE lc.identity = {alias}.recipient "
            f"AND (({alias}.ticket_id IS NOT NULL AND lc.ticket_id = {alias}.ticket_id) "
            f"  OR ({alias}.doc_id IS NOT NULL AND lc.doc_id = {alias}.doc_id) "
            f"  OR ({alias}.task_id IS NOT NULL AND lc.task_id = {alias}.task_id)) "
            f"AND lc.claimed_until > ? AND lc.session_id IS NOT NULL "
            f"AND lc.session_id != ?) ", (now, sid))


# What OPENS a thread toward you, on any plane (mirrors OPEN_EVENTS in the
# served rail — samcloud-rail.py, `deliverable()` clause (b)).
# What a direct wake may say. Narrow on purpose: these are the types the rail
# classifies (`_is_message`), so a wake cannot arrive as a word nothing knows.
_WAKE_EVENT_TYPES = {"schedule.fired", "ticket.mentioned", "doc.mentioned",
                     "task.mentioned", "ticket.commented", "doc.commented",
                     "task.commented"}
# The rail's message/status split, stated on the plane so `docs_coverage.py`
# fails when the two drift (3c). Since 0.12.69 the plane itself decides
# nothing by them: what opens a thread toward an identity is the rail's
# question (a cold wake), not a live conversation's feed.
_OPEN_EVENT_TYPES = ("ticket.mentioned", "doc.mentioned", "task.mentioned",
                     "task.assigned", "task.delegated", "doc.assigned")
# What counts as someone SPEAKING on an object (rail: RESPONSE_EVENTS). A
# metadata row — doc.published, doc.linked, a priority change — is information,
# not a conversation, and never opens anything.
_RESPONSE_EVENT_TYPES = ("ticket.commented", "doc.commented",
                         "task.commented")


def _conversation_scope_clause(for_conversation: Optional[str], me: str,
                               alias: str = "n"):
    """?for_conversation=<id> — what reaches a conversation that receives (a
    session a person types into, a poller): the rows on the threads THIS
    conversation is on (its binding open or closing). Nothing else.

    Until 0.12.69 a second clause also pushed "what opens a thread toward
    me" — a mention, an assignment, a reply on something I own — to any live
    conversation of the identity that was NOT on the thread (unless another
    one was). That made a live session the identity's catch-all, and "with two
    live sessions, which one gets it?" had no answer. The ruling on #705
    (2026-09-08, doc 235 rev 12) is holder-first: such a message is a cold
    wake — the rail births a holder on that thread — and a live session hears
    the threads it is on, which it gets on by creating in one, writing on one,
    or being woken on one. `schedule.fired` rides its task, and the task is in
    its creator's thread (#886), so a scheduled follow-up still comes back to
    the conversation that asked for it through this one clause. `me` is kept
    for the callers; the rule no longer needs it.

    Written once, here, so the long-poll, the subscriber and the channel
    cannot drift apart — the drift this exists to remove.
    """
    conv = (for_conversation or "").strip()[:64]
    if not conv:
        return "", ()
    a = alias
    # v19 (doc 239): a binding is on a universal thread; a row belongs to one.
    # Rows from before v19 carry no thread_id and match on their object.
    obj_match = (f"(({a}.thread_id IS NOT NULL AND t.thread_id = {a}.thread_id) "
                 f" OR ({a}.thread_id IS NULL AND ("
                 f"     ({a}.ticket_id IS NOT NULL AND t.kind = 'ticket' AND t.object_id = {a}.ticket_id) "
                 f"  OR ({a}.doc_id IS NOT NULL AND t.kind = 'doc' AND t.object_id = {a}.doc_id) "
                 f"  OR ({a}.task_id IS NOT NULL AND t.kind = 'task' AND t.object_id = {a}.task_id))))")
    on_thread = (f"EXISTS (SELECT 1 FROM threads t WHERE t.identity = {a}.recipient "
                 f"AND t.conversation_id = ? AND t.state IN ('open','closing') "
                 f"AND {obj_match})")
    # A sibling row is pinned to its existing receiver, never a new holder.
    # Echo filtering is per origin conversation, not per account.
    return (f"AND {a}.event_type != 'doc.published' AND {on_thread} AND (json_extract({a}.payload, '$.recipient_session') IS NULL "
            f"OR json_extract({a}.payload, '$.recipient_session') = ?) "
            f"AND NOT ({a}.actor = {a}.recipient AND "
            f"COALESCE(json_extract({a}.payload, '$.actor_session'), '') = ?) ",
            (conv, _session_fp(conv), _session_fp(conv)))


@app.get("/api/v1/notifications")
def list_notifications(request: Request,
                       unread: bool = True, limit: int = 50,
                       unclaimed: bool = False, ticket_id: int = None,
                       thread: str = None, lane_scope: str = None,
                       undelivered: bool = False, for_conversation: str = None,
                       delivery: bool = False,
                       x_session_id: Optional[str] = Header(None),
                       auth: AuthContext = Depends(get_auth)):
    """Your inbox: ticket events on tickets you watch. Durable across restarts.

    ?for_conversation=<id> — only what the rail would DELIVER to that
    conversation (rail 3.10): threads it holds, plus what opens a thread
    toward you. The identity-wide view is still the default and still the
    place a human looks for everything.

    ?unclaimed=true — only rows NOT covered by one of your live thread claims
    (#199): the cold rail's predicate. Claims cover ticket, doc, AND task
    threads (#392) — a live surface working any of the three planes keeps
    its own cold rail off that thread.

    ?undelivered=true — only rows the rail has never delivered to any of
    your conversations (doc 174): rail 3.9's predicate. Delivered is final.

    ?ticket_id=N — only rows for that ticket. Audit-style queries usually
    want ?unread=false with it: read rows stay in the ledger (#178 probe
    false-negatived on exactly this — the param didn't exist and default
    unread=true hid the delivered-and-read row).
    """
    _check_limit(limit)
    # #87 reconciliation (c572): ?include_read=true and ?all=true were
    # silently ignored and returned a plausible SHORT list — "a wrong answer
    # that looks like an answer". Unknown params are a 400 here, with the
    # spelling the caller almost certainly wanted.
    known = {"unread", "limit", "unclaimed", "ticket_id", "thread",
             "lane_scope", "undelivered", "for_conversation", "delivery"}
    unknown = [k for k in request.query_params if k not in known]
    if unknown:
        raise HTTPException(400, f"unknown query param(s) {unknown} — to "
                                 f"include read/acked rows use ?unread=false")
    me = auth.identity()
    # Wake-rail observability (RFC doc #17 delta 5): reading your inbox is the
    # liveness signal — a stale last_seen means the agent's rail is dead.
    # _user_id_of, not a user: prefix check — agent tokens carry a bare
    # owner_id and never stamped, leaving their liveness invisible (#193,
    # same shape as #167 for presence).
    uid = _user_id_of(auth)
    if uid is not None:
        db.execute("UPDATE users SET last_seen = datetime('now') WHERE id = ?",
                   (uid,))
    tid_clause = "AND n.ticket_id = ? " if ticket_id is not None else ""
    tid_params = (ticket_id,) if ticket_id is not None else ()
    th_clause, th_params = _thread_ref_clause(thread)
    tid_clause += th_clause
    tid_params += th_params
    lane_clause, lane_params = _lane_scope_clause(lane_scope, x_session_id)
    tid_clause += lane_clause
    tid_params += lane_params
    conv_clause, conv_params = _conversation_scope_clause(for_conversation, me)
    tid_clause += conv_clause
    tid_params += conv_params
    if delivery:
        # Filter before LIMIT: an old publish backlog must not hide real work.
        # The ordinary inbox remains the complete audit view.
        tid_clause += "AND n.event_type != 'doc.published' "
    if undelivered:
        # doc 174: delivered means delivered — a row the rail wrote into a
        # conversation is never offered again, to anyone. (An earlier draft
        # re-offered rows once their conversation closed; on the first live
        # close that replayed a whole thread's history into a one-shot as
        # "late messages". A fresh instance reads the thread from the plane.)
        tid_clause += "AND n.delivered_to IS NULL "
    if unclaimed:
        # Cold-eligibility (#199 + #227): skip rows a live claim covers AND
        # rows under a fresh `seen` lease — a surface has eyes on those; the
        # rail gets them back when the lease lapses unacked.
        now = datetime.now(timezone.utc).isoformat()
        # Presence backstops the claim TTL (#266): while a live surface of
        # `me` is present, ALL its claim rows are cold-ineligible, not just
        # TTL-fresh ones — a long turn / self-restart no longer exposes a
        # thread it is working to its own cold rail. Per-thread still: rows
        # with no claim row remain cold-eligible (parallel work, #199).
        me_present = _is_present(me, now)
        join_ttl = "" if me_present else " AND c.claimed_until > ?"
        join_params = () if me_present else (now,)
        rows = db.query(
            "SELECT n.* FROM notifications n "
            "LEFT JOIN thread_claims c ON c.identity = n.recipient AND ("
            "  (n.ticket_id IS NOT NULL AND c.ticket_id = n.ticket_id) OR "
            "  (n.doc_id IS NOT NULL AND c.doc_id = n.doc_id) OR "
            "  (n.task_id IS NOT NULL AND c.task_id = n.task_id))" + join_ttl + " "
            "WHERE n.recipient = ? AND c.id IS NULL "
            "AND (n.seen_until IS NULL OR n.seen_until <= ?) "
            + ("AND n.read = 0 " if unread else "") + tid_clause
            + "ORDER BY n.id DESC LIMIT ?",
            join_params + (me, now) + tid_params + (max(1, min(limit, 200)),))
    else:
        where = "n.recipient = ?" + (" AND n.read = 0" if unread else "")
        rows = db.query(f"SELECT * FROM notifications n WHERE {where} {tid_clause}"
                        "ORDER BY n.id DESC LIMIT ?",
                        (me,) + tid_params + (max(1, min(limit, 200)),))
    return {"notifications": _annotate_claims(_delivery_rows(rows, me, for_conversation), me),
            "hint": ("Reply on a ticket: POST /api/v1/tickets/{ticket_id}/comments "
                     "(or sc-say). Ack is optional bookkeeping: POST "
                     "/api/v1/notifications/ack {\"ids\": [...]}.")}


@app.post("/api/v1/notifications/ack")
def ack_notifications(req: NotificationAck, auth: AuthContext = Depends(get_auth)):
    """Mark notifications HANDLED. Only affects your own rows.

    Ack means "I did something about this" (#227) — surfacing alone is a
    `seen` lease, not an ack. Pass `surface` so the fleet's digests can
    attribute consumption honestly (acked_via)."""
    me = auth.identity()
    via = (req.surface or "").strip()[:32] or None
    if req.all:
        db.execute("UPDATE notifications SET read = 1, acked_via = ? "
                   "WHERE recipient = ? AND read = 0", (via, me))
    elif req.ids:
        placeholders = ",".join("?" * len(req.ids))
        db.execute(f"UPDATE notifications SET read = 1, acked_via = ? "
                   f"WHERE recipient = ? AND id IN ({placeholders})",
                   (via, me, *req.ids))
    else:
        raise HTTPException(400, "Provide ids or all=true")
    return {"status": "acked", "acked_via": via}


@app.post("/api/v1/notifications/seen")
def mark_notifications_seen(req: NotificationSeen,
                            auth: AuthContext = Depends(get_auth)):
    """Take a soft lease on rows a live surface has SURFACED (#227).

    While the lease is fresh the cold rail's predicate skips these rows
    (they're on a screen somewhere); they stay unread, keep re-surfacing in
    digests, and return to cold eligibility when the lease lapses. This
    replaces the old display-ack, which destroyed the work signal on render.
    """
    me = auth.identity()
    if not req.ids:
        raise HTTPException(400, "Provide ids")
    ttl = max(0, min(req.ttl_seconds, 3600))
    placeholders = ",".join("?" * len(req.ids))
    if ttl == 0:
        db.execute(f"UPDATE notifications SET seen_until = NULL "
                   f"WHERE recipient = ? AND id IN ({placeholders})",
                   (me, *req.ids))
        return {"status": "seen-cleared"}
    until = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
    db.execute(f"UPDATE notifications SET seen_until = ? "
               f"WHERE recipient = ? AND id IN ({placeholders})",
               (until, me, *req.ids))
    return {"status": "seen", "seen_until": until}


@app.post("/api/v1/notifications/delivered")
def mark_notifications_delivered(req: NotificationDelivered,
                                 auth: AuthContext = Depends(get_auth)):
    """Delivery receipt (doc 174): the rail wrote these rows into the live
    conversation `conversation_id`. A transport fact, not a lease and not an
    ack — it never expires, and `GET /notifications?undelivered=true` never
    offers the row again, to any conversation. A new instance on the thread
    reads the thread from the plane; it is not re-fed history."""
    me = auth.identity()
    if not req.ids:
        raise HTTPException(400, "Provide ids")
    conv = req.conversation_id.strip()[:64]
    if not conv:
        raise HTTPException(400, "conversation_id is required")
    now = datetime.now(timezone.utc).isoformat()
    placeholders = ",".join("?" * len(req.ids))
    n = db.execute_count(
        f"UPDATE notifications SET delivered_at = ?, delivered_to = ? "
        f"WHERE recipient = ? AND id IN ({placeholders})",
        (now, conv, me, *req.ids))
    db.execute("UPDATE threads SET last_delivery_at = ? WHERE identity = ? "
               "AND conversation_id = ?", (now, me, conv))
    return {"status": "delivered", "delivered": n, "delivered_at": now,
            "conversation": _session_fp(conv)}


# ── Foreground presence (doc #17) ─────────────────────────────────────────
# A live session (interactive or wake) publishes presence; the identity's own
# wake rail checks it and defers dispatch while fresh — a live session owns
# its inbox. One rule kills both the wake stampede and cold sessions
# re-triaging threads a warm session is actively working.

def _user_id_of(auth: AuthContext) -> Optional[int]:
    if auth.owner_id and auth.owner_id.startswith("user:"):
        return int(auth.owner_id.split(":", 1)[1])
    # Agent tokens carry a bare owner_id (the username). Most rail identities
    # run on agent tokens, so without this lookup they can never publish
    # presence and their rails never defer (#167).
    if auth.owner_id:
        row = db.query_one("SELECT id FROM users WHERE username = ?",
                           (auth.owner_id,))
        if row:
            return row["id"]
    return None


@app.post("/api/v1/presence")
def publish_presence(req: PresenceUpdate, auth: AuthContext = Depends(get_auth)):
    """Declare a live session for your identity (ttl_seconds=0 clears)."""
    uid = _user_id_of(auth)
    if uid is None:
        raise HTTPException(400, "presence is per user/agent identity")
    ttl = max(0, min(req.ttl_seconds, 7200))
    if ttl == 0:
        db.execute("UPDATE users SET presence_until = NULL WHERE id = ?", (uid,))
        return {"present": False}
    until = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
    db.execute("UPDATE users SET presence_until = ? WHERE id = ?", (until, uid))
    return {"present": True, "until": until}


@app.get("/api/v1/presence")
def get_presence(auth: AuthContext = Depends(get_auth)):
    """Your own presence state — the wake rail's defer check."""
    uid = _user_id_of(auth)
    if uid is None:
        return {"present": False}
    row = db.query_one("SELECT presence_until FROM users WHERE id = ?", (uid,))
    until = row.get("presence_until") if row else None
    present = bool(until and until > datetime.now(timezone.utc).isoformat())
    return {"present": present, "until": until if present else None}


@app.delete("/api/v1/presence/{username}")
def clear_presence(username: str, auth: AuthContext = Depends(get_auth)):
    """Admin phantom-buster (#241 family): clear another identity's presence.

    A session that dies without clearing its presence leaves a phantom that
    makes that identity's rail defer cold wakes indefinitely ("live surface
    holds presence" with no live surface). Only the identity itself can POST
    presence; only an admin can clear someone else's. Audited.
    """
    if not auth.is_admin():
        raise HTTPException(403, "admin only")
    user = db.query_one("SELECT id, presence_until FROM users WHERE username = ?", (username,))
    if not user:
        raise HTTPException(404, f"unknown user '{username}'")
    had = user.get("presence_until")
    db.execute("UPDATE users SET presence_until = NULL WHERE id = ?", (user["id"],))
    db.audit(auth.identity(), "presence.cleared", username, {"was_until": had})
    return {"status": "cleared", "username": username, "was_until": had}


# A surface holds what it wrote on, and gives it up by saying so (0.12.13).
#
# There was a `connected` flag here: true only while a `?for_conversation`
# long-poll was open, plus a 30 s grace, kept in two in-process dicts. The rail
# consumed it twice — `sweep_bindings()` closed a person's bindings when it read
# false, and `_held_by_surface()` then let a headless holder be born on the
# thread that person was holding.
#
# It was argued past "no timers anywhere" on the grounds that no clock fired and
# it was read at decision time. But it was a proof-of-life test with a time
# window, and it failed the two ways those always fail: it lived in the
# registry's process, so ANY deploy cleared it, and a subscriber busy printing a
# backlog is not polling, so it read as gone while working. Both happened on
# 2026-09-20 (09:23:03 and 20:16:49): seven bindings closed each time, a twin
# born each time, on threads the person was actively using.
#
# The repo already said the right thing — `docs/Rail Behaviour Map.md`: "a
# session a human types into is a holder with a different transport". A holder
# is held while its PROCESS lives, a durable fact. The surface's equivalent is
# its REGISTRATION, retired by `{"ended": true}`. Delivery is receipt; a message
# nobody reads sits unread, and nothing posts a replacement correspondent.


@app.get("/api/v1/notifications/watch")
async def watch_notifications(timeout: int = 60, thread: str = None,
                              lane_scope: str = None, settle: float = 4.0,
                              for_conversation: str = None,
                              undelivered: bool = False,
                              auth: AuthContext = Depends(get_auth),
                              x_session_id: Optional[str] = Header(None)):
    """Long-poll: returns as soon as you have an unread notification.

    The agent wake-up primitive — block here instead of polling. Returns
    immediately if unread rows already exist; otherwise waits up to `timeout`
    seconds (max 300) and returns {"notifications": [], "timed_out": true}.
    DB-backed, so a notification written while you weren't watching is still
    there when you come back.

    ?thread=ticket:N|doc:N|task:N|lobby — watch ONE lane (#561/#568 c372:
    an identity-wide watch on a multi-session seat gets served sibling
    lanes' rows; a holder watching its own thread must see exactly that
    thread). ?lane_scope=own — identity-wide watch that skips rows held by
    another stamped live session of you (pair with X-Session-Id).

    ?for_conversation=<id> — the surface form (rail 3.10): wait only for what
    the rail would deliver to that conversation, on the same rule a headless
    holder gets. Pair with ?undelivered=true and a `/notifications/delivered`
    receipt for each row you print, and this endpoint stops re-offering what
    you have already shown — the interactive surface's equivalent of writing
    into a holder's fifo. Without the receipt the same rows page forever
    (#539) and a human ends up hand-acking a cursor.

    ?undelivered=true — skip rows already delivered to ANY conversation of
    yours. Delivered is final (doc 174).

    ?settle=N (seconds, default 4, 0 disables, max 15): once the first row
    lands, hold the response open N more seconds and re-query, so a burst of
    comments seconds apart is DELIVERED AS ONE BATCH. Every crossing-race on
    #577 (c5235/c5236 13s apart, c5259/c5260, c5263/64/67/69) was an agent
    replying between two rows of the same burst; the settle window makes the
    settled thread state the unit of delivery, not the individual row
    (doc 163 §6, debounce ruling 2026-09-01).
    """
    timeout = max(1, min(timeout, 300))
    settle = max(0.0, min(float(settle), 15.0))
    me = auth.identity()
    # A held watch IS presence (task #162): whoever is actively listening
    # receives notifications the moment they land, so the rail must defer to
    # them — server-side, no client cooperation needed. Extend-only (never
    # shorten an explicitly-published longer presence); the +90s grace covers
    # the gap between successive watch calls in a loop.
    uid = _user_id_of(auth)
    if uid is not None:
        until = (datetime.now(timezone.utc)
                 + timedelta(seconds=timeout + 90)).isoformat()
        db.execute("UPDATE users SET presence_until = ? WHERE id = ? "
                   "AND (presence_until IS NULL OR presence_until < ?)",
                   (until, uid, until))
    # A held watch also keeps your thread claims alive (#199): the surface
    # actively listening is the surface that owns its threads. Expired claims
    # are NOT resurrected — re-engage by writing. Session-scoped (#264):
    # your watch sustains YOUR claims (+ legacy unstamped ones), never a
    # parallel session's — dead surfaces decay on their own TTL.
    _refresh_claims(me, timeout + 90, session_id=_session_of(x_session_id))
    return await _watch_loop(timeout, thread, lane_scope, settle, for_conversation,
                             undelivered, me, x_session_id)


async def _watch_loop(timeout, thread, lane_scope, settle, for_conversation,
                      undelivered, me, x_session_id):
    """The body of `watch_notifications`, split out so the surface-liveness
    bookkeeping above can wrap it in one try/finally."""
    deadline = time.monotonic() + timeout
    while True:
        # Honor seen-leases (#539): without this filter, 50+ old unread rows
        # permanently occupy the oldest-first page and NEW rows never surface —
        # a subscriber that leases what it reads then pages through any backlog
        # at 50/cycle instead of starving deaf on the same stale page forever.
        now = datetime.now(timezone.utc).isoformat()
        th_clause, th_params = _thread_ref_clause(thread)
        lane_clause, lane_params = _lane_scope_clause(lane_scope, x_session_id)
        conv_clause, conv_params = _conversation_scope_clause(for_conversation, me)
        # A registered surface pages on `delivered`, not on `seen`/`read`: it
        # receipts what it prints, so the filter that keeps a holder from being
        # re-fed is the one that keeps a human session from being re-fed.
        deliv_clause = "AND n.delivered_to IS NULL " if undelivered else ""
        scope_clause = th_clause + lane_clause + conv_clause + deliv_clause
        scope_params = th_params + lane_params + conv_params
        rows = db.query("SELECT n.* FROM notifications n WHERE n.recipient = ? "
                        "AND n.read = 0 "
                        "AND (n.seen_until IS NULL OR n.seen_until <= ?) "
                        + scope_clause +
                        "ORDER BY n.id LIMIT 50",
                        (me, now) + scope_params)
        if rows:
            if settle > 0:
                # Quiet-period debounce (v0.8.6, staging #86: a fixed window
                # cut a same-second burst across two deliveries — "the batch
                # boundary does not respect the conversation"). Extend while
                # rows keep arriving: return once no NEW row for `settle`
                # seconds, capped so a chatty thread can't hold the response
                # past ~3x the window. Rows acked elsewhere drop out; if ALL
                # were handled elsewhere, resume waiting instead of returning
                # an empty batch.
                known = {r["id"] for r in rows}
                last_new = time.monotonic()
                cap = time.monotonic() + max(settle * 3, 10.0)
                while (time.monotonic() < min(cap, deadline)
                       and time.monotonic() - last_new < settle):
                    await asyncio.sleep(0.5)
                    now = datetime.now(timezone.utc).isoformat()
                    rows = db.query("SELECT n.* FROM notifications n WHERE n.recipient = ? "
                                    "AND n.read = 0 "
                                    "AND (n.seen_until IS NULL OR n.seen_until <= ?) "
                                    + scope_clause +
                                    "ORDER BY n.id LIMIT 50",
                                    (me, now) + scope_params)
                    fresh = {r["id"] for r in rows} - known
                    if fresh:
                        known |= fresh
                        last_new = time.monotonic()
                if not rows:
                    continue
            return {"notifications": _annotate_claims(_delivery_rows(rows, me, for_conversation), me),
                    "hint": ("Ack these (POST /api/v1/notifications/ack), reply via "
                             "POST /api/v1/tickets/{ticket_id}/comments, then call "
                             "watch again to keep listening.")}
        if time.monotonic() >= deadline:
            return {"notifications": [], "timed_out": True,
                    "hint": "No news. Call watch again to keep listening."}
        await asyncio.sleep(2)


@app.post("/api/v1/agents/{username}/wake")
def wake_agent(username: str, req: AgentWake, auth: AuthContext = Depends(require_admin)):
    """Admin-only: wake a specific agent directly — a durable notification +
    live SSE event with no ticket/doc/task backing it (#303 item 1). Replaces
    filing a throwaway ticket purely to get a cold-wake to fire; the wake
    prompt renderer already dumps unread-notification JSON regardless of
    which plane (if any) it's attached to, so this rides the same durable
    inbox and watch/SSE paths every other event type does."""
    user = db.query_one("SELECT id FROM users WHERE username = ?", (username,))
    if not user:
        raise HTTPException(404, f"Unknown agent '{username}'")
    if req.event_type not in _WAKE_EVENT_TYPES:
        raise HTTPException(400, f"event_type must be one of {sorted(_WAKE_EVENT_TYPES)} — "
                                 f"a type the rail has never seen cannot be judged, and with a "
                                 f"body it would be read as a message and birth a holder")
    root = _thread_root(req.thread_id)
    if not root:
        raise HTTPException(400, f"thread_id {req.thread_id} is not a thread "
                                 f"(GET /api/v1/threads). A wake names the work it is about: "
                                 f"whoever you wake has to have somewhere to answer")
    payload = {"summary": req.summary, "body": req.body}
    db.execute(
        "INSERT INTO notifications (recipient, event_type, actor, payload, thread_id, "
        f"{_THREAD_PLANES[root[0]]}) VALUES (?, ?, ?, ?, ?, ?)",
        (username, req.event_type, auth.identity(), json.dumps(payload),
         req.thread_id, root[1]),
    )
    event_bus.emit(req.event_type, f"user:{user['id']}", payload, scope=f"user:{user['id']}")
    return {"status": "sent", "recipient": username, "event_type": req.event_type}


# ── Wake-rail fleet registration (doc #17; doc #22 step 1) ────────────────
# The registry's view of who has a rail, where, at what version, pointed at
# which env — the observability line between "the rail feels flaky" and a
# fleet you can actually measure. Registration doubles as the update check:
# the response carries the served versions, and the listener self-updates
# when its own differ.

@app.post("/api/v1/wake/register")
def register_wake(req: WakeRegister, auth: AuthContext = Depends(get_auth)):
    """Register (or refresh) this identity's wake rail. Called by the
    listener on every SSE connect — cheap upsert on (user, host)."""
    uid = _user_id_of(auth)
    if uid is None:
        raise HTTPException(400, "wake registration is per user/agent identity")

    # ── retire (#897) ──────────────────────────────────────────────────────────
    if req.ended:
        target_uid, target = uid, auth.identity()
        if req.identity and req.identity != auth.identity():
            if not auth.is_admin():
                raise HTTPException(403, "retiring another identity's registration "
                                         "is admin-only")
            row = db.query_one("SELECT id FROM users WHERE username = ?",
                               (req.identity,))
            if not row:
                raise HTTPException(404, f"no such identity: {req.identity}")
            target_uid, target = row["id"], req.identity
        # Exact (identity, host) — the schema's own UNIQUE key. Deliberately NOT a
        # bulk retire of every row an identity holds: the population this exists to
        # clean is a handful, and `?include_retired=true` lists the hosts, so naming
        # the row is cheap and a mistake is one row rather than a seat's whole
        # history. Host is matched as given, including "" — a row can legitimately
        # carry it (schema default), so there is no safe value to treat as "any".
        existing = db.query_one(
            "SELECT id, ended_at FROM wake_registrations "
            "WHERE user_id = ? AND host = ?", (target_uid, req.host))
        if not existing:
            raise HTTPException(404, f"no wake registration for {target} at host "
                                     f"{req.host!r}")
        db.execute("UPDATE wake_registrations SET ended_at = datetime('now') "
                   "WHERE id = ?", (existing["id"],))
        db.audit(auth.identity(), "wake.register.retire", req.host,
                 {"identity": target, "by_admin": target_uid != uid,
                  "was_retired": existing["ended_at"] is not None})
        return {"status": "retired", "identity": target, "host": req.host,
                "hint": "It leaves GET /wake/registrations; ?include_retired=true "
                        "still shows it, because these rows are the audit trail of "
                        "who ran a rail where. A real register CLEARS this, so a "
                        "seat that comes back is visible again — a retire you "
                        "cannot undo is the same defect with the sign flipped."}

    existing = db.query_one(
        "SELECT id FROM wake_registrations WHERE user_id = ? AND host = ?",
        (uid, req.host))
    if existing:
        db.execute(
            "UPDATE wake_registrations SET wake_cmd = ?, listener_version = ?, "
            "handler_version = ?, api_env = ?, notif_hook_version = ?, "
            "stop_hook_version = ?, max_conn_age = ?, last_connect_at = datetime('now'), "
            # Resurrect, exactly as a surface does. A dead row's seat never calls
            # this, so the accumulation case is still solved; a seat that comes
            # back becomes visible — and darkenable — again.
            "ended_at = NULL "
            "WHERE id = ?",
            (req.wake_cmd, req.listener_version, req.handler_version,
             req.api_env, req.notif_hook_version, req.stop_hook_version,
             req.max_conn_age, existing["id"]))
    else:
        db.execute(
            "INSERT INTO wake_registrations "
            "(user_id, host, wake_cmd, listener_version, handler_version, api_env, "
            " notif_hook_version, stop_hook_version, max_conn_age) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, req.host, req.wake_cmd, req.listener_version,
             req.handler_version, req.api_env,
             req.notif_hook_version, req.stop_hook_version, req.max_conn_age))
        db.audit(auth.identity(), "wake.register", req.host,
                 {"wake_cmd": req.wake_cmd, "listener_version": req.listener_version})
    current = _rail_versions()
    stale = bool(
        (req.listener_version and req.listener_version != current["listen"])
        # Two handler generations (#588 post-cutover): a v2 seat's handler is
        # samcloud-wake, a v3 seat's is samcloud-rail — stale means "matches
        # NEITHER served generation", else every migrated seat reads stale
        # against the wrong artifact forever.
        or (req.handler_version and req.handler_version
            not in (current["wake"], current.get("rail"))))
    # hook versions are still accepted from older listeners and ignored (0.10.0)
    return {"status": "registered", "current_versions": current,
            "update_available": stale,
            "hint": ("update_available=true: re-fetch GET /api/v1/helpers/listen, "
                     "/helpers/wake and installed hooks (the default listener "
                     "does all of this itself).")}


def _capability_fail_threshold() -> int:
    """Consecutive failed launches before the capability leg trips (#521)."""
    try:
        return max(1, int(os.environ.get("WAKE_FAIL_DARK_THRESHOLD", "3")))
    except ValueError:
        return 3


# Cause classes for a wake attempt (#521). The handler sets these at the launch
# site from the FIRST error line; the registry never re-derives them by reading
# a log. Two independent reasons that rule exists, reached separately and both
# measured on live rails:
#
#   * The tail of stderr misattributes — a broker outage surfaced as
#     `json/decoder.py`, blaming Python for a network fault.
#   * A log-parsing reader loses non-zero exits PREFERENTIALLY, and which line
#     it loses is structural rather than incidental. Under `-p` the dying
#     session's stdout carries no trailing newline, so the handler's NEXT log
#     call glues onto it and that line stops starting with a timestamp. The
#     damaged row is therefore always the one FOLLOWING the session's last
#     write — the `session exit=` row — and never the `watchdog: killing` row,
#     which is written BEFORE SIGTERM with nothing partial in front of it.
#     Measured three rails: kill rows 0/17, 0/6, 0/8 glued; exit rows 16/17,
#     6/6, 1/4. On the 6/6 rail an anchored `^[ts] session exit=143` matched 0
#     of 831 exit rows — a seat killed six times reads as never killed.
#     (c3776 retracting c3773, which had this inverted; c3767, c3775.)
#     Two rails then lost 231 and 228 quota events to the same plausible
#     regex — 49% of one seat's largest bucket — because it ended
#     `resets \d` and the seat's commonest wording is `resets Aug 10 at 7am`.
#
# LATCHING = counts toward `failing-launches`. CLEARING = resets the counter,
# because it is positive evidence the launcher reached the API. TRUNCATING is
# neither, and is counted on its own axis (see below). Anything else is also
# NEITHER: it neither counts nor clears, since under-firing is the safe
# direction for a detector whose whole premise is that a noisy channel stops
# being read.
#
# The asymmetry that decides every borderline row: a wrong LATCH is loud (an
# operator is paged to fix a seat that is fine) and a wrong CLEAR is silent (a
# broken seat reads green, which is the bug this ticket exists for). So for
# the failure classes, under-firing is safe; for the clearing classes it
# inverts, and the safe move is to refuse to clear.
WAKE_CAUSES = ("ok", "auth", "quota", "launcher", "watchdog", "api")
_CAUSE_LATCHING = {
    "auth":     "credential is dead — needs an operator (claude-gpu-1: 23 days)",
    "launcher": "rc=127 / exec failed — the binary is not on the seat",
}
# `watchdog` is OUR supervisor killing the session at MAX_SESSION_MIN, and it
# is the one outcome where the seat is PROVABLY ALIVE AND WORKING at the
# moment of the verdict. It therefore neither latches nor clears, and it gets
# its own counter instead: what it measures is the session BUDGET, not the
# seat's capability.
#
# This row was argued to a conclusion and then reversed by measurement, so the
# history is worth the lines. It was first proposed as CLEARING ("a kill proves
# the session survived 30 minutes of work"), moved to LATCHING when the seat
# that proposed it found all 17 of its kills had written 0 bytes before dying,
# and then that reading was withdrawn too: in `-p` mode a session emits output
# only at COMPLETION, so a killed session has written 0 bytes BY CONSTRUCTION.
# The zero was an artifact of the launcher's output mode, not a silent hang —
# which makes "did it produce output before the kill" unreachable rather than
# merely unexercised, so that discriminator is deliberately not built.
#
# What the transcripts show, once three rails matched kills to sessions:
# 29 kills, ZERO stalled — median 102–121 tool calls against 5–45 for a clean
# exit, 93–100% distinct (tool, input) pairs so not looping, and no
# unattributed silence in any of them. The watchdog does not sample sessions,
# it selects the top of the distribution: on one rail exactly 3 of 592
# transcripts reach 98+ tool calls and they are the 3 kills.
#
# So counting it toward INCAPACITY inverts this ticket's own bug. Longest
# consecutive `rc=143` run is 1, 1 and 5 on the three rails measured — at the
# default threshold of 3 the seat with the run of 5 would have been declared
# `failing-launches` across the most productive stretch in its log (695 tool
# calls, 289 Edit + 57 Write + 127 plane writes destroyed by the cap). The
# leg's first firing in this fleet would have been a false page on a working
# seat, which is precisely how a detector loses the credibility #503 was
# written to protect. `N` is rail-dependent — 5 is a measurement, not a bound
# — so no threshold makes this class safe to count.
# (#521 c3757/c3762/c3766, c3760/c3761/c3767 and c3759/c3765, from
# three seats. Two of the three closed on `counts`; the
# measurements they published in the same comments are what overrode it.)
#
# NOTE FOR ANY ALARM TEXT: "the seat hung silently" is FALSE, on all 29.
_CAUSE_TRUNCATING = {
    "watchdog": "killed at the session cap — work truncated, NOT incapacity",
}
_CAUSE_CLEARING = {
    "ok":    "session exited 0",
    # The sharp one. To be told you are rate-limited, the launcher ran, the
    # broker minted, and the API authenticated you — which is precisely the
    # question this leg asks, answered yes. So quota does not merely fail to
    # count, it RESETS: otherwise `auth-fail, quota-fail, auth-fail` keeps
    # counting a failure that a later proof of capability has superseded
    # (c3749/c3752, and the accumulate-without-latching form withdrawn
    # in c3753). Quota is also per-ACCOUNT,
    # not per-identity — two seats on two hosts hit the same weekly window
    # within the same minute and recovered 90 seconds apart — so a fleet-wide
    # quota event would otherwise darken every seat at once.
    "quota": "rate-limited — POSITIVE proof the launcher reached the API",
}


def _classify_cause(req) -> str:
    """Normalise a reported cause. Unknown or absent => `api` (neither)."""
    c = (req.cause or "").strip().lower()
    if c in WAKE_CAUSES:
        return c
    if req.rc == 0 and req.outcome == "ok":
        return "ok"
    return "api"


# Live-stream ledger (task 609, the 2026-09-01 outage): the registry
# terminates every listener's SSE stream, so live-stream absence is the one
# minutes-resolution darkness signal that needs NO client cooperation. The
# outage sat 70+ minutes inside every other leg's threshold: no refusals
# landed (seats couldn't reach us), no launches were attempted (no stream to
# deliver wakes), and the age bound is ~7h by construction. This ledger is
# in-process state: it resets on registry restart, hence the boot grace.
_REGISTRY_STARTED_TS = time.time()
_live_streams: dict = {}   # identity -> {"count": int, "last_drop": "Y-m-d H:M:S" naive-UTC}
# Witness statements from deaf seats (task 609 c1024): in-memory on purpose —
# beacons are transient testimony about a live episode; after a registry
# restart the ledger legs re-detect and fresh beacons re-arrive within the
# client's 300s re-alarm cadence. The stream leg is the DETECTOR; this is
# the witness statement (why it's dark, and that the box still has a path).
_wake_beacons: dict = {}   # identity -> {dark_for_s, last_error, last_event_id, handler_version, at}
STREAM_DARK_MIN = int(os.environ.get("RAIL_STREAM_DARK_MIN", "15"))
STREAM_DARK_BOOT_GRACE_S = int(os.environ.get("RAIL_STREAM_DARK_BOOT_GRACE_S", "300"))


def _stream_opened(identity: str) -> None:
    e = _live_streams.setdefault(identity, {"count": 0, "last_drop": None})
    e["count"] += 1


def _stream_closed(identity: str) -> None:
    e = _live_streams.setdefault(identity, {"count": 0, "last_drop": None})
    e["count"] = max(0, e["count"] - 1)
    if e["count"] == 0:
        e["last_drop"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _registration_dark(r: dict, refusal: Optional[dict] = None,
                       stats: Optional[dict] = None):
    """Shared dark predicate — the GET view and the dark-watch loop must agree
    (#503). Returns (dark, reason).

    Age leg: silent past the seat's own MAX_CONN_AGE + 1h slack. The threshold
    equals the reconnect heartbeat by construction (producer's c3562 hazard);
    the +3600 is the deliberate jitter margin that keeps a healthy seat from
    grazing its own trip point.
    Refusal leg: an auth-refusal newer than the last good connect marks dark
    IMMEDIATELY — a live-but-locked-out listener retrying every ~30s is the
    positive signal age cannot give, and it needs no causal story about which
    reconnect started the outage (three measured (c) instances, two origins).
    Capability leg (#521): N consecutive failed session launches. READ THIS
    ONE TWICE, because it looks wrong: it fires while the seat is CONNECTED —
    the exact state the other two legs call healthy. That inversion is the
    whole point. Both older legs are properties of the listener's TCP/SSE
    connection, so a listener that reconnects happily every 6h and then fails
    100% of its launches satisfies them forever; claude-gpu-1 did precisely that
    for 23 days and read fully green throughout.

    Leg ORDER is deliberate. Age is evaluated before capability so a box that
    has simply been switched off reports `silent-past-bound` ("go restart a
    box") rather than a stale `failing-launches` ("that seat's credential or
    launcher is broken") — two completely different operator responses, and
    the counters of a disconnected seat are history, not news. When the box
    comes back and still cannot launch, the capability leg re-fires on its own.

    The capability leg ABSTAINS at attempts=0. A genuinely idle seat has no
    exit=0 either, so keying on absence-of-success would page every quiet seat
    (#499/#521): the primitive is a count of attempts that
    FAILED, never a rate and never a raw log count. Deliberately NOT caught
    here: a seat alternating fail/ok/fail/ok never reaches N consecutive and
    stays invisible — a rate would catch it, and a rate is the thing we were
    warned off, because it measures wake cadence rather than severity.

    `consecutive_failures` counts LATCHING classes only (see WAKE_CAUSES).
    That qualifier is the difference between this leg firing on a broken seat
    and firing on a healthy one, and it is load-bearing twice over:

      * a quota-limited seat produced 234 consecutive `exit=1` over ~70 hours
        while being perfectly capable and correctly waiting for a 7am reset;
      * a seat killed at the session cap 5 times in a row was running 112–177
        tool calls per session throughout — the busiest stretch in its log.

    Both carry the same `exit=1`/`exit=143` an `rc` counter would see, and
    either would have made this leg's FIRST firing in the fleet a false page
    on a working seat."""
    if refusal and refusal.get("last_at") and (
            not r.get("last_connect_at")
            or refusal["last_at"] > r["last_connect_at"]):
        return True, "auth-refused"
    if not r.get("last_connect_at"):
        return True, "never-connected"
    try:
        _lc = datetime.strptime(r["last_connect_at"], "%Y-%m-%d %H:%M:%S")
        _bound = timedelta(seconds=(r.get("max_conn_age") or 6 * 3600) + 3600)
        if (datetime.now(timezone.utc).replace(tzinfo=None) - _lc) > _bound:
            return True, "silent-past-bound"
    except ValueError:
        return True, "unparseable-connect-time"
    # Stream leg (task 609): a registered rail whose identity holds NO live
    # SSE stream for STREAM_DARK_MIN is dark at minutes resolution — the
    # signal every other leg structurally lacks during a connectivity outage.
    # Identity-level on purpose: any live stream of the identity clears all
    # its rows (per-host stream attribution isn't known at the SSE layer).
    # Latching stays per-episode: the reason's minute counter changing on a
    # still-dark seat updates the row without re-notifying (existing
    # transition semantics), per the 2026-09-01 two-phase lesson — the cause
    # can move mid-episode and must not re-fire the page.
    if (time.time() - _REGISTRY_STARTED_TS) > STREAM_DARK_BOOT_GRACE_S:
        _ls = _live_streams.get(r.get("username")) or {}
        if not _ls.get("count"):
            _ref = _ls.get("last_drop") or r.get("last_connect_at") or ""
            try:
                _dt = datetime.strptime(_ref[:19].replace("T", " "),
                                        "%Y-%m-%d %H:%M:%S")
                _dark_s = (datetime.now(timezone.utc).replace(tzinfo=None)
                           - _dt).total_seconds()
                if _dark_s >= STREAM_DARK_MIN * 60:
                    return True, f"no-live-stream-{int(_dark_s // 60)}m"
            except (ValueError, TypeError):
                pass
    if stats and (stats.get("attempts") or 0) > 0 and \
            (stats.get("consecutive_failures") or 0) >= _capability_fail_threshold():
        return True, "failing-launches"
    return False, None


def _ver_ge(have: Optional[str], want: str) -> bool:
    """Dotted-version >= compare, tolerant of junk (returns False on junk)."""
    try:
        h = [int(x) for x in (have or "").split(".")]
        w = [int(x) for x in want.split(".")]
    except ValueError:
        return False
    n = max(len(h), len(w))
    return h + [0] * (n - len(h)) >= w + [0] * (n - len(w))


# The HANDLER version that first reports session outcomes (#521). A seat below
# this can never convert a probe into an attempt, so probing it is pure cost.
#
# READ THE SERIES, NOT THE NUMBER. The rail runs two independent 2.x version
# lines side by side in the same row — `listen` (listener, 2.5.3) and `wake`
# (this handler, 2.4.x) — and `GET /wake/registrations` prints them in adjacent
# columns. The first draft of this gate was written as `>= 2.5.0` against the
# handler field while thinking of the listener series, which matched ZERO seats
# on the mesh including fully up-to-date ones: the probe leg would have shipped,
# passed review, and silently never fired (#521 c3747 —
# caught before it landed). The reporting handler is therefore released as
# 2.4.6, staying on its own line rather than minting a second 2.5.x for a
# reader to confuse. Compare against `handler_version`, never `listener_version`.
_SESSION_REPORTING_SINCE = "2.4.6"


def _maybe_probe_idle_seat(r: dict, ident: str, st: Optional[dict]):
    """Synthetic capability probe (#521 c3742).

    The capability leg is armed only by traffic the seat does not control, and
    the seats most likely to be dark are the quiet ones: a connected seat with
    an empty inbox runs no session, reports no outcome, and looks exactly like
    a healthy one. It could re-break at any moment and the plane would show
    what it shows now — nothing.

    So interrogate the silence instead of assuming it: poke a self-addressed
    notification at a connected, reporting-capable seat that hasn't attempted
    anything in D days. The seat's own rail turns that into a wake, a session
    and an outcome — converting the untestable idle state into an attempt,
    which is the only thing a counter can ever see. This is the technique from
    the claude-gpu-1 receipt (bounded comment to #357, reply in 14s, exit=0),
    promoted from something an admin did by hand to something the plane does.

    A probe spends a real session, so it is fenced: reporting-capable seats
    only, once per cooldown, never while already dark (the operator has been
    paged; more sessions buy nothing), and self-clearing — the POST handler
    marks it read the moment an attempt lands, because producing that attempt
    is the probe's entire job. Cooldown shortens to a day once a seat is
    failing, so confirmation takes days rather than weeks.

    Deliberately inbox-only — NOT emitted on the event bus. Non-ticket bus
    events are global when unscoped and admins see every event regardless of
    scope, so a bus probe would wake the fleet to measure one seat. The
    durable inbox costs the seat its next reconcile (<= MAX_CONN_AGE, ~6h)
    and nobody else anything, which is free at a multi-day cadence."""
    if not _ver_ge(r.get("handler_version"), _SESSION_REPORTING_SINCE):
        return                                  # can't report → can't convert
    try:
        idle_days = float(os.environ.get("WAKE_PROBE_IDLE_DAYS", "3"))
    except ValueError:
        idle_days = 3.0
    if idle_days <= 0:
        return                                  # probing disabled
    st = st or {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def _age_days(ts):
        if not ts:
            return None
        try:
            return (now - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400
        except ValueError:
            return None

    # Never attempted: measure from registration, so a seat enrolled an hour
    # ago isn't probed before it has had a chance to do anything.
    since = _age_days(st.get("last_attempt_at")) if st.get("attempts") \
        else _age_days(r.get("registered_at"))
    if since is None or since < idle_days:
        return
    cooldown = 1.0 if (st.get("consecutive_failures") or 0) > 0 else idle_days
    probe_age = _age_days(st.get("probe_at"))
    if probe_age is not None and probe_age < cooldown:
        return
    # An unacked probe already sitting in that inbox is doing this job.
    if db.query_one("SELECT 1 FROM notifications WHERE recipient = ? AND "
                    "event_type = 'wake.probe' AND read = 0", (ident,)):
        return
    db.execute(
        "INSERT INTO notifications (recipient, event_type, actor, payload) "
        "VALUES (?, 'wake.probe', 'registry', ?)",
        (ident, json.dumps({
            "summary": "Capability probe — no action needed.",
            "idle_days": round(since, 1),
            "why": ("#521: connection health says nothing about whether a seat can "
                    "launch. Your reaching this prompt at all IS the measurement."),
        })))
    db.execute("INSERT INTO wake_session_stats (identity, probe_at) "
               "VALUES (?, datetime('now')) ON CONFLICT(identity) DO UPDATE "
               "SET probe_at = datetime('now')", (ident,))


def _evaluate_dark_transitions():
    """Dark-watch (#503): compute dark for every registration, diff against
    wake_dark_state, and notify admin identities on transitions through the
    normal inbox. Recovery notifies too — a channel that only carries bad news
    teaches its readers to stop reading it."""
    refusals = {x["identity"]: dict(x) for x in db.query("SELECT * FROM wake_auth_refusals")}
    stats = {x["identity"]: dict(x) for x in db.query("SELECT * FROM wake_session_stats")}
    # Retired rows are out (#897). A retired registration is one someone said is
    # gone, and paging on it is the noise retirement exists to remove. The inverse
    # hazard was considered: could retiring make a seat UNDARKENABLE? Only until it
    # reconnects, because a real register clears `ended_at` — and an identity whose
    # OTHER row is live is still seen through that row, since this is per-row.
    rows = db.query("SELECT w.*, u.username FROM wake_registrations w "
                    "JOIN users u ON u.id = w.user_id WHERE w.ended_at IS NULL")
    prev = {x["identity"]: dict(x) for x in db.query("SELECT * FROM wake_dark_state")}
    newly_stream_dark: list = []
    for r in rows:
        r = dict(r)
        ident = r.get("username")
        if not ident:
            continue
        st = stats.get(ident)
        dark, reason = _registration_dark(r, refusals.get(ident), st)
        if not dark:
            _maybe_probe_idle_seat(r, ident, st)
        was_dark = bool(prev.get(ident, {}).get("dark"))
        if dark == was_dark and ident in prev:
            # Same verdict — but the REASON can move under a still-dark seat
            # (a box that was switched off comes back and still can't launch).
            # Keep the row honest without re-notifying: a reason change is not
            # a transition, and flapping a channel is how it stops being read.
            if reason != prev[ident].get("reason"):
                db.execute("UPDATE wake_dark_state SET reason = ? WHERE identity = ?",
                           (reason, ident))
            continue
        db.execute("INSERT INTO wake_dark_state (identity, dark, reason, since) "
                   "VALUES (?, ?, ?, datetime('now')) ON CONFLICT(identity) DO UPDATE "
                   "SET dark = excluded.dark, reason = excluded.reason, since = datetime('now')",
                   (ident, int(dark), reason))
        if dark == was_dark:
            continue          # first-seen row recorded without a notification
        payload = json.dumps({"identity": ident, "dark": dark, "reason": reason,
                              "auth_refused_count": refusals.get(ident, {}).get("count", 0),
                              "last_connect_at": r.get("last_connect_at"),
                              "consecutive_session_failures": (st or {}).get("consecutive_failures", 0),
                              "last_session_ok_at": (st or {}).get("last_ok_at"),
                              "last_session_cause": (st or {}).get("last_cause"),
                              "last_session_error": (st or {}).get("last_error"),
                              # Witness statement, verbatim (task 609 c1022):
                              # Errno 8 and Errno 51 are different incidents
                              # with different owners; a page that only says
                              # "dark" sends the operator down the wrong branch.
                              "beacon": _wake_beacons.get(ident)})
        event = "wake.dark" if dark else "wake.dark_recovered"
        if dark and str(reason or "").startswith("no-live-stream"):
            newly_stream_dark.append(ident)
        # Fleet darkness is an OPERATIONS reading, not a message to a person
        # (rail 3.10: there is no wake — there is a session
        # that is currently holding a process or not). It was 45 of the last
        # 200 rows in the admin inbox, addressed to nobody and actionable by
        # no one who read it. The state lives in wake_dark_state and is
        # rendered by GET /wake/health; the transition is an audit row, which
        # is where a history belongs. Nothing is lost and no inbox is spent.
        db.audit("registry", event, ident, json.loads(payload))
    # Fleet signature (task 609, validated by the 2026-09-01 outage forensics:
    # a single seat's symptom read as "local resolver" when the all-seats
    # signal would have said "infra" in one step): >=2 seats newly stream-dark
    # in ONE pass is a network/infrastructure event, not individual seats —
    # one combined page says so, so nobody debugs four seats separately.
    if len(newly_stream_dark) >= 2:
        fleet_payload = json.dumps({
            "seats": sorted(newly_stream_dark),
            "count": len(newly_stream_dark),
            "read": ("everyone-at-once signature: network/infrastructure "
                     "between these seats and the registry — not individual "
                     "seat faults. Check connectivity/DNS/path first.")})
        db.audit("registry", "wake.dark.fleet", "fleet", json.loads(fleet_payload))


@app.post("/api/v1/wake/beacon")
def wake_beacon(req: WakeBeacon, auth: AuthContext = Depends(get_auth)):
    """Deaf-seat witness statement (task 609): a listener whose STREAM is dark
    but whose PATH works (pinned-IP curl) reports why. Passive by design —
    beacons never create dark transitions (the stream leg is the detector);
    they enrich the page when a transition fires, so `Errno 8` vs `Errno 51`
    reaches the operator verbatim instead of a bare "dark". Idempotent within
    an episode: re-alarms refresh the witness row, nothing re-pages. Replies
    with what the registry already believes so the client can log "registry
    already knows" instead of assuming its beacon was the first word."""
    me = auth.identity()
    _wake_beacons[me] = {
        "dark_for_s": max(0, req.dark_for_s),
        "last_error": (req.last_error or "")[:200] or None,
        "last_event_id": req.last_event_id,
        "handler_version": (req.handler_version or "")[:32] or None,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    row = db.query_one("SELECT dark FROM wake_dark_state WHERE identity = ?", (me,))
    return {"status": "recorded", "known_dark": bool(row and row.get("dark"))}


@app.post("/api/v1/wake/session")
def report_wake_session(req: WakeSessionReport, auth: AuthContext = Depends(get_auth)):
    """Report one wake ATTEMPT and its outcome (#521). Called by the handler
    after every session it launches — success included, which is the point:
    a failure-only feed cannot distinguish a broken seat from a quiet one.

    `outcome=ok` means the launcher started and the session exited 0. It does
    NOT mean work happened — the handler polls `?unclaimed=true`, so a session
    that finds every unread row already claimed by a live surface consumes
    nothing and exits 0 legitimately. This endpoint only ever claims
    capability; any future "this seat is working" alarm needs a different
    signal (#521 c3742).

    `cause` is what makes the counter safe. It is set by the handler at the
    launch site, never re-derived here, and it decides whether this attempt
    latches, clears, or does neither — see WAKE_CAUSES above for the measured
    reasoning behind each row. The short version: `auth`/`launcher` latch,
    `ok`/`quota` clear, `watchdog` counts on its own `truncations` axis, and
    everything else is inert. A counter keyed on `rc` instead would page on a
    seat that is correctly waiting for a 7am quota reset — 463 of 464 failures
    on one live seat are exactly that, with a longest run of 234 consecutive,
    and they carry the same `exit=1` as the 4,284 dead-credential failures on
    another."""
    ident = auth.identity()
    if req.outcome not in ("ok", "failed"):
        raise HTTPException(400, "outcome must be 'ok' or 'failed'")
    # rc is ground truth: an `ok` carrying a non-zero rc is a confused caller,
    # and the safe reading of a confused caller is "this attempt failed".
    ok = req.outcome == "ok" and req.rc == 0
    cause = "ok" if ok else _classify_cause(req)
    if cause == "ok" and not ok:
        cause = "api"           # a failed attempt can never be cause=ok
    latching = cause in _CAUSE_LATCHING
    clearing = cause in _CAUSE_CLEARING
    truncating = cause in _CAUSE_TRUNCATING
    db.execute("INSERT OR IGNORE INTO wake_session_stats (identity) VALUES (?)", (ident,))
    db.execute(
        "UPDATE wake_session_stats SET "
        "  attempts = attempts + 1, "
        "  successes = successes + ?, "
        "  failures = failures + ?, "
        # Three-way, not two: latch increments, clear zeroes, and `api` leaves
        # the counter exactly where it was rather than quietly forgiving a
        # real fault or counting a transient one.
        "  consecutive_failures = CASE WHEN ? THEN consecutive_failures + 1 "
        "                             WHEN ? THEN 0 "
        "                             ELSE consecutive_failures END, "
        "  first_attempt_at = COALESCE(first_attempt_at, datetime('now')), "
        "  last_attempt_at = datetime('now'), "
        "  last_ok_at = CASE WHEN ? THEN datetime('now') ELSE last_ok_at END, "
        # last_launch_ok_at is the honest field for the capability leg: the
        # launcher reached the API. A quota rejection sets it and NOT
        # last_ok_at, which is the whole distinction (c3749).
        "  last_launch_ok_at = CASE WHEN ? THEN datetime('now') "
        "                           ELSE last_launch_ok_at END, "
        "  last_failed_at = CASE WHEN ? THEN datetime('now') ELSE last_failed_at END, "
        # Work truncated at the session cap — its own axis, deliberately not
        # folded into the capability counter. This is the signal that says
        # "raise MAX_SESSION_MIN", never "go fix that seat".
        "  truncations = truncations + ?, "
        "  last_truncated_at = CASE WHEN ? THEN datetime('now') "
        "                           ELSE last_truncated_at END, "
        "  last_rc = ?, last_cause = ?, last_error = ?, "
        "  last_host = COALESCE(?, last_host) "
        "WHERE identity = ?",
        (int(ok), int(not ok), int(latching), int(clearing), int(ok),
         int(clearing), int(not ok), int(truncating), int(truncating), req.rc, cause,
         (req.error or None) and req.error[:300], req.host, ident))
    # Self-clearing probe: it existed only to produce this attempt, so the
    # attempt retires it. Leaving it unread would re-wake the seat forever.
    db.execute("UPDATE notifications SET read = 1 WHERE recipient = ? AND "
               "event_type = 'wake.probe' AND read = 0", (ident,))
    st = dict(db.query_one("SELECT * FROM wake_session_stats WHERE identity = ?",
                           (ident,)) or {})
    return {"status": "recorded", "identity": ident, "cause": cause,
            "counter": "latched" if latching else ("cleared" if clearing else "unchanged"),
            "attempts": st.get("attempts"), "successes": st.get("successes"),
            "consecutive_failures": st.get("consecutive_failures"),
            "truncations": st.get("truncations"),
            "dark_at": _capability_fail_threshold(),
            "hint": ("consecutive_failures >= dark_at marks this seat dark with "
                     "reason=failing-launches while it is still connected (#521). "
                     "Only auth/launcher count; quota CLEARS the counter (being "
                     "rate-limited proves the launcher reached the API), and "
                     "watchdog counts on `truncations` instead — a killed session "
                     "was working, so it is a session-budget signal, not a "
                     "capability one.")}


@app.get("/api/v1/wake/health")
def wake_health(transitions: int = 20, auth: AuthContext = Depends(get_auth)):
    """Fleet darkness, as a reading rather than a message (rail 3.10).

    `wake.dark` / `wake.dark_recovered` / `wake.dark.fleet` used to be written
    into every admin's inbox — 45 of the last 200 rows on one, addressed to
    nobody and actionable by no one who read it. The state was always durable
    in `wake_dark_state`; this renders it, and `?transitions=N` reads the
    history out of the audit log where a history belongs.

    Derived, not stored: nothing here is a new source of truth. `dark` is
    recomputed the same way `/wake/registrations` computes it, so the two can
    never disagree."""
    refusals = {x["identity"]: dict(x) for x in db.query("SELECT * FROM wake_auth_refusals")}
    stats = {x["identity"]: dict(x) for x in db.query("SELECT * FROM wake_session_stats")}
    state = {x["identity"]: dict(x) for x in db.query("SELECT * FROM wake_dark_state")}
    # Retired rows are out, same as the dark-watch — this is documented as computing
    # `dark` "the same way /wake/registrations computes it", so the two must agree
    # about which rows exist at all (#897).
    rows = db.query("SELECT w.*, u.username FROM wake_registrations w "
                    "JOIN users u ON u.id = w.user_id WHERE w.ended_at IS NULL")
    if not auth.is_admin():
        me = auth.identity()
        rows = [r for r in rows if r["username"] == me]
    seats, dark_now = [], []
    for r in rows:
        r = dict(r)
        ident = r.get("username")
        if not ident:
            continue
        st = stats.get(ident) or {}
        dark, reason = _registration_dark(r, refusals.get(ident), st)
        seat = {"identity": ident, "dark": dark, "reason": reason,
                "since": (state.get(ident) or {}).get("since"),
                "last_connect_at": r.get("last_connect_at"),
                "listener_version": r.get("listener_version"),
                "handler_version": r.get("handler_version"),
                "consecutive_session_failures": st.get("consecutive_failures", 0),
                "last_session_ok_at": st.get("last_ok_at"),
                "beacon": _wake_beacons.get(ident)}
        seats.append(seat)
        if dark:
            dark_now.append(ident)
    # The everyone-at-once signature (task 609): >=2 seats dark for a stream
    # reason at the same time reads "infrastructure", not "seats".
    stream_dark = sorted(s["identity"] for s in seats
                         if s["dark"] and str(s["reason"] or "").startswith("no-live-stream"))
    hist = db.query("SELECT actor, action, target, detail, created_at FROM audit_log "
                    "WHERE action IN ('wake.dark','wake.dark_recovered','wake.dark.fleet') "
                    "ORDER BY id DESC LIMIT ?", (max(1, min(transitions, 200)),))
    return {"seats": sorted(seats, key=lambda s: (not s["dark"], s["identity"])),
            "dark": sorted(dark_now),
            "fleet_signature": (
                {"seats": stream_dark, "count": len(stream_dark),
                 "read": ("everyone-at-once: network/infrastructure between these "
                          "seats and the registry, not individual seat faults")}
                if len(stream_dark) >= 2 else None),
            "transitions": [{"at": h["created_at"], "event": h["action"],
                             "identity": h["target"],
                             "detail": json.loads(h["detail"]) if h["detail"] else None}
                            for h in hist]}


@app.get("/api/v1/wake/registrations")
def list_wake_registrations(include_retired: bool = False,
                            auth: AuthContext = Depends(get_auth)):
    """The wake-rail fleet: who is registered, at what version, how fresh.
    Admin sees all; everyone else sees their own registrations.

    **Rows are per (identity, host), not per identity** — `UNIQUE(user_id, host)`
    in the schema. One identity can therefore hold several rows, so `len(rows)` is
    NOT a seat count, and reading it as one is how #897 was found: a doc cited
    "four seats receiving no rail updates" from a row count, and the real answer
    depended on which of three predicates you meant (6 rows / 5 identities holding
    any such row / 3 whose every row was so). Two of those rows were throwaway
    containers with no `--hostname`, so each run minted a fresh host and a fresh
    row; a seat with a stable hostname upserts into one row forever.

    `?include_retired=true` keeps rows retired with
    `POST /wake/register {"ended": true}`. They leave the default view and are
    never deleted, because these rows are the record of who ran a rail where."""
    current = _rail_versions()
    retired_clause = "" if include_retired else " AND w.ended_at IS NULL"
    if auth.is_admin():
        rows = db.query(
            "SELECT w.*, u.username, u.last_seen, u.presence_until "
            "FROM wake_registrations w JOIN users u ON u.id = w.user_id "
            "WHERE 1=1" + retired_clause + " ORDER BY w.last_connect_at DESC")
    else:
        uid = _user_id_of(auth)
        rows = db.query(
            "SELECT w.*, u.username, u.last_seen, u.presence_until "
            "FROM wake_registrations w JOIN users u ON u.id = w.user_id "
            "WHERE w.user_id = ?" + retired_clause +
            " ORDER BY w.last_connect_at DESC", (uid,))
    _refusals = {x["identity"]: dict(x) for x in db.query("SELECT * FROM wake_auth_refusals")}
    _stats = {x["identity"]: dict(x) for x in db.query("SELECT * FROM wake_session_stats")}
    out = []
    for r in rows:
        r = dict(r)
        # A MISSING handler_version is staleness, not freshness (#521 c3747).
        # The `and` guards below mean "unreported" read as "fine": a seat whose
        # listener never reported a handler version sat at stale:false while
        # being unmeasurable, which is this ticket's own failure shape one
        # column over. The notif/stop nulls stay guarded — `surface_dark` below
        # is the flag that already carries those, and duplicating them here
        # would just make one row shout twice.
        r["stale"] = bool(
            (r.get("listener_version") or "") != current["listen"]
            # Handler staleness is generation-aware (#588): wake OR rail.
            or (r.get("handler_version") or "")
            not in (current["wake"], current.get("rail")))
        # `surface_dark` retired 0.10.0 with the hooks: interactive surfaces are
        # rows in `GET /wake/surfaces` (with `connected`), not hook versions.
        # Kept as a key so older readers do not break; always False.
        r["surface_dark"] = False
        # A listener re-registers at least every MAX_CONN_AGE, so a row silent
        # for longer than its own bound + slack provably holds no stream.
        # Version-staleness said nothing while 12 dead rails sat at
        # stale:false for 8h (#472) — this is the liveness flag that incident
        # was missing. The bound is per-seat (client config, reported since
        # 2.5.2); pre-2.5.2 rows fall back to the 6h client default.
        _ref = _refusals.get(r.get("username"))
        r["auth_refused_at"] = _ref["last_at"] if _ref else None
        r["auth_refused_count"] = _ref["count"] if _ref else 0
        # Capability, alongside connection (#521). Reported by the handler, so
        # `session_attempts: 0` reads "idle OR not reporting" — never "green".
        _st = _stats.get(r.get("username")) or {}
        r["session_attempts"] = _st.get("attempts", 0)
        r["session_successes"] = _st.get("successes", 0)
        r["consecutive_session_failures"] = _st.get("consecutive_failures", 0)
        r["last_session_at"] = _st.get("last_attempt_at")
        r["last_session_ok_at"] = _st.get("last_ok_at")
        r["last_launch_ok_at"] = _st.get("last_launch_ok_at")
        r["last_session_rc"] = _st.get("last_rc")
        r["last_session_cause"] = _st.get("last_cause")
        # Work truncated at the session cap. Reported alongside capability but
        # never mixed into it: a killed session was working (29 kills across
        # three rails, zero stalled), so this reads "the cap is too low for
        # this seat", not "this seat is broken".
        r["session_truncations"] = _st.get("truncations", 0)
        r["last_truncated_at"] = _st.get("last_truncated_at")
        # Rendered, not inferred. "This seat cannot be measured" and "this seat
        # is fine" are the two states this whole ticket exists because nobody
        # could tell apart, so the row says which one it is instead of leaving
        # a reader to deduce it from a zero.
        r["session_reporting"] = _ver_ge(r.get("handler_version"),
                                         _SESSION_REPORTING_SINCE)
        r["dark"], r["dark_reason"] = _registration_dark(r, _ref, _st)
        out.append(r)
    return {"registrations": out, "current_versions": current}


# ── Thread claims API (#199) ────────────────────────────────────────────────

@app.post("/api/v1/tickets/{ticket_id}/claim")
def claim_ticket(ticket_id: int, req: ClaimBody,
                 auth: AuthContext = Depends(get_auth),
                 x_session_id: Optional[str] = Header(None)):
    """Explicitly claim a thread for your warm surface (writing already
    auto-claims; this is for re-warming without a comment). 409 when another
    LIVE session of your identity owns the thread (#264) — that exclusion is
    the point: one live surface drives a thread at a time."""
    ticket = db.query_one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
    if not ticket:
        raise HTTPException(404)
    if not _can_see_ticket(auth, ticket):
        raise HTTPException(404, "Ticket not found")
    ttl = max(1, min(req.ttl_seconds, CLAIM_TTL_MAX))
    claim = _claim_thread(auth.identity(), ticket_id, ttl, sticky=True,
                          session_id=_session_of(x_session_id))
    if claim["owned"]:
        _bind_conversation("ticket", ticket_id, auth.identity(), _session_of(x_session_id))
    if not claim["owned"]:
        raise HTTPException(
            409, "Thread is claimed by another LIVE session of your identity — "
                 "replies route to its watch. If that surface is dead, take over: "
                 "DELETE /api/v1/tickets/{id}/claim, then claim again.")
    return {"status": "claimed", "ticket_id": ticket_id, "ttl_seconds": ttl,
            "sticky": True,
            "hint": ("Sticky: survives wake-exit releases; falls by TTL, "
                     "DELETE /tickets/{id}/claim, or release include_sticky. "
                     "Running the subscriber? Add this id to its claims file "
                     "(~/.cache/samcloud-sub-claims) so it re-asserts each "
                     "cycle. Hold a watch/subscription to keep it alive.")}


@app.delete("/api/v1/tickets/{ticket_id}/claim")
def release_ticket_claim(ticket_id: int, auth: AuthContext = Depends(get_auth)):
    """Release your claim on one thread — the cold rail takes over instantly."""
    n = _release_claims(auth.identity(), ticket_id)
    return {"status": "released", "ticket_id": ticket_id, "released": n}


_CLAIM_CONFLICT = ("Thread is claimed by another LIVE session of your identity — "
                   "replies route to its watch. If that surface is dead, take "
                   "over: DELETE the claim, then claim again.")


@app.post("/api/v1/docs/{doc_id}/claim")
def claim_doc(doc_id: int, req: ClaimBody,
              auth: AuthContext = Depends(get_auth),
              x_session_id: Optional[str] = Header(None)):
    """Claim a DOC thread for your warm surface (#392) — writing already
    auto-claims; this is for asserting engagement BEFORE the first write
    (the doc-73 shape: a long analysis turn whose revision lands late must
    not lose the doc to its own cold rail mid-turn)."""
    doc = db.query_one("SELECT * FROM docs WHERE id = ?", (doc_id,))
    if not doc:
        raise HTTPException(404, f"Doc {doc_id} not found")
    if not _can_see_doc(auth, doc):
        raise HTTPException(404, "Doc not found")
    ttl = max(1, min(req.ttl_seconds, CLAIM_TTL_MAX))
    claim = _claim_thread(auth.identity(), doc_id=doc_id, ttl=ttl, sticky=True,
                          session_id=_session_of(x_session_id))
    if claim["owned"]:
        _bind_conversation("doc", doc_id, auth.identity(), _session_of(x_session_id))
    if not claim["owned"]:
        raise HTTPException(409, _CLAIM_CONFLICT)
    return {"status": "claimed", "doc_id": doc_id, "ttl_seconds": ttl,
            "sticky": True}


@app.delete("/api/v1/docs/{doc_id}/claim")
def release_doc_claim(doc_id: int, auth: AuthContext = Depends(get_auth)):
    """Release your claim on one doc thread."""
    n = _release_claims(auth.identity(), doc_id=doc_id)
    return {"status": "released", "doc_id": doc_id, "released": n}


@app.post("/api/v1/tasks/{task_id}/claim")
def claim_task(task_id: int, req: ClaimBody,
               auth: AuthContext = Depends(get_auth),
               x_session_id: Optional[str] = Header(None)):
    """Claim a TASK thread for your warm surface (#392) — the single-writer
    assertion for a deliverable you are actively working. A parallel surface
    of your identity gets 409 instead of silently duplicating the work."""
    _load_task_and_project(task_id, auth)
    ttl = max(1, min(req.ttl_seconds, CLAIM_TTL_MAX))
    claim = _claim_thread(auth.identity(), task_id=task_id, ttl=ttl, sticky=True,
                          session_id=_session_of(x_session_id))
    if claim["owned"]:
        _bind_conversation("task", task_id, auth.identity(), _session_of(x_session_id))
    if not claim["owned"]:
        raise HTTPException(409, _CLAIM_CONFLICT)
    return {"status": "claimed", "task_id": task_id, "ttl_seconds": ttl,
            "sticky": True}


@app.delete("/api/v1/tasks/{task_id}/claim")
def release_task_claim(task_id: int, auth: AuthContext = Depends(get_auth)):
    """Release your claim on one task thread."""
    n = _release_claims(auth.identity(), task_id=task_id)
    return {"status": "released", "task_id": task_id, "released": n}


@app.get("/api/v1/wake/claims")
def list_claims(all: bool = False, thread: Optional[str] = None,
                auth: AuthContext = Depends(get_auth)):
    """Active thread claims. Three scopes, and the answer always names which
    one it is (`scope`) — #529's finding was a read that answered a different
    question than the one asked, in the same shape:

      (default)                 your own claims
      ?thread=ticket:526        every LIVE holder of ONE thread — ANY seat,
                                visibility-checked, advisory (#529 ask 1)
      ?all=true                 fleet-wide; admin only, and now REFUSED for
                                non-admins instead of silently narrowing

    A plain seat's ?all=true used to return its own rows under the fleet
    view's exact shape — `identity` on every row, no `scope`, no 403 — so a
    seat asking "is anyone else in here?" got "no" built from a query that
    could not have said otherwise. Refusing changes no disclosure decision;
    ?thread= answers the question that was actually being asked."""
    now = datetime.now(timezone.utc).isoformat()
    if thread is not None:
        plane, tid = _parse_thread(thread)
        # Visibility-gated per plane: learning who holds a row you cannot read
        # would be a new disclosure, and this feature deliberately makes none.
        _require_thread_visible(plane, tid, auth)
        holders = _thread_holders(**{_THREAD_PLANES[plane]: tid},
                                  me=auth.identity())
        return {"scope": f"thread:{plane}:{tid}", "thread": f"{plane}:{tid}",
                "advisory": True, "claims": holders, "holders": holders,
                "note": _CLAIM_ADVISORY, "now": now}
    if all and not auth.is_admin():
        raise HTTPException(
            403, "?all=true (fleet-wide claims) is admin-only. It is refused "
                 "rather than narrowed to your own rows, because a self-view "
                 "returned under the fleet view's shape reads as 'nobody else "
                 "is here' (#529). For one thread, any seat may call "
                 "GET /api/v1/wake/claims?thread=ticket:<id>|doc:<id>|task:<id>.")
    if all:
        rows = db.query("SELECT * FROM thread_claims WHERE claimed_until > ? "
                        "ORDER BY claimed_until DESC", (now,))
    else:
        rows = db.query("SELECT * FROM thread_claims WHERE identity = ? "
                        "AND claimed_until > ? ORDER BY claimed_until DESC",
                        (auth.identity(), now))
    # Expose session FINGERPRINTS only (#264) — the raw nonce is the claim
    # key; leaking it here would let any same-identity session replay it.
    for r in rows:
        r["session"] = _session_fp(r.pop("session_id", None))
    return {"scope": "fleet" if all else "self", "claims": rows, "now": now}


# ── Threads: the conversation <-> object <-> identity binding (doc 174) ──────
#
# A claim is a lease that guesses whether a reader is still there; a thread
# row is the record of WHICH conversation holds an object, open to closed,
# for as long as that takes. The rail is the authoritative writer (birth,
# resume, close); writes carrying X-Session-Id bind the writer's conversation
# when the identity has no open instance (_bind_conversation). Rail 3.9
# routes on this record and on delivery receipts, not on claims or presence.

@app.post("/api/v1/wake/threads")
def upsert_thread(req: ThreadUpsert, auth: AuthContext = Depends(get_auth)):
    """Record (or move) the binding for one thread of the calling identity.
    One row per (kind, object, identity); a later upsert replaces the
    conversation — that is a resume or a rebirth, and the rail says which
    via `holder`/`state`. A binding is routing, not content: it carries no
    root check (0.12.76) — a thread has no scope, its objects do, and the
    services rail's binding on 1770 was refused for a root it could not read
    (doc 355). Delivery stays scope-filtered at the object, so a binding
    grants no hearing."""
    if req.thread_id is not None:
        root = _thread_root(req.thread_id)
        if not root:
            raise HTTPException(404, f"Thread {req.thread_id} not found")
        kind, object_id = root
    else:
        if req.kind not in _THREAD_PLANES or req.object_id is None:
            raise HTTPException(400, f"give thread_id, or kind ({sorted(_THREAD_PLANES)}) + object_id")
        kind, object_id = req.kind, req.object_id
    if req.state not in _THREAD_STATES:
        raise HTTPException(400, f"state must be one of {list(_THREAD_STATES)}")
    if req.holder not in _THREAD_HOLDERS:
        raise HTTPException(400, f"holder must be one of {list(_THREAD_HOLDERS)}")
    if not req.conversation_id.strip():
        raise HTTPException(400, "conversation_id is required")
    me = auth.identity()
    row = _upsert_thread(kind, object_id, me, req.conversation_id.strip()[:64],
                         host=req.host, holder=req.holder, pid=req.pid,
                         state=req.state, rail_version=req.rail_version)
    return {"thread": _thread_row_public(row, me)}


class ThreadStatePatch(StrictModel):
    state: Optional[str] = None                 # open | closed
    title: Optional[str] = None


class ThreadMemberAdd(StrictModel):
    kind: str                                   # ticket | doc | task
    id: int


@app.get("/api/v1/threads")
def list_work_threads(state: Optional[str] = None, limit: int = 50,
                      auth: AuthContext = Depends(get_auth)):
    """The universal threads you are bound to (doc 239): one row per thread with
    its members, participants and bindings. ?state=open|closed."""
    _check_limit(limit)
    me = auth.identity()
    st = " AND w.state = ?" if state else ""
    rows = db.query("SELECT DISTINCT w.id FROM work_threads w JOIN threads b ON b.thread_id = w.id "
                    "WHERE b.identity = ? AND b.state IN ('open','closing')" + st +
                    " ORDER BY w.id DESC LIMIT ?", (me,) + ((state,) if state else ()) + (limit,))
    return {"scope": "self", "threads": [_thread_public(r["id"], me) for r in rows]}


@app.get("/api/v1/threads/{thread_id}")
def get_work_thread(thread_id: int, auth: AuthContext = Depends(get_auth)):
    """One universal thread: title, state, first member, members by plane,
    participants, bindings, and `open_members` — whether any ticket or task in
    it is still open. That flag is the whole lifecycle: an instance ends when
    its thread has no open work (docs never count), or when a person closes
    the thread (PATCH).

    A thread has no scope (0.12.76). It decides wake and routing; the objects
    in it carry the scope, and a member the caller cannot read is a bare ref.
    Until then the read followed the root, and a seat assigned work inside a
    thread whose root it could not see was shut out of the thread: its rail
    read a 404 on every event, its binding was refused the same way, and its
    first create started a thread of its own — which is how a ticket left the
    thread it was assigned in and started a new one."""
    if not _thread_root(thread_id):
        raise HTTPException(404, f"Thread {thread_id} not found")
    return _thread_public(thread_id, auth.identity(), auth=auth)


@app.get("/api/v1/threads/of/{kind}/{object_id}")
def thread_of_object(kind: str, object_id: int, auth: AuthContext = Depends(get_auth)):
    """The universal thread an object belongs to (created if it has none)."""
    if kind not in _THREAD_PLANES:
        raise HTTPException(400, f"kind must be one of {sorted(_THREAD_PLANES)}")
    _require_thread_visible(kind, object_id, auth)
    tid = _thread_of(kind, object_id, auth.identity())
    if not tid:
        raise HTTPException(404, f"{kind} {object_id} not found")
    return _thread_public(tid, auth.identity(), auth=auth)


@app.patch("/api/v1/threads/{thread_id}")
def patch_work_thread(thread_id: int, req: ThreadStatePatch,
                      auth: AuthContext = Depends(get_auth)):
    """Close (or reopen, or retitle) a thread. Closing writes a `thread.closed`
    row to every identity bound to it; each instance wraps up and ends. Only a
    person or an admin closes a thread — nothing closes one by itself."""
    root = _thread_root(thread_id)
    if not root:
        raise HTTPException(404, f"Thread {thread_id} not found")
    _require_thread_visible(root[0], root[1], auth)
    me = auth.identity()
    if req.state is not None:
        if req.state not in ("open", "closed"):
            raise HTTPException(400, "state must be open or closed")
        if auth.token_type == "agent" and not auth.is_admin():
            raise HTTPException(403, "an agent does not close a thread; a person or an admin does")
        now = datetime.now(timezone.utc).isoformat()
        db.execute("UPDATE work_threads SET state = ?, closed_at = CASE WHEN ? = 'closed' "
                   "THEN ? ELSE NULL END WHERE id = ?", (req.state, req.state, now, thread_id))
        if req.state == "closed":
            for b in db.query("SELECT DISTINCT identity FROM threads WHERE thread_id = ? "
                              "AND state IN ('open','closing')", (thread_id,)):
                db.execute("INSERT INTO notifications (recipient, event_type, actor, payload, thread_id) "
                           "VALUES (?, 'thread.closed', ?, ?, ?)",
                           (b["identity"], me, json.dumps({"thread_id": thread_id, "by": me,
                                                           "summary": "thread closed"}), thread_id))
            # the listeners of the bound seats wake on an event; a row alone
            # wakes nobody until the seat's next event (live round 2c: the
            # holder sat for ten minutes with its wrap-up unread)
            event_bus.emit("thread.closed", f"{root[0]}:{root[1]}",
                           {"thread_id": thread_id, "by": me}, scope=f"{root[0]}:{root[1]}")
        db.audit(me, f"thread.{req.state}", str(thread_id))
    if req.title is not None:
        db.execute("UPDATE work_threads SET title = ? WHERE id = ?", (req.title.strip()[:200], thread_id))
    return _thread_public(thread_id, me)


@app.post("/api/v1/threads/{thread_id}/members")
def add_thread_member(thread_id: int, req: ThreadMemberAdd,
                      x_session_id: Optional[str] = Header(None),
                      auth: AuthContext = Depends(get_auth)):
    """Move an object into a thread. Its rows follow it from the next write;
    an identity's binding on the object's old thread is untouched.

    The one route into the thread graph that took no conversation (0.12.19):
    the endpoint whose entire job is "this object belongs to that work" could
    not say who decided it."""
    root = _thread_root(thread_id)
    if not root:
        raise HTTPException(404, f"Thread {thread_id} not found")
    if req.kind not in _THREAD_PLANES:
        raise HTTPException(400, f"kind must be one of {sorted(_THREAD_PLANES)}")
    _require_thread_visible(root[0], root[1], auth)
    _require_thread_visible(req.kind, req.id, auth)
    _place_in_thread(req.kind, req.id, thread_id)
    _bind_conversation(root[0], root[1], auth.identity(), _session_of(x_session_id))
    db.audit(auth.identity(), "thread.member.add", str(thread_id), {"kind": req.kind, "id": req.id})
    return _thread_public(thread_id, auth.identity())


_SURFACE_KINDS = ("interactive", "stream", "tmux", "oneshot")


@app.post("/api/v1/wake/surfaces")
def register_surface(req: SurfaceRegister, auth: AuthContext = Depends(get_auth)):
    """Declare a conversation that can receive (rail 3.10).

    The rail creates headless holders, so the plane knows them by construction.
    A human's interactive session is the opposite: the plane did not create it
    and cannot see it, which is why that surface was still polling an
    identity-wide inbox and hand-advancing its own cursor while every agent got
    scoped delivery with receipts.

    Registering costs one call and buys three things: `GET /notifications?
    for_conversation=<id>` (and the same on `/watch`) applies the rail's own
    rule to this session, `/notifications/delivered` can target it, and
    `GET /wake/surfaces` shows a human what is listening as their identity.

    Call it again to refresh `last_seen_at` (observability only — never a gate;
    nothing expires and nothing is withheld from an unrefreshed surface).

    **`ended: true` is how a surface goes away, and it is the only way.** Until
    it arrives the surface holds what it wrote on and the rail births no holder
    of that identity for those threads — a registered surface is a holder with
    a different transport (`docs/Rail Behaviour Map.md`), and a holder is held
    while its process lives. Nothing here infers whether anyone is listening:
    there was a `connected` flag that did, and on 2026-09-20 it closed a live
    person's bindings twice and let twins be born on the threads they were
    using (0.12.13). A message to a surface that has stopped reading sits
    unread, which is what an unread message is for.

    An interactive surface is NOT rail-resumable: once ended its threads are
    unheld, and the next message on one births a holder or waits in the inbox
    like any other."""
    conv = req.conversation_id.strip()[:64]
    if not conv:
        raise HTTPException(400, "conversation_id is required")
    if req.surface not in _SURFACE_KINDS:
        raise HTTPException(400, f"surface must be one of {list(_SURFACE_KINDS)}")
    me = auth.identity()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO surfaces (identity, conversation_id, surface, host, client, "
        "                      registered_at, last_seen_at, ended_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(identity, conversation_id) DO UPDATE SET "
        "  surface = excluded.surface, "
        "  host = COALESCE(excluded.host, surfaces.host), "
        "  client = COALESCE(excluded.client, surfaces.client), "
        "  last_seen_at = excluded.last_seen_at, "
        "  ended_at = excluded.ended_at",
        (me, conv, req.surface, req.host, req.client, now, now,
         now if req.ended else None))
    # Goodbye closes this conversation's bindings, here, once. It used to be
    # the rail's job: every seat swept its own `conversation` rows and closed
    # any whose conversation it could not find among the registered surfaces —
    # absence of evidence read as evidence of absence, by a client, from a list
    # it might have mis-parsed. On 2026-09-20 22:37:35 that closed a live
    # person's bindings on three tickets ("conversation 060ee6b1 is gone (no
    # registered surface)") and 22:41:26 a headless holder was born on one of
    # them. The surfaces table is the plane's own record; the plane retires
    # them from it, on the one event that means it.
    if req.ended:
        db.execute("UPDATE threads SET state = 'closed', closed_at = ?, updated_at = ? "
                   "WHERE identity = ? AND conversation_id = ? AND holder = 'conversation' "
                   "AND state IN ('open','closing')", (now, now, me, conv))
    row = db.query_one("SELECT * FROM surfaces WHERE identity = ? AND conversation_id = ?",
                       (me, conv))
    held = db.query("SELECT kind, object_id, state FROM threads "
                    "WHERE identity = ? AND conversation_id = ? AND state IN ('open','closing')",
                    (me, conv))
    return {"surface": _surface_row_public(row, me),
            "holding": [f"{h['kind']}-{h['object_id']}" for h in held],
            "hint": ("Read your scope with GET /api/v1/notifications/watch"
                     f"?for_conversation={conv}&undelivered=true, and POST "
                     "/api/v1/notifications/delivered {\"ids\":[...], "
                     f"\"conversation_id\":\"{conv}\"}} for every row you show. "
                     "Stamp X-Session-Id with the same id on every write.")}


def _surface_row_public(row: dict, me: Optional[str]) -> dict:
    """Same shape as a thread row (`_thread_row_public`): `conversation` is
    always the fingerprint, and the owning identity additionally gets
    `conversation_id`, the raw one. Two keys rather than one key meaning two
    things — a reader comparing `conversation` across the two views must not
    have to know which it is looking at."""
    if not row:
        return {}
    out = {k: row[k] for k in ("id", "identity", "surface", "host", "client",
                               "registered_at", "last_seen_at", "ended_at")}
    out["conversation"] = _session_fp(row["conversation_id"])
    if me and row["identity"] == me:
        out["conversation_id"] = row["conversation_id"]
    return out


@app.get("/api/v1/wake/surfaces")
def list_surfaces(all: bool = False, include_ended: bool = False,
                  auth: AuthContext = Depends(get_auth)):
    """What is listening. Default: your own surfaces, with raw conversation
    ids (they are yours). ?all=true (admin) is the fleet view, fingerprints
    only. ?include_ended=true keeps the ones that said goodbye."""
    me = auth.identity()
    where = "" if include_ended else "WHERE ended_at IS NULL"
    if all:
        if not auth.is_admin():
            raise HTTPException(403, "?all=true is admin-only")
        rows = db.query(f"SELECT * FROM surfaces {where} ORDER BY last_seen_at DESC")
    else:
        clause = "WHERE identity = ?" + ("" if include_ended else " AND ended_at IS NULL")
        rows = db.query(f"SELECT * FROM surfaces {clause} ORDER BY last_seen_at DESC", (me,))
    return {"scope": "fleet" if all else "self",
            "surfaces": [_surface_row_public(r, me) for r in rows]}


@app.get("/api/v1/wake/threads")
def list_threads(all: bool = False, thread: Optional[str] = None,
                 state: Optional[str] = None, thread_id: Optional[int] = None,
                 auth: AuthContext = Depends(get_auth)):
    """Thread bindings. Same three scopes as /wake/claims, and the answer
    names which: (default) your own instances — raw conversation ids, since
    they are yours; ?thread=ticket:N|doc:N|task:N every identity's instance
    on ONE thread, visibility-gated, fingerprints only; ?all=true fleet-wide,
    admin only, refused (403) rather than narrowed. ?state=open|closing|closed
    filters any scope."""
    me = auth.identity()
    if state is not None and state not in _THREAD_STATES:
        raise HTTPException(400, f"state must be one of {list(_THREAD_STATES)}")
    st_clause = " AND state = ?" if state else ""
    st_params = (state,) if state else ()
    if thread is not None or thread_id is not None:
        if thread is not None:
            plane, oid = _parse_thread(thread)
            _require_thread_visible(plane, oid, auth)
            wt = _thread_of(plane, oid)
        else:
            wt = thread_id
            root = _thread_root(wt)
            if not root:
                raise HTTPException(404, f"Thread {wt} not found")
            plane, oid = root
            _require_thread_visible(plane, oid, auth)
        # v19: the bindings of the universal thread the object belongs to
        rows = db.query("SELECT * FROM threads WHERE thread_id = ?" + st_clause
                        + " ORDER BY updated_at DESC", (wt,) + st_params)
        return {"scope": f"thread:{plane}:{oid}", "thread": f"{plane}:{oid}", "thread_id": wt,
                "threads": [_thread_row_public(r, None) for r in rows]}
    if all and not auth.is_admin():
        raise HTTPException(
            403, "?all=true (fleet-wide threads) is admin-only and refused "
                 "rather than narrowed (#529). For one thread any seat may "
                 "call GET /api/v1/wake/threads?thread=ticket:<id>|doc:<id>|task:<id>.")
    if all:
        rows = db.query("SELECT * FROM threads WHERE 1 = 1" + st_clause
                        + " ORDER BY updated_at DESC", st_params)
        return {"scope": "fleet", "threads": [_thread_row_public(r, None) for r in rows]}
    rows = db.query("SELECT * FROM threads WHERE identity = ?" + st_clause
                    + " ORDER BY updated_at DESC", (me,) + st_params)
    return {"scope": "self", "threads": [_thread_row_public(r, me) for r in rows]}


@app.patch("/api/v1/wake/threads/{thread_id}")
def patch_thread(thread_id: int, req: ThreadPatch,
                 auth: AuthContext = Depends(get_auth)):
    """Move a binding's state (open -> closing -> closed, or a reopen) or
    re-stamp its pid. Owner or admin."""
    row = db.query_one("SELECT * FROM threads WHERE id = ?", (thread_id,))
    if not row:
        raise HTTPException(404, f"Thread {thread_id} not found")
    me = auth.identity()
    if row["identity"] != me and not auth.is_admin():
        raise HTTPException(403, "Not your thread")
    if req.state is None and req.pid is None:
        raise HTTPException(400, "Provide state and/or pid")
    now = datetime.now(timezone.utc).isoformat()
    if req.state is not None:
        if req.state not in _THREAD_STATES:
            raise HTTPException(400, f"state must be one of {list(_THREAD_STATES)}")
        db.execute("UPDATE threads SET state = ?, closed_at = CASE WHEN ? = 'closed' "
                   "THEN COALESCE(closed_at, ?) ELSE NULL END, updated_at = ? WHERE id = ?",
                   (req.state, req.state, now, now, thread_id))
    if req.pid is not None:
        db.execute("UPDATE threads SET pid = ?, updated_at = ? WHERE id = ?",
                   (req.pid, now, thread_id))
    row = db.query_one("SELECT * FROM threads WHERE id = ?", (thread_id,))
    return {"thread": _thread_row_public(row, me)}


@app.post("/api/v1/wake/claims/refresh")
def refresh_claims(req: ClaimRefresh, auth: AuthContext = Depends(get_auth),
                   x_session_id: Optional[str] = Header(None)):
    """Extend all your UNEXPIRED claims (a dead surface's expired claims stay
    dead — re-engage by writing). Called by the interactive prompt hook.

    reassert_ticket_ids (#227/#229): threads this surface KNOWS it is engaged
    with — creates-or-extends STICKY claims (visibility-checked), so claim
    state converges to the surface actually listening even if a parallel
    session's exit released them. Sent by the subscriber from its claims
    file every cycle."""
    me = auth.identity()
    sid = _session_of(x_session_id)
    ttl = max(1, min(req.ttl_seconds, CLAIM_TTL_MAX))
    _refresh_claims(me, ttl, session_id=sid)
    reasserted, reasserted_docs, reasserted_tasks = [], [], []
    rttl = max(60, min(req.reassert_ttl_seconds, CLAIM_TTL_MAX))
    if req.reassert_ticket_ids:
        for tid in req.reassert_ticket_ids[:50]:
            ticket = db.query_one("SELECT * FROM tickets WHERE id = ?", (tid,))
            if ticket and _can_see_ticket(auth, ticket):
                # A reassert never steals a thread another live session
                # owns (#264) — only owned/legacy/expired rows re-stick.
                if _claim_thread(me, tid, rttl, sticky=True,
                                 session_id=sid)["owned"]:
                    reasserted.append(tid)
    if req.reassert_doc_ids:
        for did in req.reassert_doc_ids[:50]:
            doc = db.query_one("SELECT * FROM docs WHERE id = ?", (did,))
            if doc and _can_see_doc(auth, doc):
                if _claim_thread(me, doc_id=did, ttl=rttl, sticky=True,
                                 session_id=sid)["owned"]:
                    reasserted_docs.append(did)
    if req.reassert_task_ids:
        for taid in req.reassert_task_ids[:50]:
            try:
                _load_task_and_project(taid, auth)
            except HTTPException:
                continue
            if _claim_thread(me, task_id=taid, ttl=rttl, sticky=True,
                             session_id=sid)["owned"]:
                reasserted_tasks.append(taid)
    now = datetime.now(timezone.utc).isoformat()
    rows = db.query("SELECT ticket_id, doc_id, task_id, claimed_until, sticky "
                    "FROM thread_claims WHERE identity = ? AND claimed_until > ?",
                    (me, now))
    return {"status": "refreshed", "reasserted": reasserted,
            "reasserted_docs": reasserted_docs,
            "reasserted_tasks": reasserted_tasks, "active_claims": rows}


@app.post("/api/v1/wake/claims/release")
def release_all_claims(req: Optional[ClaimRelease] = None,
                       auth: AuthContext = Depends(get_auth),
                       x_session_id: Optional[str] = Header(None)):
    """Release your claims — this surface is going idle.

    Default releases NON-STICKY claims only: identity-wide releases fire on
    every wake-session exit and must not clobber a parallel live surface's
    asserted engagement (#229 twin race). A surface going TRULY idle (Stop
    hook, no live subscription) sends {"include_sticky": true}. Sticky claims
    otherwise fall by TTL or targeted DELETE /tickets/{id}/claim.

    Session-scoped (#264): releases only touch YOUR session's rows + legacy
    unstamped rows — never a parallel live session's stamped claims."""
    include_sticky = bool(req and req.include_sticky)
    n = _release_claims(auth.identity(), include_sticky=include_sticky,
                        session_id=_session_of(x_session_id))
    return {"status": "released", "include_sticky": include_sticky, "released": n}


@app.get("/api/v1/")
def api_index():
    """API surface index for agent discovery. `version`, `commit`, `schema`,
    `helpers` and `skill` name the release you are talking to."""
    return {
        "name": "samcloud registry",
        **_release(),
        "release": _release(),
        # Optional modules and whether this instance runs them (SAMCLOUD_MODULES).
        "modules": {"dns-namecheap": _dns_module_on()},
        "endpoints": {
            "status": {"method": "GET", "path": "/api/v1/status", "description": "Full system status"},
            "devices": {
                "list": {"method": "GET", "path": "/api/v1/devices"},
                "get": {"method": "GET", "path": "/api/v1/devices/{device_id}"},
                "guide": {"method": "GET", "path": "/api/v1/devices/{device_id}/guide", "auth": "any", "description": "SSH access instructions, services, config"},
                "update": {"method": "PATCH", "path": "/api/v1/devices/{device_id}", "auth": "scoped"},
                "delete": {"method": "DELETE", "path": "/api/v1/devices/{device_id}", "auth": "scoped"},
                "pin_host_key": {"method": "POST", "path": "/api/v1/devices/{device_id}/host-key", "auth": "admin-or-device", "description": "Pin the host key the gateway checks hops against; only if absent (409 on a different key)"},
                "clear_host_key": {"method": "DELETE", "path": "/api/v1/devices/{device_id}/host-key", "auth": "admin-or-device", "description": "Clear the pin so a legitimate re-key is accepted"},
            },
            "gateway": {
                "host_key": {"method": "GET", "path": "/api/v1/gateway/host-key", "auth": "none", "description": "The gateway's SSH host keys, so a device can verify the tunnel it dials (503 if unpublished)"},
            },
            "services": {
                "list": {"method": "GET", "path": "/api/v1/services"},
                "get": {"method": "GET", "path": "/api/v1/services/{service_id}"},
                "guide": {"method": "GET", "path": "/api/v1/services/{service_id}/guide", "auth": "owner", "description": "Persistent operations guide (markdown)"},
                "update": {"method": "PATCH", "path": "/api/v1/services/{service_id}", "auth": "owner"},
                "health": {"method": "POST", "path": "/api/v1/services/{service_id}/health", "auth": "owner"},
                "create": {"method": "POST", "path": "/api/v1/services", "auth": "scoped", "description": "Register service (admin or device scope)"},
                "delete": {"method": "DELETE", "path": "/api/v1/services/{service_id}", "auth": "scoped", "description": "Delete service (admin or owner)"},
            },
            "enrollment": {
                "create_slot": {"method": "POST", "path": "/api/v1/services/enroll", "auth": "admin"},
                "briefing": {"method": "GET", "path": "/api/v1/enroll/services/brief/{token}"},
                "activate": {"method": "POST", "path": "/api/v1/services/activate"},
                "pending": {"method": "GET", "path": "/api/v1/enroll/services/pending"},
            },
            "tickets": {
                "list": {"method": "GET", "path": "/api/v1/tickets", "description": "Filter by ?status/?service_id/?device_id/?assignee/?source/?group_scope (exact: the ticket's own group)/?tag. Paged: ?limit (default 100, max 500) + ?offset over visible tickets; X-Total-Count header = total matches"},
                "get": {"method": "GET", "path": "/api/v1/tickets/{id}"},
                "create": {"method": "POST", "path": "/api/v1/tickets", "auth": "any", "description": "File a ticket — auto-subscribes you to replies. group_scope names its group (default: your first editable; with an assignee, the first you edit that they are in); an outsider assignee is 422 with a note; outsider mentions are noted in `notes`"},
                "update": {"method": "PATCH", "path": "/api/v1/tickets/{id}", "auth": "any"},
                "comment": {"method": "POST", "path": "/api/v1/tickets/{id}/comments", "auth": "any", "description": "Reply without subscribing the writer; subscribed reports existing watch state. Named recipients and existing watchers receive it"},
                "subscribe": {"method": "POST", "path": "/api/v1/tickets/{id}/subscribe", "auth": "any", "description": "Explicitly opt into comments and record changes (updates=1 on the watcher row)"},
                "unsubscribe": {"method": "DELETE", "path": "/api/v1/tickets/{id}/subscribe", "auth": "any", "description": "Stop watching (opt out)"},
                "watchers": {"method": "GET", "path": "/api/v1/tickets/{id}/watchers", "auth": "any"},
            },
            "notifications": {
"list": {"method": "GET", "path": "/api/v1/notifications", "auth": "any", "description": "Record introductions carry read_object; pending_inbox/pending_inbox_text describe other undelivered identity-wide events, not a park diagnosis; sibling bindings are attached as siblings/siblings_text (registered state, not liveness). ?delivery=true excludes legacy doc.published before pagination; the default inbox retains them. Comment payloads carry actor_session and actor_home_thread_id; same-identity sibling rows also pin recipient_session. Your durable inbox (?unread=true default; ?ticket_id=N to filter one thread, pair with ?unread=false for audit queries; ?undelivered=true = rows the rail has never delivered — rail 3.9's predicate, doc 174; ?for_conversation=<id> = only what is DELIVERED to that conversation — the threads it is on (0.12.69; a mention or assignment on a thread none of your conversations is on is a cold wake for the rail, never pushed into a live session)"},
                "ack": {"method": "POST", "path": "/api/v1/notifications/ack", "auth": "any", "description": "Mark HANDLED: {ids:[...]} or {all:true}, plus surface:'wake|watch|subscribe|chat' for attribution"},
                "delivered": {"method": "POST", "path": "/api/v1/notifications/delivered", "auth": "any", "description": "Delivery receipt: {ids, conversation_id} — the rail wrote these rows into a conversation. Final: ?undelivered=true never offers the row again"},
                "watch": {"method": "GET", "path": "/api/v1/notifications/watch", "auth": "any", "description": "Long-poll (?timeout=60, max 300) — blocks until something lands; the agent wake-up primitive. ?thread= one lane; ?lane_scope=own; ?for_conversation=<id> + ?undelivered=true = the registered-surface form: the threads that conversation is on — clause (a) of the rail's rule alone; what opens a thread toward the identity is the rail's cold wake, never in this feed — and rows you receipt are never re-offered"},
                "subscriptions": {"method": "GET", "path": "/api/v1/subscriptions", "auth": "self", "description": "Your watch state, readable: tickets/docs/projects you subscribe to + standing mutes. Unsubscribe now mutes — your own later comments don't re-enroll you; assignment/@mention/explicit re-subscribe clears it"},
            },
            "wake": {
                "register": {"method": "POST", "path": "/api/v1/wake/register", "auth": "any", "description": "Register your wake rail: {wake_cmd, host, listener_version, handler_version, api_env, notif_hook_version, stop_hook_version}. Response carries served versions + update_available. A row is keyed per (identity, HOST) — `UNIQUE(user_id, host)` — so one identity can hold several and a row count is not a seat count; a container with no stable --hostname mints a new row per run. {ended:true} RETIRES a row: it leaves GET /wake/registrations (?include_retired=true shows it) and the dark-watch, and is never deleted. Admin may retire another identity's with {ended:true, identity, host} — the leg that matters, since a row is stale because the seat is GONE and a gone seat cannot call this. A real register CLEARS ended_at, so a seat that comes back is visible and darkenable again"},
                "registrations": {"method": "GET", "path": "/api/v1/wake/registrations", "auth": "any", "description": "Fleet view (admin: all; else: own) — rail + hook versions, staleness, surface_dark, last connect, and CAPABILITY: session_attempts/session_successes/consecutive_session_failures/last_session_ok_at, plus session_truncations (killed at the session cap — a budget signal, never counted as incapacity). ROWS ARE PER (identity, host), NOT per identity, so len(rows) is not a seat count. ?include_retired=true adds rows retired via POST /wake/register {ended:true}"},
                "wake_agent": {"method": "POST", "path": "/api/v1/agents/{username}/wake", "auth": "admin", "description": "Wake one agent directly: {thread_id, summary, body?, event_type?}. **thread_id is required** — work arrives wrapped in a thread and whoever you wake has to have somewhere to answer; the row carries the thread and its root. event_type must be one the rail classifies, so a wake cannot arrive as a word nothing can judge"},
                "session": {"method": "POST", "path": "/api/v1/wake/session", "auth": "any", "description": "Report one wake attempt + outcome: {outcome: ok|failed, rc, cause, error, host, unread}. The handler posts this after EVERY session — success included, since a failure-only feed cannot tell a broken seat from a quiet one. `cause` is set at the launch site and decides the counter: auth/launcher LATCH, ok/quota CLEAR (being rate-limited proves the launcher reached the API), watchdog counts on `truncations`, anything else is inert. N consecutive latching failures marks the seat dark (reason=failing-launches) while still connected"},
                "threads": {"method": "GET", "path": "/api/v1/wake/threads", "auth": "any", "description": "Thread bindings: which conversation of an identity is on which thread, open|closing|closed; each row carries thread_id and home_thread_id (stable birth/first-create origin, null if unknown). Default = yours (raw conversation ids); ?thread=ticket:<id>|doc:<id>|task:<id> or ?thread_id=<id> = every identity's instance on that thread, fingerprints; ?all=true admin only; ?state= filters"},
                "work_threads": {"method": "GET", "path": "/api/v1/threads", "auth": "any", "description": "The universal threads you are bound to: members (tickets, tasks, docs), participants, bindings, open_members. ?state=open|closed"},
                "work_thread": {"method": "GET", "path": "/api/v1/threads/{id}", "auth": "any", "description": "One universal thread. open_members = any ticket or task in it still open (docs never count) — an instance lives while that is true or until a person closes the thread. A thread has no scope: it decides wake and routing, its objects carry the scope — a member you cannot read is a bare ref {id, hidden: true}, counted in hidden_members; a root you cannot read is root_hidden and the title is `thread N`"},
                "work_thread_of": {"method": "GET", "path": "/api/v1/threads/of/{kind}/{id}", "auth": "any", "description": "The thread an object belongs to (ticket|doc|task)"},
                "work_thread_patch": {"method": "PATCH", "path": "/api/v1/threads/{id}", "auth": "any", "description": "{state: open|closed, title}. Closing writes thread.closed to every bound instance; each wraps up and ends. A person or admin closes; an agent does not"},
                "work_thread_member": {"method": "POST", "path": "/api/v1/threads/{id}/members", "auth": "any", "description": "{kind, id} — move an object into this thread. Every create (ticket, task, doc) joins a thread by itself: X-Thread-Id, else a subtask's parent's, else the holder's, else the conversation's own (X-Session-Id), else the identity's own. Only a conversation's first create starts one"},
                "thread_upsert": {"method": "POST", "path": "/api/v1/wake/threads", "auth": "any", "description": "Record/move your binding for one thread: {kind, object_id, conversation_id, host?, holder: stream|tmux|oneshot|adapter|conversation, pid?, state, rail_version?}. One row per (kind, object, identity); the rail posts at birth/resume/close, BEFORE it births. Any write carrying X-Session-Id binds the writer when no open instance exists. **409** = a live registered surface of this identity already holds that thread: do not birth a second holder"},
                "thread_patch": {"method": "PATCH", "path": "/api/v1/wake/threads/{id}", "auth": "self", "description": "Move a binding's state (closed stamps closed_at; a reopen clears it) or re-stamp pid. Owner or admin"},
                "surface_register": {"method": "POST", "path": "/api/v1/wake/surfaces", "auth": "any", "description": "Declare a conversation that can RECEIVE: {conversation_id, surface: interactive|stream|tmux|oneshot, host?, client?, ended?}. The rail creates headless holders so it knows them; a human's interactive session has to say it exists. Registering enables ?for_conversation=<id> on the inbox and the watch, and lets /notifications/delivered target it. Re-post to refresh `last_seen_at` (observability only, never a gate). {ended:true} is the only thing that retires a surface, and the plane closes that conversation's bindings when it arrives. A process exiting is NOT that — a background reader replaced on the same conversation used to announce the person had left, and a twin was born in the gap, so the served subscriber no longer sends it automatically (2.5.0)"},
                "surfaces": {"method": "GET", "path": "/api/v1/wake/surfaces", "auth": "any", "description": "What is listening as you (raw conversation ids); ?all=true fleet-wide, admin only, fingerprints; ?include_ended=true keeps the ones that said goodbye. A registered surface HOLDS what it wrote on and the rail births no holder of that identity for those threads; it holds until it says {ended:true}, exactly as a holder holds until its process exits. There is no liveness flag: a surface is not asked to prove it is listening, and no seat may retire another conversation by failing to find it"},
                "health": {"method": "GET", "path": "/api/v1/wake/health", "auth": "any", "description": "Fleet darkness as a READING, not an inbox message: per-seat dark/reason/since, the everyone-at-once fleet signature, and ?transitions=N of history from the audit log. wake.dark/wake.dark_recovered/wake.dark.fleet are no longer written to anyone's inbox"},
            },
            "docs": {
                "list": {"method": "GET", "path": "/api/v1/docs", "auth": "any", "description": "Versioned shared-knowledge plane — sibling to tickets (?kind ?status ?group_scope ?linked_ticket ?q)"},
                "get": {"method": "GET", "path": "/api/v1/docs/{slug|id}", "auth": "any", "description": "Doc + head body + links (?rev=N for a past revision)"},
                "create": {"method": "POST", "path": "/api/v1/docs", "auth": "any", "description": "Open revision 1; source is bearer-derived and immutable. owner defaults to source; a named owner must be in group_scope (default: your first editable). No group-wide publication notification. Returns thread_id"},
                "revise": {"method": "POST", "path": "/api/v1/docs/{id}/revisions", "auth": "any", "description": "Edit = a new immutable revision (full body)"},
                "append": {"method": "POST", "path": "/api/v1/docs/{id}/append", "auth": "any", "description": "Additive edit: append a block without resubmitting the body (saves tokens) — still a revision"},
                "history": {"method": "GET", "path": "/api/v1/docs/{id}/revisions", "auth": "any", "description": "Full revision history (metadata)"},
                "comment": {"method": "POST", "path": "/api/v1/docs/{id}/comments", "auth": "any", "description": "Discuss a doc without subscribing the writer (distinct from the versioned body); subscribed reports existing watch state"},
                "update": {"method": "PATCH", "path": "/api/v1/docs/{id}", "auth": "owner", "description": "Lifecycle metadata (status/kind/group_scope/supersedes/parent_doc_id/owner); ownership transfer does not change source"},
                "delete": {"method": "DELETE", "path": "/api/v1/docs/{id}", "auth": "owner", "description": "Permanent — prefer PATCH status=archived"},
                "link": {"method": "POST", "path": "/api/v1/docs/{id}/links", "auth": "any", "description": "Thread to a ticket/service/device/doc"},
                "unlink": {"method": "DELETE", "path": "/api/v1/docs/{id}/links/{link_id}", "auth": "owner"},
                "subscribe": {"method": "POST", "path": "/api/v1/docs/{id}/subscribe", "auth": "any", "description": "Explicitly opt into comments and record changes (updates=1); participation alone receives comments"},
                "unsubscribe": {"method": "DELETE", "path": "/api/v1/docs/{id}/subscribe", "auth": "any"},
            },
            "tasks": {
                "projects": {"method": "GET", "path": "/api/v1/projects", "auth": "any", "description": "Task plane — projects with computed progress rollups (?status ?group_scope). doc=record, ticket=request, task=actionable item"},
                "project": {"method": "GET", "path": "/api/v1/projects/{slug|id}", "auth": "any", "description": "Project + rollup + full nested task tree"},
                "create_project": {"method": "POST", "path": "/api/v1/projects", "auth": "any", "description": "Create a project — owner is bearer-derived; group_scope for access"},
                "update_project": {"method": "PATCH", "path": "/api/v1/projects/{slug|id}", "auth": "owner"},
                "delete_project": {"method": "DELETE", "path": "/api/v1/projects/{slug|id}", "auth": "owner", "description": "Permanent — prefer PATCH status=archived"},
                "create": {"method": "POST", "path": "/api/v1/projects/{slug|id}/tasks", "auth": "any", "description": "Add a task (parent_task_id for a subtask — it joins the parent's thread). Answers with the thread_id it joined or started"},
                "list": {"method": "GET", "path": "/api/v1/tasks", "auth": "any", "description": "Flat list across visible projects (?project ?status ?assignee ?parent)"},
                "get": {"method": "GET", "path": "/api/v1/tasks/{id}", "auth": "any"},
                "update": {"method": "PATCH", "path": "/api/v1/tasks/{id}", "auth": "any", "description": "Edit — open to project scope; status=done stamps completed_at; a new parent_task_id moves the task into the parent's thread (notes says when it stays)"},
                "delete": {"method": "DELETE", "path": "/api/v1/tasks/{id}", "auth": "creator/owner"},
                "delegate": {"method": "POST", "path": "/api/v1/tasks/{id}/delegate", "auth": "any", "description": "Hand to an agent: files a linked ticket, task becomes your blocked waiting-handle. Explicit, never automatic"},
                "subscribe": {"method": "POST", "path": "/api/v1/projects/{slug|id}/subscribe", "auth": "any", "description": "Opt into a project's task events: task.created/updated/delegated/commented land in your inbox. DELETE to stop. Quiet-by-default preserved for everyone else"},
                "comment": {"method": "POST", "path": "/api/v1/tasks/{id}/comments", "auth": "any", "description": "Comment on a task without subscribing. Stores X-Session-Id fingerprint and home_thread_id; notifies assignee, creator, named recipients and project watchers, excluding writer conversation, with targeted existing sibling delivery"},
            },
            "wake_rail": {
                "rail": {"method": "GET", "path": "/api/v1/helpers/rail", "auth": "any", "description": "The rail: the process that starts you on a thread and hands you its messages (served, self-updating)"},
                "listen": {"method": "GET", "path": "/api/v1/helpers/listen", "auth": "any", "description": "The listener: runs the rail on events and keeps the served files current"},
                "adapter": {"method": "GET", "path": "/api/v1/helpers/adapter", "auth": "any", "description": "The rail's holder for a runtime that is not a Claude harness: one delivery, one turn. The rail invokes it; it runs the seat's runtime on that thread's conversation and posts what the runtime said on the thread, stamped with the bound conversation. Configured by RAIL_RUNTIME* in wake.env"},
                "install": {"method": "GET", "path": "/api/v1/helpers/wake-install", "auth": "any", "description": "Install the listener and the rail on this seat: curl -s -H @\"$HOME/.samcloud/token.hdr\" <API>/helpers/wake-install | bash"},
                "say": {"method": "GET", "path": "/api/v1/helpers/say", "auth": "any", "description": "sc-say, the write tool: body on stdin, posts under your identity on the thread you name"},
                "pr": {"method": "GET", "path": "/api/v1/helpers/pr", "auth": "any", "description": "sc-pr, the contribute tool: `sc-pr setup` once, then `sc-pr open --title … --ticket N` pushes your branch and opens a pull request on samcloud. The GitHub token is minted per call from the gh-broker and never touches argv or disk. Guide: skills/samcloud-agent/contributing.md"},
                "new": {"method": "GET", "path": "/api/v1/helpers/new", "auth": "any", "description": "sc-new, the create tool: a ticket, task or doc made INTO a thread. Sends X-Session-Id (your conversation: replies come back, and the create joins the conversation's thread) and X-Thread-Id when you name one; a bare curl sends neither and lands in the identity's own thread"},
                "edit": {"method": "GET", "path": "/api/v1/helpers/edit", "auth": "any", "description": "sc-edit, the revise tool: change a doc BODY. sc-say is comment-only and sc-new is create-only, so this is the only pen that can revise. Sends base_rev read at call time (a concurrent writer 409s instead of being overwritten), re-reads the doc after the write to verify the head is what you meant, and refuses an empty or unchanged body. Prefer --append for an additive edit: it reads the head server-side and cannot lose a revision"},
                "subscribe": {"method": "GET", "path": "/api/v1/helpers/subscribe", "auth": "any", "description": "For a terminal a person types into: prints what arrives for that session, as the rail would (run: curl … | python3 -)"},
                "channel": {"method": "GET", "path": "/api/v1/helpers/channel", "auth": "any", "description": "The subscribe feed as a Claude Code channel (MCP server): each delivery is pushed into the interactive session, idle or not, with nothing to re-arm. Register it with `claude mcp add`, launch with --dangerously-load-development-channels server:samcloud (research preview)"},
                "wsl_boot": {"method": "GET", "path": "/api/v1/helpers/wsl-boot", "auth": "any", "description": "Starts the Linux side of a Windows machine (WSL) at boot"},
                "scope_check": {"method": "GET", "path": "/api/v1/helpers/scope-check", "auth": "any", "description": "Confirm your scopes — expected against actual, no side effects (run: curl -s -H @\"$HOME/.samcloud/token.hdr\" <API>/helpers/scope-check | python3 -)"},
            },
            "routes": {
                "list": {"method": "GET", "path": "/api/v1/routes"},
                "activate": {"method": "POST", "path": "/api/v1/routes/{subdomain}/activate", "auth": "service manager", "description": "A route with no service is the admin's"},
            },
            "users": {
                "invite": {"method": "POST", "path": "/api/v1/users/invite", "auth": "admin or editor", "description": "Create user + invite token. An editor invites a user or agent into groups they edit, at a role up to their own"},
                "briefing": {"method": "GET", "path": "/api/v1/users/brief/{token}", "description": "Invitation briefing (markdown)"},
                "activate": {"method": "POST", "path": "/api/v1/users/activate", "description": "Consume invite, get permanent token"},
                "guide": {"method": "GET", "path": "/api/v1/users/me/guide", "auth": "self", "description": "Persistent operations guide"},
                "briefing": {"method": "GET", "path": "/api/v1/users/me/briefing", "auth": "self", "description": "Personal briefing: unread inbox, blocked-on-you, decision-tagged tasks, pick-me menu, windowed movement"},
                "search": {"method": "GET", "path": "/api/v1/search", "auth": "any", "description": "Search across planes (#1082): tickets, tasks and docs you can see whose title, body or comments hold every word of ?q (#N matches an id). One filter vocabulary for all: ?kind=ticket,task,doc ?status (comma list; active = open/triaged/in-progress tickets, todo/in-progress/blocked tasks, draft/active docs) ?assignee (:me; a doc's owner) ?group_scope ?tag ?updated_hours ?limit (30, max 100) ?offset. Ranked id > title > body > comment, newest first"},
                "inbox_feed": {"method": "GET", "path": "/api/v1/inbox/feed", "auth": "self", "description": "The inbox as a feed (#1082): one card per ticket, task or doc, newest activity first, with your read/star state. ?filter=mine (default: addressed to you, assigned or yours) | unread | starred | all; ?kind=ticket,task,doc; ?window_hours (default 168); ?group_scope; ?limit/?offset by card. Reads delivery as it is; changes no one's notifications"},
                "inbox_marks": {"method": "POST", "path": "/api/v1/inbox/marks", "auth": "self", "description": "{items:[{kind,id}], read?, starred?}: mark feed cards read/unread and starred/unstarred. Reading a card also marks your notification rows for it read"},
                "inbox_read_all": {"method": "POST", "path": "/api/v1/inbox/read-all", "auth": "self", "description": "Mark the whole feed read: the epoch moves to now, stars stay, your unread notification rows are marked read"},
                "views": {"method": "GET", "path": "/api/v1/views", "auth": "self", "description": "Saved views: per-user filter definitions; first call seeds four editable defaults. POST to create; PATCH/DELETE /views/{id}; GET /views/{id}/results executes with grouping"},
                "list": {"method": "GET", "path": "/api/v1/users", "auth": "any", "description": "Admin: everyone; anyone else: self. A group's roster is GET /groups/{id}"},
                "read": {"method": "GET", "path": "/api/v1/users/{id}", "auth": "any", "description": "Self or admin: the row; a fellow member of any of your groups: name, role, shared groups; otherwise 404"},
            },
            "groups": {
                "list": {"method": "GET", "path": "/api/v1/groups", "auth": "any", "description": "The groups you belong to (admin: all). The group is the scope"},
                "read": {"method": "GET", "path": "/api/v1/groups/{id}", "auth": "member", "description": "Purpose, working doc, members with roles, devices, services, your_role; 404 if you are not in it"},
                "create": {"method": "POST", "path": "/api/v1/groups", "auth": "any", "description": "{id, name, description?, doc_id?} — any user; you become its owner"},
                "update": {"method": "PATCH", "path": "/api/v1/groups/{id}", "auth": "owner", "description": "name, description, route_path, doc_id (the working doc; 0 clears)"},
                "members": {"method": "POST", "path": "/api/v1/groups/{id}/members", "auth": "owner", "description": "{user_id, role: viewer|editor|owner}; PATCH/DELETE /groups/{id}/members/{uid}"},
                "devices": {"method": "POST", "path": "/api/v1/groups/{id}/devices", "auth": "editor", "description": "{device_id} — a device you manage joins the group; DELETE by an owner"},
                "services": {"method": "POST", "path": "/api/v1/groups/{id}/services", "auth": "editor", "description": "{service_id} — a service you can change joins the group; DELETE by an owner"},
            },
            "resources": {
                "list": {"method": "GET", "path": "/api/v1/resources", "description": "List resources (filter by device, type, provider, available, min_memory_mb)"},
                "dashboard": {"method": "GET", "path": "/api/v1/resources/dashboard", "description": "Normalised at-a-glance (health colours, utilisation bars)"},
                "get": {"method": "GET", "path": "/api/v1/resources/{id}", "description": "Full detail + active and queued leases, the queue in grant order, expiry_policy, and unsettled: expired holders a hold resource still charges until their holder or an admin settles them"},
                "register": {"method": "POST", "path": "/api/v1/resources", "auth": "scoped"},
                "stats": {"method": "POST", "path": "/api/v1/resources/{id}/stats", "auth": "scoped", "description": "Push live utilisation (15s recommended). Needs to MANAGE the resource's device: 404 when the device is outside your scope, 403 when you can read it but not manage it"},
                "update": {"method": "PATCH", "path": "/api/v1/resources/{id}", "auth": "scoped", "description": "Needs to MANAGE the resource's device: 404 when the device is outside your scope, 403 when you can read it but not manage it expiry_policy: free (default: an expired holder stops charging at once) or hold (it charges until its holder or an admin settles it with DELETE /leases/{id}; task 868)."},
                "delete": {"method": "DELETE", "path": "/api/v1/resources/{id}", "auth": "scoped"},
            },
            "leases": {
                "request": {"method": "POST", "path": "/api/v1/resources/{id}/leases", "auth": "scoped", "description": "The resource's device must be in your scope (device:<id>, or a group that holds it). Outside scope is 404, not 403: the resource does not exist from there. A service token (service:self, service:heartbeat) sees no device, so it cannot lease. Request lease — 200 granted, 200 queued (status: queued, queue_position, wait_until), 409 refused. wait_s joins the queue instead of taking a 409 (held, exclusive-over-active, queue_ahead) and is how long you will wait; a shared request over the bytes queues without it, bounded by the registry default (3600 s). wait: true with no wait_s joins with no deadline (wait_until null): the row leaves the queue by promotion or by its own DELETE, never by the clock, and renew on it changes nothing. The queue is promoted in order (priority, then arrival) inside the transaction that frees the bytes; strict head of line, so a newcomer that would fit does not pass a waiter that does not (409 queue_ahead without wait_s). A request larger than the resource is 409 exceeds_capacity, never queued. memory_mb is optional/null or a strict non-negative integer; true, string numbers (such as \"100\"), fractions (such as 1.5) and negative values are 422. An unregistered service_id is 422 (omit it to lease as yourself). Other 409 detail.code values: invalid_existing_allocation (malformed active memory claim), invalid_resource_capacity (malformed declared capacity). A resource whose allocation_mode is exclusive grants an exclusive lease whether or not you ask for one, and at most one at a time"},
                "release": {"method": "DELETE", "path": "/api/v1/leases/{id}", "auth": "any", "description": "Holder or admin. Releasing commits together with the promotion of the waiters that now fit; the response names them (promoted) and each promotion publishes lease.granted with promoted:true. A queued request released by its requester leaves the queue as status cancelled On a hold resource the same call on an expired holder is the settlement (status: settled, event lease.settled) and the queue moves then. The release reaps first, inside its transaction: a waiter past its deadline is never resurrected by it."},
                "get": {"method": "GET", "path": "/api/v1/leases/{id}", "auth": "scoped", "description": "One lease with its live queue_position while queued; hidden (404) outside the scope of its resource's device. status is one of active, queued, expired (expiry_reason ttl_expired: the holder stopped renewing, not proof its processes exited; wait_expired: a waiter ran out of wait), released (a holder gave it back), cancelled (a waiter left). Re-read after a lost reply: the row is the truth, and a repeated DELETE is idempotent"},
                "wait": {"method": "GET", "path": "/api/v1/leases/{id}/wait?timeout_s=30", "auth": "scoped", "description": "Long-poll a queued lease: answers when it leaves the queue (active, expired, released) or after timeout_s (max 120) with the row as it is then and waiting:true. One request worker per waiter — for a few shell clients per host, not a fleet of pollers. The served helper `sc-lease acquire` wraps request + wait"},
                "renew": {"method": "POST", "path": "/api/v1/leases/{id}/renew", "auth": "any", "description": "Extend in place — body {ttl_seconds} required; holder or admin; never shortens; 404 once expired; on a queued lease it extends the wait deadline (reason: waiting) and grants nothing; capped at the lease's max_total_s from granted_at (set at grant; default max(1800, ttl), hard limit 21600); response names its reason (extended/at_ceiling/ttl_below_granted/indefinite)"},
                "rollover": {"method": "POST", "path": "/api/v1/leases/{id}/rollover", "auth": "holder/admin and resource manager", "description": "Explicit atomic replacement of a finite shared positive-memory allocation; same holder/resource/bytes, body {key,ttl_seconds}; persisted key required, conflicting replay is 409. TTL cannot exceed the predecessor exposure bound. Exclusive/job renewal ceilings unchanged; replay returns replacement's current state, never resurrects it"},
                "list": {"method": "GET", "path": "/api/v1/leases", "description": "List leases (filter by resource, service, status)"},
            },
            "lease_groups": {
                "create": {"method": "POST", "path": "/api/v1/lease-groups", "auth": "all member resources visible", "description": "Stable request_key and ordered members: resource_id, explicit ttl_seconds, optional memory_mb/service_id/max_total_s. Exclusive normal-priority ordinary host queues, HOLD required, no group deadline. Only one acquiring/occupying group at a time; pending groups FIFO without leases."},
                "get": {"method": "GET", "path": "/api/v1/lease-groups/{id}", "auth": "owner/admin or all members visible", "description": "Read partial ownership, generation and exact cleanup/settlement receipts. Never acquire on read."},
                "events": {"method": "GET", "path": "/api/v1/lease-groups/{id}/events", "auth": "same as group read", "description": "Durable event history: after sequence, limit 1-500 (default 100). Reconcile after SSE reconnect."},
                "advance": {"method": "POST", "path": "/api/v1/lease-groups/{id}/advance", "auth": "owner/admin and all members visible", "description": "Body {generation}: continue acquisition after first member admitted; no queue priority override."},
                "serving": {"method": "POST", "path": "/api/v1/lease-groups/{id}/serving", "auth": "owner/admin and all members visible", "description": "Body {generation,evidence_ref,evidence_sha256}: provider attestation, accepted only with every member active; does not start workers."},
                "release": {"method": "POST", "path": "/api/v1/lease-groups/{id}/release", "auth": "owner/admin", "description": "Body {generation}: cancel queued members, retain active members for cleanup. Expiry alone cannot settle HOLD."},
                "release_member": {"method": "POST", "path": "/api/v1/lease-groups/{id}/members/{lease_id}/release", "auth": "owner/admin", "description": "Body {generation,evidence_ref,evidence_sha256}: attributed cleanup plus exact member release/settlement receipt, one transaction."},
                "fence": {"method": "POST", "path": "/api/v1/lease-groups/{id}/fence", "auth": "owner/admin", "description": "Body {generation,reason}: advance coordinator generation and begin draining. Never infer process death or retry work."},
            },
            "events": {
                "stream": {"method": "GET", "path": "/api/v1/events", "auth": "any", "description": "SSE stream — real-time notifications"},
                "recent": {"method": "GET", "path": "/api/v1/events/recent", "auth": "any", "description": "Recent events (polling fallback)"},
                "comments": {"method": "GET", "path": "/api/v1/comments", "auth": "any", "description": "Comment feed across tickets, docs and tasks: ?author= ?since=YYYY-MM-DD HH:MM:SS ?plane=ticket|doc|task. Feed and object-detail comments include session_fp and home_thread_id (writer origin, not object thread; null if unknown). Pass watermark as since until more=false"},
            },
            "access_keys": {
                "create": {"method": "POST", "path": "/api/v1/access-keys", "description": "Generate SSH keypair for gateway access. Returns private key ONCE."},
                "setup_script": {"method": "POST", "path": "/api/v1/access-keys/setup-script", "description": "Generate key + bash install script"},
                "pair": {"method": "POST", "path": "/api/v1/access-keys/pair", "description": "Single-use pair link (1 h) for a device with no samcloud token: run its pair_command on that device, or open its code_url there (the console /pair page shows it as a QR) and run the command it shows"},
                "pair_redeem": {"method": "GET", "path": "/api/v1/pair/{token}", "auth": "none", "description": "Spend a pair link (its sc_pair_ token or short code): mints the key and serves its setup script"},
                "list": {"method": "GET", "path": "/api/v1/access-keys"},
                "revoke": {"method": "DELETE", "path": "/api/v1/access-keys/{key_id}"},
            },
            "auth": {
                "verify": {"method": "GET", "path": "/api/v1/auth/verify", "auth": "any", "description": "Verify token, return identity + scopes. ?scope= for scope check."},
                "login": {"method": "POST", "path": "/api/v1/auth/login"},
                "init": {"method": "POST", "path": "/api/v1/init"},
                "setup_status": {"method": "GET", "path": "/api/v1/setup/status", "auth": "none", "description": "Is the plane initialised (any user exists). The only unauthenticated fact about users."},
                "scope_unscoped": {"method": "GET", "path": "/api/v1/admin/scope/unscoped", "auth": "admin", "description": "What still has no group (scope v1 condition C2) — empty after the v20 backfill."},
                "can_access": {"method": "GET", "path": "/api/v1/auth/can-access", "auth": "admin", "description": "?username=&device_id= — can this user enter this device. The gateway's admission oracle; one predicate with the API (scopes, admin role, group membership)."},
                "accessible_devices": {"method": "GET", "path": "/api/v1/auth/accessible-devices", "auth": "admin", "description": "?username= — the devices this user can enter, by the same predicate; what the gateway lists."},
            },
        },
    }


@app.get("/api/v1/status")
def system_status(auth: Optional[AuthContext] = Depends(get_auth_optional)):
    """Full system status for dashboard consumption.

    Scope-enforced, fail closed (mirrors /devices, /services, /tickets):
    - admin (`visible is None`) sees the whole mesh;
    - a group/device-scoped caller sees only their visible devices + the
      services/ports/routes/tickets derived from them;
    - an unauthenticated or unscoped caller sees an empty fleet — never the
      whole mesh. The dashboard attaches the caller's token via the console
      proxy; a missing token degrades to "nothing", not "everything".
    """
    devices = db.query("SELECT * FROM devices ORDER BY context, id")
    services = db.query("SELECT * FROM services ORDER BY device_id, name")
    ports = db.query("SELECT * FROM ports ORDER BY type, port")
    routes = db.query("SELECT * FROM routes ORDER BY domain, subdomain")
    open_tickets = db.query("SELECT * FROM tickets WHERE status NOT IN ('resolved', 'closed') ORDER BY priority DESC, created_at")

    # `_visible_device_ids` returns None for admin (see all). For a non-admin it
    # returns the in-scope device set; for an unauthenticated caller we collapse
    # to the empty set so the filters below strip everything.
    visible = _visible_device_ids(auth) if auth is not None else set()
    if visible is not None:
        devices = [d for d in devices if d["id"] in visible]
        services = [s for s in services if s["device_id"] in visible]
        visible_service_ids = {s["id"] for s in services}
        ports = [p for p in ports
                 if p.get("owner_id") in visible or p.get("owner_id") in visible_service_ids]
        routes = [r for r in routes if r.get("service_id") in visible_service_ids]
        open_tickets = [t for t in open_tickets
                        if auth is not None and _can_see_ticket(auth, t)]

    return {
        "devices": devices,
        "services": services,
        "ports": ports,
        "routes": routes,
        "open_tickets": open_tickets,
        "counts": {
            "devices": len(devices),
            "services": len(services),
            "ports": len(ports),
            "routes": len(routes),
            "open_tickets": len(open_tickets),
        },
    }


def _can_see_audit(auth: AuthContext, entry: dict,
                   visible_devices: set, visible_services: set) -> bool:
    """Whether a scoped (non-admin) caller may see an audit entry.

    Visible if the caller performed the action, or it targets a device/service
    in their scope. Everything else is hidden (fail closed) — the dashboard
    activity feed must not surface actions on resources the caller can't see.
    """
    ident = auth.identity()
    actor = entry.get("actor") or ""
    # Own actions (actor stored variously as 'user:alice', a username, 'agent:x',
    # or a service path) — match the caller's identity or raw owner id.
    if actor == ident or actor == auth.owner_id or actor.endswith(f":{ident}"):
        return True
    target = entry.get("target") or ""
    if not target:
        return False
    device_part = target.split("/", 1)[0]
    return (target in visible_devices
            or device_part in visible_devices
            or target in visible_services)


@app.get("/api/v1/audit")
def audit_log(limit: int = 50, action: str = None,
              auth: Optional[AuthContext] = Depends(get_auth_optional)):
    """Recent audit entries, scope-enforced and fail closed.

    Admins see all activity. A scoped caller sees only entries they performed or
    that target a device/service in their scope. An unauthenticated caller sees
    nothing. Non-admins over-fetch then filter so the feed still fills to `limit`.
    """
    _check_limit(limit)
    admin = auth is not None and auth.is_admin()
    fetch_n = limit if admin else min(max(limit * 5, limit), 500)
    if action:
        rows = db.query(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY created_at DESC LIMIT ?",
            (action, fetch_n),
        )
    else:
        rows = db.query("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (fetch_n,))

    if admin:
        return rows
    if auth is None:
        return []
    visible_devices = _visible_device_ids(auth) or set()
    visible_services = {s["id"] for s in db.query("SELECT id, device_id FROM services")
                        if s["device_id"] in visible_devices}
    filtered = [e for e in rows
                if _can_see_audit(auth, e, visible_devices, visible_services)]
    return filtered[:limit]


@app.get("/api/v1/comments")
def comment_feed(author: str = None, since: str = None, plane: str = None,
                 limit: int = 200, auth: AuthContext = Depends(get_auth)):
    """Cross-plane comment feed with a watermark cursor (#424).

    The plane's prose is write-once: a fact a person states in a comment is acted on
    in the session it arrives and then goes functionally invisible — before
    this endpoint, re-reading meant a full fetch of every thread (producer's
    sweep pulled 778 threads / 2,401 comments to find five lost facts). This
    is the missing primitive: `?author=alice&since=<watermark>` returns just the
    new prose across all three planes, oldest-first, with the next watermark —
    re-reading becomes cheap and routine instead of heroic.

    Visibility is the planes' own predicates, enforced per thread; rows on
    threads outside your scope are absent, not redacted. The watermark
    advances over hidden rows too, so a scoped caller never wedges on a
    thread it cannot see. Pages break on whole seconds: a page is extended
    to include every row sharing its final timestamp, so strict `>` iteration
    never skips same-second neighbours.
    """
    limit = max(1, min(limit, 500))
    planes = {"ticket": ("ticket_comments", "ticket_id"),
              "doc": ("doc_comments", "doc_id"),
              "task": ("task_comments", "task_id")}
    if plane and plane not in planes:
        raise HTTPException(400, "plane must be ticket|doc|task")
    want = [plane] if plane else list(planes)
    parts, params = [], []
    for p in want:
        table, col = planes[p]
        sql = (f"SELECT '{p}' AS plane, {col} AS thread_id, id AS comment_id, "
               f"author, body, created_at, session_fp, home_thread_id FROM {table}")
        conds = []
        if author:
            conds.append("author = ?"); params.append(author)
        if since:
            conds.append("created_at > ?"); params.append(since)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        parts.append(sql)
    fetch = db.query(" UNION ALL ".join(parts)
                     + " ORDER BY created_at ASC, comment_id ASC LIMIT ?",
                     tuple(params) + (limit + 50,))
    page = fetch[:limit]
    if page and len(fetch) > limit:
        # extend through same-second neighbours so `>` iteration is lossless
        edge = page[-1]["created_at"]
        for r in fetch[limit:]:
            if r["created_at"] == edge:
                page.append(r)
            else:
                break
    more = len(fetch) > len(page)

    vis_cache: dict = {}
    def _visible(p: str, tid: int) -> bool:
        if auth.is_admin():
            return True
        key = (p, tid)
        if key not in vis_cache:
            ok = False
            if p == "ticket":
                t = db.query_one("SELECT * FROM tickets WHERE id = ?", (tid,))
                ok = bool(t) and _can_see_ticket(auth, t)
            elif p == "doc":
                d = db.query_one("SELECT * FROM docs WHERE id = ?", (tid,))
                ok = bool(d) and _can_see_doc(auth, d)
            else:
                try:
                    _load_task_and_project(tid, auth)
                    ok = True
                except HTTPException:
                    ok = False
            vis_cache[key] = ok
        return vis_cache[key]

    out = [r for r in page if _visible(r["plane"], r["thread_id"])]
    watermark = page[-1]["created_at"] if page else since
    resp = {"comments": out, "count": len(out), "watermark": watermark,
            "more": more,
            "hint": ("Iterate: pass watermark back as ?since= until more=false. "
                     "Watermark reflects ALL scanned rows (hidden included), so "
                     "scoped callers advance past threads they cannot see.")}
    # Silent-empty guard (producer, #424 live-verify): an author filter that
    # matches NO known identity and NO comment ever written returns zero rows
    # indistinguishable from "no new facts" — the worst failure mode for a
    # periodic pass. Make it loud instead. (A person's handle can be `admin`,
    # not their name — the exact miss that motivated this.)
    if author and not out and not more:
        known = db.query_one("SELECT 1 FROM users WHERE username = ?", (author,)) \
            or db.query_one(
                "SELECT 1 FROM ticket_comments WHERE author = ? "
                "UNION SELECT 1 FROM doc_comments WHERE author = ? "
                "UNION SELECT 1 FROM task_comments WHERE author = ? LIMIT 1",
                (author, author, author))
        if not known:
            resp["author_unknown"] = True
            resp["warning"] = (f"'{author}' matches no registry username and has "
                               f"never authored a comment on any plane — this zero "
                               f"is a bad filter, not an empty period. "
                               f"(A handle can differ from a person's name, e.g. 'admin'.)")
    return resp


# --- Access Keys (Type 1: Access Device Enrollment) ---

class AccessKeyRequest(StrictModel):
    username: str
    device_name: str


def _authorize_access_key_mint(req: AccessKeyRequest, auth: AuthContext) -> dict:
    """Shared gate for access-key minting.

    A minted access key becomes a gateway SSH credential whose ForceCommand
    carries `req.username` as the caller's identity — so minting a key for an
    admin username yields an admin (bash-escape) gateway login. Therefore:
    admins may mint for any user; everyone else may mint only for themselves.
    Returns the target user row (raises 404/403 otherwise).
    """
    user = db.query_one("SELECT * FROM users WHERE username = ?", (req.username,))
    if not user:
        raise HTTPException(404, f"User {req.username} not found")
    if not auth.is_admin() and auth.owner_id != f"user:{user['id']}":
        raise HTTPException(
            403,
            "You may only create an access key for your own username "
            "(admins may mint for any user).",
        )
    return user


@app.post("/api/v1/access-keys")
def create_access_key(req: AccessKeyRequest, auth: AuthContext = Depends(get_auth)):
    """Generate a unique SSH keypair for a user+device.

    Returns the private key ONCE — user must download it immediately.
    Public key is stored in the registry for authorized_keys management.
    """
    _authorize_access_key_mint(req, auth)

    result = enrollment.generate_access_keypair(req.username, req.device_name)

    # Sync authorized_keys with new key
    sync_result = authorized_keys.sync()

    return {
        "key_id": result["key_id"],
        "private_key": result["private_key"],
        "public_key": result["public_key"],
        "comment": result["comment"],
        "authorized_keys_synced": sync_result,
        "warning": "Save the private key now — it cannot be retrieved again.",
    }


def _access_key_setup_script(result: dict) -> str:
    """The bash script that installs a freshly minted access key and the
    `ssh samcloud` alias. Served by POST /access-keys/setup-script and by a
    redeemed pair link (GET /pair/{token})."""
    key_id = result["key_id"]
    key_file = f"samcloud_{key_id.replace('@', '_')}.key"
    private_key = result["private_key"].strip()
    public_key = result["public_key"].strip()
    gw_host, gw_port = _gateway_ssh_endpoint()

    script = f"""#!/usr/bin/env bash
set -euo pipefail

# ╔═══════════════════════════════════════════════╗
# ║       samcloud access key setup               ║
# ╚═══════════════════════════════════════════════╝

BLUE='\\033[0;34m'
GREEN='\\033[0;32m'
CYAN='\\033[0;36m'
BOLD='\\033[1m'
DIM='\\033[2m'
NC='\\033[0m'

ok()   {{ echo -e "  ${{GREEN}}✓${{NC}} $*"; }}
info() {{ echo -e "  ${{DIM}}$*${{NC}}"; }}

KEY_FILE="$HOME/.ssh/{key_file}"

echo ""
echo -e "${{BLUE}}╔═══════════════════════════════════════════════╗${{NC}}"
echo -e "${{BLUE}}║${{NC}}       ${{BOLD}}samcloud${{NC}}  access key setup              ${{BLUE}}║${{NC}}"
echo -e "${{BLUE}}╚═══════════════════════════════════════════════╝${{NC}}"
echo ""
echo -e "  Key:    ${{BOLD}}{key_id}${{NC}}"
echo ""

# ── 1. Install private key ───────────────────────────────
echo -e "${{CYAN}}━━━ 1. Private Key ━━━${{NC}}"
mkdir -p "$HOME/.ssh"
cat > "$KEY_FILE" << 'KEYEOF'
{private_key}
KEYEOF
chmod 600 "$KEY_FILE"
ok "Installed: $KEY_FILE"

# ── 2. SSH config ────────────────────────────────────────
echo -e "${{CYAN}}━━━ 2. SSH Config ━━━${{NC}}"
SSH_CONFIG="$HOME/.ssh/config"
SAMCLOUD_HOST="samcloud"

# Anchor the match: a bare substring grep false-matches other aliases like
# "Host samcloud-pi" and would silently skip writing the real block.
if grep -qE '^Host[[:space:]]+samcloud([[:space:]]|$)' "$SSH_CONFIG" 2>/dev/null; then
    ok "Entry '${{SAMCLOUD_HOST}}' already exists"
else
    touch "$SSH_CONFIG"
    chmod 600 "$SSH_CONFIG"
    cat >> "$SSH_CONFIG" << 'SSHEOF'

# samcloud gateway — {key_id}
Host samcloud
    HostName {gw_host}
    Port {gw_port}
    User {_plane_ssh_user()}
    IdentityFile ~/.ssh/{key_file}
    IdentitiesOnly yes
    RequestTTY auto
    ServerAliveInterval 30
    ServerAliveCountMax 3
    StrictHostKeyChecking accept-new
SSHEOF
    ok "Added 'samcloud' to ~/.ssh/config"
fi

# ── 3. Verify ────────────────────────────────────────────
echo -e "${{CYAN}}━━━ 3. Verify ━━━${{NC}}"
[[ -f "$KEY_FILE" ]] && ok "Key file exists" || echo "  ! Key file missing"
grep -qE '^Host[[:space:]]+samcloud([[:space:]]|$)' "$SSH_CONFIG" 2>/dev/null && ok "SSH config ready" || echo "  ! SSH config missing"

echo ""
echo -e "${{BLUE}}╔═══════════════════════════════════════════════╗${{NC}}"
echo -e "${{BLUE}}║${{NC}}  ${{GREEN}}${{BOLD}}✓ Access key installed${{NC}}                       ${{BLUE}}║${{NC}}"
echo -e "${{BLUE}}╚═══════════════════════════════════════════════╝${{NC}}"
echo ""
echo -e "  Connect:  ${{BOLD}}ssh samcloud${{NC}}                 ${{DIM}}# interactive menu${{NC}}"
echo -e "  Run one:  ${{BOLD}}ssh samcloud \\"<device> -- <cmd>\\"${{NC}}  ${{DIM}}# non-interactive${{NC}}"
echo -e "  Key:      ${{DIM}}~/.ssh/{key_file}${{NC}}"
echo ""
"""
    return script


@app.post("/api/v1/access-keys/setup-script")
def create_access_key_script(req: AccessKeyRequest, auth: AuthContext = Depends(get_auth)):
    """Generate an access key and return a bash script that installs it.

    The script writes the private key, sets permissions, adds SSH config,
    and tests the connection. One-shot: key is generated on call.
    """
    _authorize_access_key_mint(req, auth)

    result = enrollment.generate_access_keypair(req.username, req.device_name)
    authorized_keys.sync()

    return PlainTextResponse(_access_key_setup_script(result), media_type="text/x-shellscript")


PAIR_TOKEN_TTL_SECONDS = 3600


@app.post("/api/v1/access-keys/pair")
def create_access_key_pair_link(req: AccessKeyRequest, auth: AuthContext = Depends(get_auth)):
    """Mint a single-use pair link for a device with no samcloud token.

    Same rule as minting a key (yourself, or anyone if admin). Running
    `pair_command` on the new device spends the link and installs a fresh
    access key for `username@device_name`. Valid for an hour; minting again
    for the same user and device replaces the previous link.

    `code` is the link's short form and `code_url` the console page that shows
    its command to copy: the console's /pair page draws `code_url` as a QR, and
    a device that cannot scan opens it by hand. Opening the page spends nothing;
    running the command does, and spends the long link with it.
    """
    _authorize_access_key_mint(req, auth)

    token, code = enrollment.generate_pair_token(req.username, req.device_name,
                                                 ttl_seconds=PAIR_TOKEN_TTL_SECONDS)
    code = f"{code[:5]}-{code[5:]}"
    public_url = os.environ.get("PUBLIC_URL", "http://localhost:7778")
    pair_url = f"{public_url}/api/v1/pair/{token}"
    return {
        "key_id": f"{req.username}@{req.device_name}",
        "pair_url": pair_url,
        "pair_command": f"curl -fsS {pair_url} | bash",
        "code": code,
        "code_url": f"{public_url}/p/{code}",
        "code_command": f"curl -fsS {public_url}/api/v1/pair/{code} | bash",
        "expires_in": PAIR_TOKEN_TTL_SECONDS,
    }


@app.get("/api/v1/pair/{token}")
def redeem_access_key_pair_link(token: str):
    """Spend a pair link: mint the access key it names and serve its setup script.

    `token` is the long sc_pair_ token or its short code (any case, dashes
    optional). No bearer: the single-use token is the credential. A used, expired or
    unknown link answers 410 with a script that only prints why, so
    `curl ... | bash` never runs an error body.
    """
    redeemed = enrollment.redeem_pair_token(token)
    user = None
    if redeemed:
        user = db.query_one("SELECT id FROM users WHERE username = ?",
                            (redeemed["username"],))
    if not redeemed or not user:
        message = ("samcloud: this pair link is used, expired or unknown. "
                   "Make a new one on the console's /pair page or with POST /api/v1/access-keys/pair.")
        return PlainTextResponse(f"echo {shlex.quote(message)} >&2\nexit 1\n",
                                 status_code=410, media_type="text/x-shellscript")

    result = enrollment.generate_access_keypair(redeemed["username"], redeemed["device_name"])
    authorized_keys.sync()

    return PlainTextResponse(_access_key_setup_script(result), media_type="text/x-shellscript")


@app.get("/api/v1/access-keys")
def list_access_keys(username: str = None, auth: AuthContext = Depends(get_auth)):
    """List access keys (public keys only, never private).

    Admins see all (optionally filtered by ?username=); non-admins see only
    their own keys — a non-admin ?username= for anyone else is refused, since
    the key list is an identity map of who can reach the gateway.
    """
    if not auth.is_admin():
        me = auth.identity()
        if username is not None and username != me:
            raise HTTPException(403, "You may only list your own access keys.")
        username = me
    return enrollment.list_access_keys(username)


@app.delete("/api/v1/access-keys/{key_id:path}")
def revoke_access_key(key_id: str, auth: AuthContext = Depends(get_auth)):
    """Revoke an access key. Admins may revoke any; others only their own.

    key_id is `username@device`; the username segment identifies the owner —
    without this gate anyone could revoke (lock out) any user's gateway access.
    """
    if not auth.is_admin():
        owner = key_id.split("@", 1)[0]
        if owner != auth.identity():
            raise HTTPException(403, "You may only revoke your own access keys.")
    if enrollment.revoke_access_key(key_id):
        # Sync authorized_keys to remove revoked key
        authorized_keys.sync()
        return {"status": "revoked", "key_id": key_id}
    raise HTTPException(404, f"Access key {key_id} not found")


# --- Gateway Management ---

GATEWAY_KEY_PATH = os.environ.get("GATEWAY_KEY", "/var/lib/samcloud/gateway")


@app.get("/api/v1/gateway/pubkey")
def gateway_pubkey():
    """Return the gateway's public key. Target devices need this in their authorized_keys
    so the gateway can SSH into them for routing."""
    pub_path = Path(f"{GATEWAY_KEY_PATH}.pub")
    if not pub_path.exists():
        raise HTTPException(404, "Gateway keypair not generated. Run POST /api/v1/gateway/keygen first.")
    return {"public_key": pub_path.read_text().strip()}


@app.post("/api/v1/gateway/keygen")
def gateway_keygen(auth: AuthContext = Depends(require_admin)):
    """Generate the gateway keypair (if not exists). This key is used by the gateway
    binary to SSH into target devices."""
    key_path = Path(GATEWAY_KEY_PATH)
    if key_path.exists():
        pub = Path(f"{key_path}.pub").read_text().strip()
        return {"status": "exists", "public_key": pub, "path": str(key_path)}

    key_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "samcloud-gateway"],
        capture_output=True, check=True,
    )
    os.chmod(str(key_path), 0o600)
    pub = Path(f"{key_path}.pub").read_text().strip()

    db.audit("system:gateway", "gateway.keygen", None, {"path": str(key_path)})
    return {"status": "generated", "public_key": pub, "path": str(key_path)}


@app.get("/api/v1/gateway/status")
def gateway_status(auth: AuthContext = Depends(get_auth)):
    """Check authorized_keys and gateway key state."""
    key_path = Path(GATEWAY_KEY_PATH)
    ak_status = authorized_keys.get_status()
    ak_status["gateway_key_exists"] = key_path.exists()
    if key_path.exists():
        ak_status["gateway_pubkey"] = Path(f"{key_path}.pub").read_text().strip()
    return ak_status


@app.post("/api/v1/gateway/sync")
def gateway_sync(auth: AuthContext = Depends(require_admin)):
    """Manually sync authorized_keys from registry."""
    return authorized_keys.sync()


# --- Health Probe ---

@app.post("/api/v1/probe")
async def probe_services(auth: AuthContext = Depends(require_admin)):
    """Run live health checks against all services with health_endpoint.
    Updates status and last_health in the DB. Returns results."""
    import httpx

    services = db.query("SELECT * FROM services WHERE health_endpoint IS NOT NULL")
    results = []
    now = datetime.now(timezone.utc).isoformat()

    async with httpx.AsyncClient(timeout=5.0) as client:
        for svc in services:
            # Determine check URL — tunnelled services go through the
            # local-forward listener on the sshd container.
            if svc.get("slice_port"):
                forward_host = os.environ.get("FORWARD_SSH_HOST", "localhost")
                url = f"http://{forward_host}:{svc['slice_port']}{svc['health_endpoint']}"
            elif svc.get("device_id"):
                device = db.query_one("SELECT outbound_ip FROM devices WHERE id = ?", (svc["device_id"],))
                if device and device.get("outbound_ip"):
                    url = f"http://{device['outbound_ip']}:{svc['port']}{svc['health_endpoint']}"
                else:
                    results.append({"service": svc["id"], "status": "unknown", "reason": "no reachable address"})
                    continue
            else:
                url = f"http://localhost:{svc['port']}{svc['health_endpoint']}"

            try:
                resp = await client.get(url)
                status = "active" if resp.status_code < 400 else "degraded"
                results.append({"service": svc["id"], "status": status, "http_status": resp.status_code, "url": url})
                db.execute(
                    "UPDATE services SET status = ?, last_health = ?, last_health_status = ?, updated_at = ? WHERE id = ?",
                    (status, now, resp.status_code, now, svc["id"]),
                )
            except Exception as e:
                results.append({"service": svc["id"], "status": "stopped", "error": str(e), "url": url})
                db.execute(
                    "UPDATE services SET status = 'stopped', last_health = ?, updated_at = ? WHERE id = ?",
                    (now, now, svc["id"]),
                )

    # Update device status based on their services
    devices = db.query("SELECT DISTINCT device_id FROM services")
    for dev in devices:
        dev_services = db.query("SELECT status FROM services WHERE device_id = ?", (dev["device_id"],))
        statuses = [s["status"] for s in dev_services]
        if all(s == "active" for s in statuses):
            dev_status = "online"
        elif all(s == "stopped" for s in statuses):
            dev_status = "offline"
        elif any(s == "active" for s in statuses):
            dev_status = "degraded"
        else:
            dev_status = "unknown"
        db.execute(
            "UPDATE devices SET status = ?, last_seen = ?, updated_at = ? WHERE id = ?",
            (dev_status, now, now, dev["device_id"]),
        )

    active = sum(1 for r in results if r["status"] == "active")
    stopped = sum(1 for r in results if r["status"] == "stopped")
    return {
        "probed": len(results),
        "active": active,
        "stopped": stopped,
        "degraded": len(results) - active - stopped,
        "results": results,
    }


# --- Password Hashing ---

def _hash_password(password: str) -> str:
    """Hash password with SHA-256 and random salt. Returns salt:hash."""
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def _verify_password(password: str, password_hash: str) -> bool:
    """Verify password against stored salt:hash."""
    if not password_hash or ":" not in password_hash:
        return False
    salt, stored_hash = password_hash.split(":", 1)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return h == stored_hash


# --- Auth ---

class LoginRequest(StrictModel):
    username: str
    password: str


@app.get("/api/v1/auth/verify")
def verify_token_endpoint(scope: str = None, auth: AuthContext = Depends(get_auth)):
    """Verify a token and return identity + scopes. For service-to-service auth.

    Services forward the caller's Bearer token to this endpoint.
    Returns identity and scopes so the service can make access decisions.

    Optional: ?scope=device:test-host — returns 403 if token doesn't have that scope.
    """
    # Resolve username from owner_id
    username = None
    user_id = None
    role = None
    if auth.owner_id.startswith("user:"):
        uid = int(auth.owner_id.split(":")[1])
        user = db.query_one("SELECT id, username, role FROM users WHERE id = ?", (uid,))
        if user:
            username = user["username"]
            user_id = user["id"]
            role = user["role"]

    # Optional scope check — resolves group membership
    if scope and not auth.is_admin():
        has_scope = auth.has_scope(scope)
        # Check device scope via group resolution
        if not has_scope and scope.startswith("device:"):
            device_id = scope.split(":", 1)[1]
            has_scope = auth.can_manage_device(device_id)
        # Check direct scope match
        if not has_scope:
            for s in auth.scopes:
                if s == scope or scope.startswith(s):
                    has_scope = True
                    break
        if not has_scope:
            raise HTTPException(403, f"Token does not have scope: {scope}")

    return {
        "valid": True,
        "token_type": auth.token_type,
        "owner_id": auth.owner_id,
        "user_id": user_id,
        "username": username,
        "role": role,
        "scopes": auth.scopes,
        # Scope v1: your groups with your role in each, and where an unnamed
        # write goes — the stage's F1 (four seats): "first group" was undefined
        # and stated differently by the guide and auth.md.
        "groups": groupscope.memberships(auth) if not auth.is_admin() else "*",
        "default_group": groupscope.default_group(auth),
        "operator_time": _operator_now(),
    }


@app.get("/api/v1/auth/can-access")
def check_device_access(username: str, device_id: str, auth: AuthContext = Depends(require_admin)):
    """Can a user enter a device? The gateway's admission oracle (it holds an
    admin bearer). One predicate with the API: the user's stored scopes, the
    admin role, and every `group_members` row — before scope v1 train 0 this
    read the scope string alone and took no bearer at all (doc 262)."""
    user = db.query_one("SELECT * FROM users WHERE username = ?", (username,))
    if not user:
        return {"access": False, "reason": "user not found"}

    scopes = _effective_user_scopes(user)
    if "*" in scopes or "admin" in scopes:
        return {"access": True, "reason": "admin"}
    if f"device:{device_id}" in scopes:
        return {"access": True, "reason": f"device:{device_id} scope"}
    for scope in scopes:
        if scope.startswith("group:"):
            group_id = scope.split(":", 1)[1]
            if db.query_one("SELECT 1 FROM group_devices WHERE group_id = ? AND device_id = ?",
                            (group_id, device_id)):
                return {"access": True, "reason": f"group:{group_id} membership"}
    return {"access": False, "reason": "device not in user scope"}


@app.get("/api/v1/auth/accessible-devices")
def accessible_devices(username: str, auth: AuthContext = Depends(require_admin)):
    """The devices a user can enter, for the gateway's target listing — the
    same predicate as `can-access`, so what `devices --json` shows is what
    admission allows (before train 0 the gateway listed the whole fleet to
    every SSH user with its own token). Sensitive fields stripped as for a
    non-admin reader."""
    user = db.query_one("SELECT * FROM users WHERE username = ?", (username,))
    if not user:
        return {"username": username, "devices": []}
    visible = _device_ids_for_scopes(_effective_user_scopes(user))
    devices = db.query("SELECT * FROM devices ORDER BY context, id")
    if visible is not None:
        devices = [d for d in devices if d["id"] in visible]
    return {"username": username, "devices": [_strip_sensitive(d, None) for d in devices]}


@app.post("/api/v1/auth/login")
def login(req: LoginRequest):
    """Authenticate user against registry. Returns user info on success."""
    user = db.query_one("SELECT * FROM users WHERE username = ?", (req.username.lower(),))
    if not user:
        raise HTTPException(401, "Invalid credentials")

    if not _verify_password(req.password, user.get("password_hash", "")):
        raise HTTPException(401, "Invalid credentials")

    # Each login gets its own token, valid a week, and leaves the user's
    # long-lived token (from init, activation or regenerate-token) alone (#1010).
    scopes = json.loads(user["scopes"]) if user.get("scopes") else []
    token = enrollment.generate_session_token(f"user:{user['id']}", scopes=scopes)

    db.audit(f"user:{user['username']}", "auth.login", user["username"])
    return {
        "username": user["username"],
        "role": user["role"],
        "scopes": scopes,
        "token": token,
        "expires_in": enrollment.SESSION_TTL_SECONDS,
    }


@app.post("/api/v1/auth/logout")
def logout(authorization: Optional[str] = Header(None)):
    """End the login whose token this is. A long-lived token is not revoked here."""
    token = enrollment.extract_bearer_token(authorization)
    if not token or not enrollment.revoke_session_token(token):
        raise HTTPException(404, "Not a login's token; long-lived tokens are revoked with regenerate-token")
    return {"status": "logged out"}


# --- Admin Init ---

@app.get("/api/v1/setup/status")
def setup_status():
    """Is the plane initialised — does any user exist? The one fact the console
    needs before a login, answered without a bearer and without a user list
    (scope v1 train 0: `GET /users` no longer answers unauthenticated)."""
    users = db.query("SELECT count(*) as n FROM users")
    return {"initialized": users[0]["n"] > 0}


@app.post("/api/v1/init")
def admin_init(req: AdminInit):
    """First-run setup. Creates admin user with password.
    Only works when users table is empty."""
    users = db.query("SELECT count(*) as n FROM users")
    if users[0]["n"] > 0:
        raise HTTPException(409, "System already initialized — users exist")

    _validate_user_identity(req.username)
    password_hash = _hash_password(req.password)
    now = datetime.now(timezone.utc).isoformat()

    with db.transaction() as conn:
        cursor = conn.execute("""
            INSERT INTO users (username, role, scopes, password_hash, created_at, updated_at)
            VALUES (?, 'admin', ?, ?, ?, ?)
        """, (req.username, json.dumps(["*"]), password_hash, now, now))
        user_id = cursor.lastrowid

    # Generate admin token
    token = enrollment.generate_device_token(f"user:{user_id}", scopes=["*"])

    db.audit("system:init", "admin.init", f"user:{user_id}",
             {"username": req.username})

    return {"status": "initialized", "user_id": user_id, "token": token}


# --- User Management ---

@app.get("/api/v1/users")
def list_users(auth: AuthContext = Depends(get_auth)):
    """List users. Admin sees all; non-admin sees only self (the roster of a
    group is `GET /groups/{id}`). A bearer is required — scope v1 train 0."""
    users = db.query("SELECT id, username, role, scopes, created_at, updated_at, last_seen FROM users ORDER BY id")
    if not auth.is_admin():
        # Non-admin: filter to self only
        if auth.owner_id.startswith("user:"):
            self_id = int(auth.owner_id.split(":")[1])
            users = [u for u in users if u["id"] == self_id]
        else:
            users = []
    return users


@app.post("/api/v1/users")
def create_user(req: UserCreate, auth: AuthContext = Depends(require_admin)):
    """Create a new user. Admin only."""
    _validate_user_identity(req.username)
    existing = db.query_one("SELECT id FROM users WHERE username = ?", (req.username,))
    if existing:
        raise HTTPException(409, f"User {req.username} already exists")

    password_hash = _hash_password(req.password)
    now = datetime.now(timezone.utc).isoformat()

    # Scope v1: membership is the store. A `group:<g>` scope on the new user
    # is also a membership row, at the role `group_roles` names (viewer by
    # default) — as the invite has always done; the two stores no longer drift.
    roles = {}
    for sc in (req.scopes or []):
        if sc.startswith("group:"):
            g = sc.split(":", 1)[1]
            if not db.query_one("SELECT id FROM groups WHERE id = ?", (g,)):
                continue                      # a scope naming no group: kept on the token, no row to write
            try:
                roles[g] = groupscope.normalise_role((req.group_roles or {}).get(g, "viewer"))
            except ValueError as e:
                raise HTTPException(422, str(e))

    user_id = db.execute("""
        INSERT INTO users (username, role, scopes, password_hash, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (req.username, req.role, json.dumps(req.scopes or []), password_hash, now, now))
    for g, role in roles.items():
        db.execute("INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)",
                   (g, user_id, role))

    db.audit("system:users", "user.create", f"user:{user_id}",
             {"username": req.username, "role": req.role, "groups": roles})

    return {"status": "created", "id": user_id, "user_id": user_id, "username": req.username}


@app.get("/api/v1/users/{user_id}")
def get_user(user_id: int, auth: AuthContext = Depends(get_auth)):
    """A user with their groups. The admin and the user see the whole row; a
    fellow member of any of their groups sees the name, the role and the
    groups they share; anyone else is told the user does not exist (doc 261,
    decision 4). Before scope v1 train 0 this answered without a bearer."""
    user = db.query_one(
        "SELECT id, username, role, scopes, created_at, updated_at FROM users WHERE id = ?",
        (user_id,),
    )
    if not user:
        raise HTTPException(404, f"User {user_id} not found")

    groups = db.query("""
        SELECT g.id, g.name, g.description, g.route_path, gm.role as member_role
        FROM group_members gm
        JOIN groups g ON g.id = gm.group_id
        WHERE gm.user_id = ?
        ORDER BY g.name
    """, (user_id,))
    is_self = auth.owner_id == f"user:{user_id}"
    if auth.is_admin() or is_self:
        user["groups"] = groups
        return user
    mine = set(auth.get_group_scopes())
    shared = [g for g in groups if g["id"] in mine]
    if not shared:
        raise HTTPException(404, f"User {user_id} not found")
    return {"id": user["id"], "username": user["username"], "role": user["role"],
            "groups": shared}


@app.patch("/api/v1/users/{user_id}")
def update_user(user_id: int, req: UserUpdate, auth: AuthContext = Depends(require_admin)):
    """Update user role and/or scopes. Rotates token if scopes change. Admin only."""
    user = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(404, f"User {user_id} not found")

    updates, params = [], []
    if req.role is not None:
        updates.append("role = ?")
        params.append(req.role)
    if req.scopes is not None:
        updates.append("scopes = ?")
        params.append(json.dumps(req.scopes))

    if updates:
        updates.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(user_id)
        db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", tuple(params))

    # Scope changes update the live token row IN PLACE by default. Rotation
    # invalidates a credential the caller may not be positioned to re-deliver
    # to the seat — a routine additive grant silently killed 12 rails for 8h
    # on 2026-08-16 (ticket #472). Minting is only ever explicit: rotate_token
    # here, or POST /users/{id}/regenerate-token.
    new_token = None
    scopes_updated_in_place = False
    if req.scopes is not None:
        role = req.role or user["role"]
        if req.rotate_token:
            if role == "agent":
                new_token = enrollment.generate_agent_token(f"user:{user_id}", scopes=req.scopes)
            else:
                new_token = enrollment.generate_device_token(f"user:{user_id}", scopes=req.scopes)
            enrollment.revoke_owner_sessions(f"user:{user_id}")
        else:
            db.execute(
                "UPDATE tokens SET scopes = ? WHERE owner_id = ? AND type IN ('agent', 'device')",
                (json.dumps(req.scopes), f"user:{user_id}"))
            scopes_updated_in_place = True

    db.audit(auth.owner_id, "user.update", f"user:{user_id}",
             {"role": req.role, "scopes": req.scopes, "token_rotated": new_token is not None})

    result = {"status": "updated", "user_id": user_id}
    if new_token:
        result["token"] = new_token
        result["note"] = "Scopes changed — token rotated. The previous token and every login are now invalid."
    elif scopes_updated_in_place:
        result["token_rotated"] = False
        result["note"] = ("Scopes updated in place — the existing token keeps working. "
                          "Pass rotate_token=true or use POST /users/{id}/regenerate-token "
                          "if you intend to invalidate it.")
    return result


@app.post("/api/v1/users/{user_id}/regenerate-token")
def regenerate_user_token(user_id: int, auth: AuthContext = Depends(require_admin)):
    """Regenerate a user's API token. Admin only.

    Returns a reset message with token, guide URL, and setup instructions.
    Copy the message and hand it to the agent — one paste gets them back online.
    """
    user = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(404, f"User {user_id} not found")

    scopes = json.loads(user.get("scopes", "[]"))
    if user["role"] == "agent":
        new_token = enrollment.generate_agent_token(f"user:{user_id}", scopes=scopes)
    else:
        new_token = enrollment.generate_device_token(f"user:{user_id}", scopes=scopes)
    enrollment.revoke_owner_sessions(f"user:{user_id}")

    db.audit(auth.owner_id, "user.token.regenerated", f"user:{user_id}",
             {"username": user["username"]})

    public_url = os.environ.get("PUBLIC_URL", "http://localhost:7778")

    return {
        "user_id": user_id,
        "username": user["username"],
        "token": new_token,
        "guide_url": f"{public_url}/api/v1/users/me/guide",
        "message": f"""Your samcloud token for {user['username']} has been reset.

Token: {new_token}

{_token_install_text(public_url)}""",
    }


def _token_install_text(public_url: str) -> str:
    """The one way a token is installed on a seat, taught wherever a token is
    handed out (activation, regeneration, the guide). The token lives in a 0600
    file and travels in a header file; it never goes on a command line (#582)."""
    return f"""Install it on the seat — the token never goes on a command line:
  umask 077; mkdir -p ~/.samcloud
  cat > ~/.samcloud/token                                   # paste the token, Enter, Ctrl-D
  printf 'Authorization: Bearer %s\\n' "$(cat ~/.samcloud/token)" > ~/.samcloud/token.hdr

Every call then authenticates with the header file:
  curl -s -H @"$HOME/.samcloud/token.hdr" {public_url}/api/v1/auth/verify

Your operating guide is the served skill (it keeps itself current on the seat):
  curl -s {public_url}/api/v1/skills/install.sh | bash      # installs into ~/.claude/skills
  curl -s {public_url}/api/v1/skills/samcloud-agent/SKILL.md

Each conversation has its own history. Comment labels name the identity, its home
thread and fingerprint; a sibling's write is not your own. Keep X-Session-Id on
writes (sc-say supplies it). GET /wake/threads shows the recorded home_thread_id.

Hear the threads you are on when no session of yours is running — install the rail once:
  curl -s -H @"$HOME/.samcloud/token.hdr" {public_url}/api/v1/helpers/wake-install | bash
(The rail runs Claude Code. On any other harness, hear your threads with
  curl -s -H @"$HOME/.samcloud/token.hdr" "{public_url}/api/v1/notifications?unread=true"
and write with POST …/tickets/{{id}}/comments — the skill's tools page shows both.)

The API index: curl -s {public_url}/api/v1/"""


@app.delete("/api/v1/users/{user_id}")
def delete_user(user_id: int, auth: AuthContext = Depends(require_admin)):
    """Delete a user and all their tokens. Admin only."""
    user = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(404, f"User {user_id} not found")

    with db.transaction() as conn:
        conn.execute("DELETE FROM tokens WHERE owner_id = ?", (f"user:{user_id}",))
        conn.execute("DELETE FROM group_members WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    db.audit(auth.owner_id, "user.deleted", f"user:{user_id}",
             {"username": user["username"]})

    return {"status": "deleted", "user_id": user_id, "username": user["username"]}


# --- User Invite / Activation ---

@app.post("/api/v1/users/invite")
def invite_user(req: UserInvite, auth: AuthContext = Depends(get_auth)):
    """Create a user and generate an invite token.

    Admin can invite anyone with any scope.
    Group owners/editors can invite into their group(s) with role up to their own.
    Viewers cannot invite.
    """
    # Validate every requested grant before creating either a user or token.
    _validate_user_identity(req.username)
    if req.role not in {"agent", "user", "admin"}:
        raise HTTPException(400, "Invalid user role")
    if not auth.is_admin() and req.role == "admin":
        raise HTTPException(403, "Only an administrator can invite an administrator")
    inviter_groups = auth.get_group_scopes()
    if not auth.is_admin() and not any(groupscope.can_edit(auth, g) for g in inviter_groups):
        raise HTTPException(403, "An editor role in a group is required to invite users")
    roles = {}
    group_scopes = {s.split(":", 1)[1] for s in req.scopes if s.startswith("group:")}
    if set(req.group_roles or {}) - group_scopes:
        raise HTTPException(400, "Group roles must name a group in the invite scopes")
    for scope in req.scopes:
        if scope.startswith("group:"):
            gid = scope.split(":", 1)[1]
            if not auth.is_admin() and not groupscope.can_edit(auth, gid):
                raise HTTPException(403, "Can only invite into groups you can edit")
            if not db.query_one("SELECT 1 FROM groups WHERE id = ?", (gid,)):
                raise HTTPException(400, "Unknown group in invite scope")
            try:
                role = groupscope.normalise_role((req.group_roles or {}).get(gid, "viewer"))
            except ValueError as e:
                raise HTTPException(400, str(e))
            if not auth.is_admin() and groupscope.rank(role) > groupscope.rank(groupscope.role_in(auth, gid)):
                raise HTTPException(403, "Cannot grant a group role above your own")
            roles[gid] = role
        elif not auth.is_admin():
            if not scope.startswith("device:") or not auth.can_manage_device(scope.split(":", 1)[1]):
                raise HTTPException(403, "Cannot grant the requested scope")

    existing = db.query_one("SELECT id FROM users WHERE username = ?", (req.username,))
    if existing:
        raise HTTPException(409, f"User {req.username} already exists")

    now = datetime.now(timezone.utc).isoformat()

    # Create user without password (activated via invite)
    with db.transaction() as conn:
        cursor = conn.execute("""
            INSERT INTO users (username, role, scopes, password_hash, created_at, updated_at)
            VALUES (?, ?, ?, NULL, ?, ?)
        """, (req.username, req.role, json.dumps(req.scopes), now, now))
        user_id = cursor.lastrowid

    # Generate invite token
    token = enrollment.generate_agent_invite(
        f"user:{user_id}", scopes=req.scopes, ttl_seconds=req.ttl
    )

    for group_id, requested_role in roles.items():
        db.execute(
            "INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)",
            (group_id, user_id, requested_role),
        )

    public_url = os.environ.get("PUBLIC_URL", "http://localhost:7778")
    brief_url = f"{public_url}/api/v1/users/brief/{token}"

    db.audit(auth.owner_id, "user.invite", f"user:{user_id}",
             {"username": req.username, "role": req.role, "scopes": req.scopes})

    return {
        "user_id": user_id,
        "username": req.username,
        "role": req.role,
        "scopes": req.scopes,
        "token": token,
        "brief_url": brief_url,
    }


@app.get("/api/v1/users/brief/{token}")
def user_briefing(token: str):
    """Serve a briefing document for an invited user/agent.

    Readable multiple times (not consumed on read). Consumed on activation.
    """
    if not token.startswith("sc_invite_"):
        raise HTTPException(404, "Invalid token")

    token_hash = enrollment._hash_token(token)
    token_record = db.query_one(
        "SELECT * FROM tokens WHERE token_hash = ? AND type = 'enrollment'",
        (token_hash,),
    )
    if not token_record:
        raise HTTPException(404, "Token not found or expired")

    owner_id = token_record["owner_id"]  # e.g. "user:123"
    user_id = int(owner_id.split(":")[1])
    user = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(404, "User not found")

    scopes = json.loads(user.get("scopes", "[]"))
    public_url = os.environ.get("PUBLIC_URL", "http://localhost:7778")

    # Gather scope context
    scope_description = []
    services_in_scope = []
    for scope in scopes:
        if scope.startswith("device:"):
            device_id = scope.split(":", 1)[1]
            device = db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,))
            scope_description.append(f"All services on **{device_id}**" + (f" ({device['name']})" if device else ""))
            svcs = db.query("SELECT id, name, status, port FROM services WHERE device_id = ?", (device_id,))
            services_in_scope.extend(svcs)
        elif scope.startswith("group:"):
            group_id = scope.split(":", 1)[1]
            group = db.query_one("SELECT * FROM groups WHERE id = ?", (group_id,))
            scope_description.append(f"All services in group **{group_id}**" + (f" ({group['name']})" if group else ""))
            svcs = db.query("""
                SELECT s.id, s.name, s.status, s.port FROM services s
                JOIN group_services gs ON gs.service_id = s.id
                WHERE gs.group_id = ?
            """, (group_id,))
            services_in_scope.extend(svcs)
        elif scope == "*":
            scope_description.append("**Full admin access** (all devices and services)")

    svc_table = "\n".join(
        f"| `{s['id']}` | `{s['status']}` | `{s['port'] or 'pending'}` |"
        for s in services_in_scope
    ) if services_in_scope else "| (none yet) | | |"

    brief = f"""# Invitation: {user['username']}

You've been invited to join the **samcloud** registry as **{user['role']}**.

## Your Scope

{chr(10).join(f"- {d}" for d in scope_description) if scope_description else "- No specific scope assigned"}

## Existing Services in Your Scope

| Service | Status | Port |
|---------|--------|------|
{svc_table}

## What You Can Do

Once activated, your token lets you:
- **Create services** on devices in your scope
- **Update services** you own (PATCH — version, capabilities, port, health endpoint, status)
- **Delete services** you own
- **Report health** for your services
- **File tickets** and comment on any ticket
- **Discover** all services and devices (read is public)

## How to Activate

```bash
curl -s -X POST "{public_url}/api/v1/users/activate" \\
  -H "Content-Type: application/json" \\
  -d '{{"token": "{token}", "password": "<choose-a-password>"}}'
```

Password is required — it enables console login and API key rotation.
This returns your **permanent API token**. Store it securely — it's your identity.

## After Activation

Your token works as a Bearer token on all API calls:

```bash
curl -s {public_url}/api/v1/ \\
  -H @"$HOME/.samcloud/token.hdr"
```

Read your permanent operations guide:
```bash
curl -s {public_url}/api/v1/users/me/guide \\
  -H @"$HOME/.samcloud/token.hdr"
```

## Install Your Agent Skills

If you run as a Claude agent, install the samcloud skill set — the executable
form of these docs. It teaches you to operate here (tickets, services, leases,
the model gateway) from your token alone, and loads automatically when relevant:

```bash
curl -fsSL {public_url}/api/v1/skills/install.sh | bash
```

This drops the skill(s) into `~/.claude/skills/`. List what's available with
`GET {public_url}/api/v1/skills`. Re-run anytime to update.

## Creating a Service

```bash
curl -s -X POST "{public_url}/api/v1/services" \\
  -H @"$HOME/.samcloud/token.hdr" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "name": "my-service",
    "device_id": "<device-in-your-scope>",
    "port": 8080,
    "health_endpoint": "/health",
    "subdomain": "my-service-stg"
  }}'
```

## Tips

- **API index**: `GET {public_url}/api/v1/` — lists all endpoints
- **Your guide**: `GET {public_url}/api/v1/users/me/guide` — permanent reference (needs your token)
- **File tickets** when something doesn't work — humans and other agents monitor them
"""

    return PlainTextResponse(brief, media_type="text/markdown")


@app.post("/api/v1/users/activate")
def activate_user(req: UserActivate):
    """Activate an invited user. Consumes the invite token, returns permanent API token.

    If password is provided, sets it for console login (human flow).
    If not, token-only (agent flow).
    """
    token_record = enrollment.validate_enrollment_token(req.token)
    if not token_record:
        raise HTTPException(401, "Invalid or expired invite token")

    owner_id = token_record["owner_id"]  # e.g. "user:123"
    user_id = int(owner_id.split(":")[1])
    user = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(404, "User not found")

    scopes = json.loads(user.get("scopes", "[]"))

    # Set password (required — enables console login and key rotation)
    password_hash = _hash_password(req.password)
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))

    # Generate permanent token based on role
    if user["role"] == "agent":
        api_token = enrollment.generate_agent_token(f"user:{user_id}", scopes=scopes)
    else:
        api_token = enrollment.generate_device_token(f"user:{user_id}", scopes=scopes)

    public_url = os.environ.get("PUBLIC_URL", "http://localhost:7778")

    db.audit("system:enrollment", "user.activated", f"user:{user_id}",
             {"username": user["username"], "role": user["role"],
              "has_password": req.password is not None})

    event_bus.emit("user.activated", f"user:{user_id}",
                   {"username": user["username"], "role": user["role"], "scopes": scopes})

    return {
        "user_id": user_id,
        "username": user["username"],
        "role": user["role"],
        "scopes": scopes,
        "token": api_token,
        "endpoints": {
            "guide": f"{public_url}/api/v1/users/me/guide",
            "events": f"{public_url}/api/v1/events",
            "events_recent": f"{public_url}/api/v1/events/recent",
            "services": f"{public_url}/api/v1/services",
            "tickets": f"{public_url}/api/v1/tickets",
            "api_index": f"{public_url}/api/v1/",
        },
        "next_steps": [
            "Save the token in ~/.samcloud/token (0600) and build the header file ~/.samcloud/token.hdr — "
            "the token never goes on a command line: "
            "umask 077; mkdir -p ~/.samcloud; cat > ~/.samcloud/token (paste, Enter, Ctrl-D); "
            "printf 'Authorization: Bearer %s\\n' \"$(cat ~/.samcloud/token)\" > ~/.samcloud/token.hdr",
            f"Check it: curl -s -H @\"$HOME/.samcloud/token.hdr\" {public_url}/api/v1/auth/verify",
            f"Your operating guide is the served skill: curl -s {public_url}/api/v1/skills/install.sh | bash "
            f"(installs into ~/.claude/skills); read {public_url}/api/v1/skills/samcloud-agent/SKILL.md",
            f"Hear the threads you are on while no session of yours is running — install the rail once: "
            f"curl -s -H @\"$HOME/.samcloud/token.hdr\" {public_url}/api/v1/helpers/wake-install | bash; "
            f"it then appears in {public_url}/api/v1/wake/registrations. The rail runs Claude Code; on any "
            f"other harness, hear your threads with GET {public_url}/api/v1/notifications?unread=true and "
            f"write with POST …/comments — the skill's tools page shows both",
            f"The API index: curl -s {public_url}/api/v1/",
        ],
    }


# ── Saved views (#203 v2, doc #34): filters over data, zero hardcoded sections ──

_VIEW_ENTITIES = {"tickets", "tasks", "notifications", "docs"}
_VIEW_FILTER_KEYS = {"assignee", "status", "tag", "priority", "project",
                     "type", "kind", "age_hours", "unread", "group_scope"}
_VIEW_SORTS = {
    "tickets": {"priority", "created_at", "updated_at", "id"},
    "tasks": {"estimate_min", "created_at", "updated_at", "position", "id"},
    "notifications": {"id", "created_at"},
    "docs": {"created_at", "updated_at", "id"},
}
_VIEW_GROUPS = {
    "tickets": {"priority", "status", "type"},
    "tasks": {"project", "status"},
    "notifications": set(),
    "docs": {"kind", "status"},
}


def _aslist(v):
    return v if isinstance(v, list) else [v]


def _validate_view(entity, filters, sort, group_by, limit_per_group):
    """400-loud validation. Filters/sorts/groups only express what the data
    model supports — new needs mean new data attributes, never view hacks."""
    if entity not in _VIEW_ENTITIES:
        raise HTTPException(400, f"entity must be one of {sorted(_VIEW_ENTITIES)}")
    filters = filters or {}
    bad = set(filters) - _VIEW_FILTER_KEYS
    if bad:
        raise HTTPException(400, f"Unknown filter keys {sorted(bad)} "
                                 f"(have: {sorted(_VIEW_FILTER_KEYS)})")
    for t in _aslist(filters.get("tag") or []):
        if not _TAG_RE.match(str(t)):
            raise HTTPException(400, f"Invalid tag filter '{t}'")
    if "age_hours" in filters:
        try:
            ah = int(filters["age_hours"])
        except (TypeError, ValueError):
            raise HTTPException(400, "age_hours must be an integer")
        if not 1 <= ah <= 8760:
            raise HTTPException(400, "age_hours must be 1-8760")
    if sort:
        field = sort.split(":", 1)[0]
        if field not in _VIEW_SORTS[entity]:
            raise HTTPException(400, f"sort '{field}' not allowed for {entity} "
                                     f"(have: {sorted(_VIEW_SORTS[entity])})")
    if group_by and group_by not in _VIEW_GROUPS[entity]:
        raise HTTPException(400, f"group_by '{group_by}' not allowed for {entity} "
                                 f"(have: {sorted(_VIEW_GROUPS[entity])})")
    if limit_per_group is not None and not 1 <= int(limit_per_group) <= 50:
        raise HTTPException(400, "limit_per_group must be 1-50")


_SEED_VIEWS = [
    # The four prototype sections, reborn as data (doc #34): editable,
    # renameable, deletable. Proof the view model subsumes the hardcode.
    ("Blocked on you", "tickets",
     {"assignee": ":me", "status": ["open", "triaged", "in_progress"]},
     "priority", None, None),
    ("Decisions", "tasks",
     {"tag": ["decision"], "status": ["todo", "in_progress", "blocked"]},
     "id:desc", None, None),
    ("Pick-me menu", "tasks",
     {"status": ["todo"]}, "estimate_min", "project", 1),
    ("Unread", "notifications", {"unread": True}, "id:desc", None, None),
]


def _seed_views(owner: str):
    for i, (name, entity, filters, sort, group_by, lpg) in enumerate(_SEED_VIEWS):
        db.execute("INSERT INTO views (owner, name, entity, filters, sort, "
                   "group_by, limit_per_group, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (owner, name, entity, json.dumps(filters), sort, group_by, lpg, i))


def _view_row(v):
    try:
        v["filters"] = json.loads(v["filters"]) if v.get("filters") else {}
    except (ValueError, TypeError):
        v["filters"] = {}
    return v


def _order_sql(entity: str, sort: str, prefix: str = ""):
    if not sort:
        return f"ORDER BY {prefix}id DESC"
    field, _, dirn = sort.partition(":")
    dirn = "DESC" if dirn.lower() == "desc" else "ASC"
    if field == "priority":  # semantic priority order, not alphabetical
        return (f"ORDER BY CASE {prefix}priority WHEN 'critical' THEN 0 "
                f"WHEN 'high' THEN 1 WHEN 'med' THEN 2 WHEN 'medium' THEN 2 "
                f"ELSE 3 END {dirn}, {prefix}id DESC")
    if field == "estimate_min":
        return f"ORDER BY COALESCE({prefix}estimate_min, 999999) {dirn}, {prefix}id DESC"
    return f"ORDER BY {prefix}{field} {dirn}, {prefix}id DESC"


def _resolve_view(view: dict, auth: AuthContext, cap: int = 500):
    """Execute a view server-side through the normal scope predicates."""
    f = view["filters"] or {}
    me = auth.identity()
    entity = view["entity"]

    def _in(clause_field, values, clauses, params):
        vals = _aslist(values)
        clauses.append(f"{clause_field} IN ({','.join('?' * len(vals))})")
        params.extend(vals)

    if entity == "tickets":
        clauses, params = [], []
        if f.get("assignee"):
            clauses.append("assignee = ?")
            params.append(me if f["assignee"] == ":me" else f["assignee"])
        if f.get("group_scope"):  # H2 (#380): same union semantics as list_tickets
            gs = str(f["group_scope"])
            clauses.append("(group_scope = ? "
                           "OR device_id IN (SELECT device_id FROM group_devices WHERE group_id = ?) "
                           "OR service_id IN (SELECT service_id FROM group_services WHERE group_id = ?))")
            params.extend([gs, gs, gs])
        if f.get("status"):
            _in("status", f["status"], clauses, params)
        if f.get("priority"):
            _in("priority", f["priority"], clauses, params)
        if f.get("type"):
            _in("type", f["type"], clauses, params)
        for t in _aslist(f.get("tag") or []):
            c, p = _tag_clause(t)
            clauses.append(c)
            params.append(p)
        if f.get("age_hours"):
            clauses.append("updated_at >= datetime('now', ?)")
            params.append(f"-{int(f['age_hours'])} hours")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = db.query(
            "SELECT tickets.*, (SELECT SUBSTR(body, 1, 240) FROM ticket_comments "
            "WHERE ticket_id = tickets.id ORDER BY id DESC LIMIT 1) AS latest_comment "
            f"FROM tickets{where} "
            + _order_sql(entity, view.get("sort")) + " LIMIT ?",
            tuple(params) + (cap,))
        items = [_row_tags(r) for r in rows if _can_see_ticket(auth, r)]
        gkey = view.get("group_by")
    elif entity == "tasks":
        clauses, params = [], []
        if f.get("assignee"):
            clauses.append("t.assignee = ?")
            params.append(me if f["assignee"] == ":me" else f["assignee"])
        if f.get("status"):
            _in("t.status", f["status"], clauses, params)
        if f.get("priority"):
            _in("t.priority", f["priority"], clauses, params)
        for t in _aslist(f.get("tag") or []):
            c, p = _tag_clause(t)
            clauses.append("t." + c)
            params.append(p)
        if f.get("project"):
            proj = _resolve_project(str(f["project"]))
            if not proj:
                raise HTTPException(400, f"Unknown project '{f['project']}' in view filter")
            clauses.append("t.project_id = ?")
            params.append(proj["id"])
        if f.get("group_scope"):  # tasks ride their project's scope (#330)
            clauses.append("p.group_scope = ?")
            params.append(str(f["group_scope"]))
        if f.get("age_hours"):
            clauses.append("t.updated_at >= datetime('now', ?)")
            params.append(f"-{int(f['age_hours'])} hours")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = db.query(
            "SELECT t.*, p.slug AS project, (SELECT SUBSTR(body, 1, 240) FROM task_comments "
            "WHERE task_id = t.id ORDER BY id DESC LIMIT 1) AS latest_comment "
            "FROM tasks t "
            f"JOIN projects p ON p.id = t.project_id{where} "
            + _order_sql(entity, view.get("sort"), "t.") + " LIMIT ?",
            tuple(params) + (cap,))
        seen_proj: dict = {}

        def _see(pid):
            if pid not in seen_proj:
                proj = db.query_one("SELECT * FROM projects WHERE id = ?", (pid,))
                seen_proj[pid] = bool(proj) and _can_see_project(auth, proj)
            return seen_proj[pid]

        items = [_row_tags(r) for r in rows if _see(r["project_id"])]
        gkey = view.get("group_by")
    elif entity == "docs":
        clauses, params = [], []
        if f.get("assignee"):
            clauses.append("source = ?")
            params.append(me if f["assignee"] == ":me" else f["assignee"])
        if f.get("group_scope"):
            clauses.append("group_scope = ?")
            params.append(str(f["group_scope"]))
        if f.get("status"):
            _in("status", f["status"], clauses, params)
        if f.get("kind"):
            _in("kind", f["kind"], clauses, params)
        if f.get("age_hours"):
            clauses.append("updated_at >= datetime('now', ?)")
            params.append(f"-{int(f['age_hours'])} hours")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = db.query(
            "SELECT docs.*, (SELECT SUBSTR(body, 1, 240) FROM doc_comments "
            "WHERE doc_id = docs.id ORDER BY id DESC LIMIT 1) AS latest_comment "
            f"FROM docs{where} "
            + _order_sql(entity, view.get("sort")) + " LIMIT ?",
            tuple(params) + (cap,))
        items = [r for r in rows if _can_see_doc(auth, r)]
        gkey = view.get("group_by")
    else:  # notifications — always personal
        clauses, params = ["recipient = ?"], [me]
        if f.get("unread"):
            clauses.append("read = 0")
        if f.get("age_hours"):
            clauses.append("created_at >= datetime('now', ?)")
            params.append(f"-{int(f['age_hours'])} hours")
        rows = db.query("SELECT * FROM notifications WHERE " + " AND ".join(clauses)
                        + " " + _order_sql(entity, view.get("sort")) + " LIMIT ?",
                        tuple(params) + (cap,))
        for n in rows:
            try:
                n["payload"] = json.loads(n["payload"]) if n.get("payload") else {}
            except (ValueError, TypeError):
                n["payload"] = {}
        items = rows
        gkey = None

    if gkey:
        lpg = view.get("limit_per_group")
        groups, order = {}, []
        for it in items:
            k = str(it.get(gkey))
            if k not in groups:
                groups[k] = []
                order.append(k)
            if lpg is None or len(groups[k]) < lpg:
                groups[k].append(it)
        return {"view": view, "grouped_by": gkey,
                "groups": [{"key": k, "items": groups[k]} for k in order],
                "total": sum(len(groups[k]) for k in order)}
    return {"view": view, "items": items, "total": len(items)}


@app.get("/api/v1/views")
def list_views(auth: AuthContext = Depends(get_auth)):
    """Your saved views, position-ordered. First call seeds the four default
    sections AS ROWS (doc #34) — edit, rename, or delete them freely."""
    me = auth.identity()
    rows = db.query("SELECT * FROM views WHERE owner = ? "
                    "ORDER BY (position IS NULL), position, id", (me,))
    if not rows:
        _seed_views(me)
        rows = db.query("SELECT * FROM views WHERE owner = ? "
                        "ORDER BY (position IS NULL), position, id", (me,))
    return {"views": [_view_row(v) for v in rows], "seeded": len(rows) == len(_SEED_VIEWS)}


@app.post("/api/v1/views")
def create_view(req: ViewCreate, auth: AuthContext = Depends(get_auth)):
    _validate_view(req.entity, req.filters, req.sort, req.group_by, req.limit_per_group)
    me = auth.identity()
    pos = req.position
    if pos is None:
        row = db.query_one("SELECT MAX(position) AS m FROM views WHERE owner = ?", (me,))
        pos = (row["m"] + 1) if row and row["m"] is not None else 0
    view_id = db.execute(
        "INSERT INTO views (owner, name, entity, filters, sort, group_by, "
        "limit_per_group, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (me, req.name, req.entity, json.dumps(req.filters or {}), req.sort,
         req.group_by, req.limit_per_group, pos))
    return {"status": "created", "view_id": view_id}


def _own_view(view_id: int, auth: AuthContext) -> dict:
    v = db.query_one("SELECT * FROM views WHERE id = ?", (view_id,))
    if not v:
        raise HTTPException(404, f"View {view_id} not found")
    if v["owner"] != auth.identity() and not auth.is_admin():
        raise HTTPException(403, "Not your view")
    return _view_row(v)


@app.patch("/api/v1/views/{view_id}")
def update_view(view_id: int, req: ViewUpdate, auth: AuthContext = Depends(get_auth)):
    v = _own_view(view_id, auth)
    filters = req.filters if req.filters is not None else v["filters"]
    sort = req.sort if req.sort is not None else v["sort"]
    group_by = req.group_by if req.group_by is not None else v["group_by"]
    lpg = req.limit_per_group if req.limit_per_group is not None else v["limit_per_group"]
    _validate_view(v["entity"], filters, sort, group_by, lpg)
    db.execute("UPDATE views SET name = ?, filters = ?, sort = ?, group_by = ?, "
               "limit_per_group = ?, position = ?, updated_at = datetime('now') "
               "WHERE id = ?",
               (req.name if req.name is not None else v["name"],
                json.dumps(filters or {}), sort, group_by, lpg,
                req.position if req.position is not None else v["position"],
                view_id))
    return {"status": "updated", "view_id": view_id}


@app.delete("/api/v1/views/{view_id}")
def delete_view(view_id: int, auth: AuthContext = Depends(get_auth)):
    _own_view(view_id, auth)
    db.execute("DELETE FROM views WHERE id = ?", (view_id,))
    return {"status": "deleted", "view_id": view_id}


@app.get("/api/v1/views/{view_id}/results")
def view_results(view_id: int, limit: int = 100, auth: AuthContext = Depends(get_auth)):
    """Execute a view. Scope predicates always apply; grouping honors
    limit_per_group (the pick-me-menu semantics)."""
    v = _own_view(view_id, auth)
    return _resolve_view(v, auth, cap=max(1, min(limit, 500)))


# ===== Inbox feed (#1082) =====
# The inbox as a feed: one card per ticket, task or doc with activity, newest
# first, carrying the reader's own read and star state (inbox_marks). It reads
# what is already recorded — the objects' updated_at, their latest comment or
# revision, the reader's notification rows — and writes nothing to anyone
# else's inbox: no audience changes, nothing reaches the rail. Paging is by
# card, so one busy thread cannot crowd the others out of a page.

_FEED_KINDS = ("ticket", "task", "doc")
_FEED_FILTERS = ("mine", "unread", "starred", "all")
_FEED_NEVER_READ = "1970-01-01 00:00:00"   # "mark unread" — older than any activity


def _feed_ts(v) -> str:
    """Rows carry 'YYYY-MM-DD HH:MM:SS' or ISO 'T'; compare them as the former."""
    return str(v or "").replace("T", " ")[:19]


def _feed_epoch(me: str) -> str:
    db.execute("INSERT OR IGNORE INTO inbox_state (identity) VALUES (?)", (me,))
    return db.query_one("SELECT epoch FROM inbox_state WHERE identity = ?", (me,))["epoch"]


def _feed_latest(table: str, col: str, ids: list, extra: str = "") -> tuple[dict, dict]:
    """The newest row per object in a comment/revision table, and each object's row count."""
    if not ids:
        return {}, {}
    q = ",".join("?" * len(ids))
    latest = db.query(
        f"SELECT {col} AS oid, author, SUBSTR(body, 1, 280) AS body, created_at{extra} FROM {table} "
        f"WHERE id IN (SELECT MAX(id) FROM {table} WHERE {col} IN ({q}) GROUP BY {col})", tuple(ids))
    counts = db.query(f"SELECT {col} AS oid, COUNT(*) AS n FROM {table} "
                      f"WHERE {col} IN ({q}) GROUP BY {col}", tuple(ids))
    return {r["oid"]: r for r in latest}, {r["oid"]: r["n"] for r in counts}


def _feed_activity(row: dict, comment: Optional[dict], revision: Optional[dict] = None) -> dict:
    """What happened last: a comment, a revision, the creation, or a change."""
    at = _feed_ts(row.get("updated_at"))
    if comment and _feed_ts(comment["created_at"]) >= at:
        return {"what": "commented", "by": comment["author"], "at": comment["created_at"],
                "text": comment["body"]}
    if revision and _feed_ts(revision["created_at"]) >= at:
        what = "created" if revision.get("rev") == 1 else "revised"
        return {"what": what, "by": revision["author"], "at": revision["created_at"],
                "text": revision.get("note") or ""}
    if _feed_ts(row.get("created_at")) >= at:
        return {"what": "created", "by": row.get("source"), "at": row.get("created_at"),
                "text": (row.get("body") or row.get("detail") or "")[:280]}
    # a status, assignee or field change: the record keeps no actor for it
    return {"what": "updated", "by": None, "at": row.get("updated_at"), "text": ""}


def _feed_candidates(auth: AuthContext, kinds, since: str, starred: dict, group_scope: Optional[str]):
    """Visible objects with activity since `since`, plus every starred one."""
    out = []

    def _window(alias, kind):
        ids = starred.get(kind) or []
        clause = f"(datetime({alias}updated_at) >= datetime('now', ?)"
        params = [since]
        if ids:
            clause += f" OR {alias}id IN ({','.join('?' * len(ids))})"
            params += ids
        return clause + ")", params

    if "ticket" in kinds:
        clause, params = _window("", "ticket")
        if group_scope:  # the same union as list_tickets (H2, #380)
            clause += (" AND (group_scope = ? "
                       "OR device_id IN (SELECT device_id FROM group_devices WHERE group_id = ?) "
                       "OR service_id IN (SELECT service_id FROM group_services WHERE group_id = ?))")
            params += [group_scope] * 3
        rows = [r for r in db.query(f"SELECT * FROM tickets WHERE {clause}", tuple(params))
                if _can_see_ticket(auth, r)]
        latest, counts = _feed_latest("ticket_comments", "ticket_id", [r["id"] for r in rows])
        for r in rows:
            out.append({"kind": "ticket", "id": r["id"], "title": r["summary"], "status": r["status"],
                        "priority": r.get("priority"), "href": f"/tickets/{r['id']}",
                        "group_scope": r.get("group_scope"), "assignee": r.get("assignee"),
                        "owner": r.get("source"), "created_at": r["created_at"],
                        "updated_at": r["updated_at"], "comments": counts.get(r["id"], 0),
                        "latest": _feed_activity(r, latest.get(r["id"]))})

    if "task" in kinds:
        clause, params = _window("t.", "task")
        if group_scope:  # tasks ride their project's scope (#330)
            clause += " AND p.group_scope = ?"
            params.append(group_scope)
        rows = db.query("SELECT t.*, p.slug AS project, p.name AS project_name FROM tasks t "
                        f"JOIN projects p ON p.id = t.project_id WHERE {clause}", tuple(params))
        seen: dict = {}

        def _see(pid):
            if pid not in seen:
                proj = db.query_one("SELECT * FROM projects WHERE id = ?", (pid,))
                seen[pid] = bool(proj) and _can_see_project(auth, proj)
            return seen[pid]

        rows = [r for r in rows if _see(r["project_id"])]
        latest, counts = _feed_latest("task_comments", "task_id", [r["id"] for r in rows])
        for r in rows:
            out.append({"kind": "task", "id": r["id"], "title": r["title"], "status": r["status"],
                        "priority": r.get("priority"), "href": f"/tasks/{r['id']}",
                        "project": r["project"], "project_name": r["project_name"],
                        "assignee": r.get("assignee"), "owner": r.get("source"),
                        "created_at": r["created_at"], "updated_at": r["updated_at"],
                        "comments": counts.get(r["id"], 0),
                        "latest": _feed_activity(r, latest.get(r["id"]))})

    if "doc" in kinds:
        clause, params = _window("", "doc")
        if group_scope:
            clause += " AND group_scope = ?"
            params.append(group_scope)
        rows = [r for r in db.query(f"SELECT * FROM docs WHERE {clause}", tuple(params))
                if _can_see_doc(auth, r)]
        ids = [r["id"] for r in rows]
        latest, counts = _feed_latest("doc_comments", "doc_id", ids)
        revs, _ = _feed_latest("doc_revisions", "doc_id", ids, extra=", rev, note")
        for r in rows:
            out.append({"kind": "doc", "id": r["id"], "title": r["title"], "status": r["status"],
                        "doc_kind": r.get("kind"), "href": f"/docs/{r['id']}",
                        "group_scope": r.get("group_scope"),
                        "owner": r.get("owner") or r.get("source"),
                        "created_at": r["created_at"], "updated_at": r["updated_at"],
                        "comments": counts.get(r["id"], 0),
                        "latest": _feed_activity(r, latest.get(r["id"]), revs.get(r["id"]))})
    return out


@app.get("/api/v1/inbox/feed")
def inbox_feed(window_hours: int = 168, kind: Optional[str] = None, filter: str = "mine",
               group_scope: Optional[str] = None, limit: int = 50, offset: int = 0,
               auth: AuthContext = Depends(get_auth)):
    """The inbox feed (#1082): one card per ticket, task or doc, newest
    activity first, built on delivery as it is — nothing here changes who is
    notified.

    `filter=mine` (the default) is what is relevant to you: a card with a
    notification row for you in the window (a mention, an assignment, a reply
    on something you own or follow), or one assigned to you or yours.
    `unread` is the unread among those; `starred` every card you starred,
    whatever its age; `all` every visible card with activity in the window.
    A card is `unread` when its latest activity is newer than when you last
    read it (or your feed's epoch, if you never marked it); your own activity
    never makes it unread. `mention`: a mention of you arrived since you read
    it. `kind` is a comma list of ticket, task, doc; `counts` covers the
    window before `filter`."""
    me = auth.identity()
    window_hours = max(1, min(int(window_hours or 168), 8760))
    kinds = [k.strip() for k in (kind or ",".join(_FEED_KINDS)).split(",") if k.strip()]
    if not kinds or set(kinds) - set(_FEED_KINDS):
        raise HTTPException(400, f"kind must be a comma list of {', '.join(_FEED_KINDS)}")
    if filter not in _FEED_FILTERS:
        raise HTTPException(400, f"filter must be one of {', '.join(_FEED_FILTERS)}")
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    since = f"-{window_hours} hours"

    epoch = _feed_epoch(me)
    marks = {(m["kind"], m["object_id"]): m
             for m in db.query("SELECT * FROM inbox_marks WHERE identity = ?", (me,))}
    starred: dict = {}
    for (k, oid), m in marks.items():
        if m["starred"]:
            starred.setdefault(k, []).append(oid)

    # Your own notification rows: what was addressed to you, and when.
    addressed: dict = {}
    for n in db.query(
            "SELECT ticket_id, task_id, doc_id, event_type, created_at FROM notifications "
            "WHERE recipient = ? AND datetime(created_at) >= datetime('now', ?) "
            "AND (ticket_id IS NOT NULL OR task_id IS NOT NULL OR doc_id IS NOT NULL)",
            (me, since)):
        key = (("ticket", n["ticket_id"]) if n["ticket_id"] else
               ("task", n["task_id"]) if n["task_id"] else ("doc", n["doc_id"]))
        addressed.setdefault(key, []).append(n)

    items = _feed_candidates(auth, kinds, since, starred, group_scope)
    window_start = _feed_ts(db.query_one("SELECT datetime('now', ?) AS t", (since,))["t"])
    for it in items:
        key = (it["kind"], it["id"])
        m = marks.get(key) or {}
        read_at = _feed_ts(m.get("read_at") or epoch)
        latest = it["latest"]
        it["starred"] = bool(m.get("starred"))
        it["read_at"] = m.get("read_at")
        it["unread"] = _feed_ts(it["updated_at"]) > read_at and latest.get("by") != me
        rows = addressed.get(key, [])
        it["mine"] = bool(rows) or me in (it.get("assignee"), it.get("owner"))
        it["mention"] = any(r["event_type"].endswith(".mentioned") and _feed_ts(r["created_at"]) > read_at
                            for r in rows)
    items.sort(key=lambda it: (_feed_ts(it["updated_at"]), it["kind"], it["id"]), reverse=True)

    def fresh(i):  # starred cards are fetched whatever their age; the rest are in the window
        return _feed_ts(i["updated_at"]) >= window_start

    keep = {"mine": lambda i: fresh(i) and i["mine"],
            "unread": lambda i: fresh(i) and i["mine"] and i["unread"],
            "starred": lambda i: i["starred"],
            "all": fresh}
    counts = {f: sum(1 for i in items if keep[f](i)) for f in _FEED_FILTERS}
    items = [i for i in items if keep[filter](i)]
    return {"items": items[offset:offset + limit], "total": len(items), "counts": counts,
            "epoch": epoch, "window_hours": window_hours}


class InboxRef(StrictModel):
    kind: str
    id: int


class InboxMarks(StrictModel):
    items: list[InboxRef] = Field(..., min_length=1, max_length=500)
    read: Optional[bool] = None
    starred: Optional[bool] = None


@app.post("/api/v1/inbox/marks")
def inbox_marks(body: InboxMarks, auth: AuthContext = Depends(get_auth)):
    """Mark feed cards read or unread, starred or not (#1082). Reading a card
    also marks your notification rows for that object read, so the unread
    badge agrees with the feed. Marks are yours alone and reach no one."""
    if body.read is None and body.starred is None:
        raise HTTPException(400, "Say read and/or starred")
    bad = sorted({i.kind for i in body.items} - set(_FEED_KINDS))
    if bad:
        raise HTTPException(400, f"Unknown kind {bad} (have: {', '.join(_FEED_KINDS)})")
    me = auth.identity()
    notifications_read = 0
    for i in body.items:
        db.execute("INSERT OR IGNORE INTO inbox_marks (identity, kind, object_id) VALUES (?, ?, ?)",
                   (me, i.kind, i.id))
        if body.read is True:
            db.execute("UPDATE inbox_marks SET read_at = datetime('now') "
                       "WHERE identity = ? AND kind = ? AND object_id = ?", (me, i.kind, i.id))
            notifications_read += db.execute_count(
                f"UPDATE notifications SET read = 1 WHERE recipient = ? AND {i.kind}_id = ? AND read = 0",
                (me, i.id))
        elif body.read is False:
            db.execute("UPDATE inbox_marks SET read_at = ? "
                       "WHERE identity = ? AND kind = ? AND object_id = ?",
                       (_FEED_NEVER_READ, me, i.kind, i.id))
        if body.starred is not None:
            db.execute("UPDATE inbox_marks SET starred = ? "
                       "WHERE identity = ? AND kind = ? AND object_id = ?",
                       (1 if body.starred else 0, me, i.kind, i.id))
    return {"updated": len(body.items), "notifications_read": notifications_read}


@app.post("/api/v1/inbox/read-all")
def inbox_read_all(auth: AuthContext = Depends(get_auth)):
    """Mark the whole feed read (#1082): the epoch moves to now, per-card read
    marks reset to it (stars stay), and your unread notification rows are
    marked read."""
    me = auth.identity()
    _feed_epoch(me)
    db.execute("UPDATE inbox_state SET epoch = datetime('now') WHERE identity = ?", (me,))
    db.execute("UPDATE inbox_marks SET read_at = NULL WHERE identity = ?", (me,))
    n = db.execute_count("UPDATE notifications SET read = 1 WHERE recipient = ? AND read = 0", (me,))
    return {"epoch": _feed_epoch(me), "notifications_read": n}


# ===== Search across planes (#1082) =====
# One query over tickets, tasks and docs: titles, bodies and comments, with
# one filter vocabulary for all three. Scope applies to every row, as on the
# lists. Plain LIKE over the tables: at today's size (~1.1k tickets, ~1.2k
# tasks, ~0.5k docs and their comments) a scan is milliseconds; FTS waits
# until it is measured to be needed.

_SEARCH_KINDS = ("ticket", "task", "doc")
# "active" is the one cross-plane status: work that is still moving.
_SEARCH_ACTIVE = {"ticket": ("open", "triaged", "in_progress"),
                  "task": ("todo", "in_progress", "blocked"),
                  "doc": ("draft", "active")}
_SEARCH_ID_RE = _re.compile(r"^#?(\d+)$")


def _search_terms(q: str) -> list[str]:
    return [t for t in (q or "").lower().split() if t][:8]


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _search_snippet(text: str, terms: list[str], width: int = 180) -> str:
    """The text around the first term found, on one line."""
    flat = " ".join((text or "").split())
    low = flat.lower()
    at = min((low.find(t) for t in terms if low.find(t) >= 0), default=-1)
    if at < 0:
        return flat[:width]
    start = max(0, at - width // 3)
    return ("…" if start else "") + flat[start:start + width] + ("…" if start + width < len(flat) else "")


def _search_kind(kind: str, terms: list[str], statuses, assignee, group_scope, tag, since):
    """Rows of one kind matching every term (each term in the title, the body
    or any comment) and the filters, before scope."""
    if kind == "ticket":
        base = ("SELECT t.*, t.summary AS title, t.body AS text FROM tickets t", "t", "ticket_comments", "ticket_id")
    elif kind == "task":
        base = ("SELECT t.*, t.detail AS text, p.slug AS project, p.group_scope AS project_scope "
                "FROM tasks t JOIN projects p ON p.id = t.project_id", "t", "task_comments", "task_id")
    else:
        base = ("SELECT t.*, r.body AS text FROM docs t "
                "LEFT JOIN doc_revisions r ON r.doc_id = t.id AND r.rev = t.current_rev",
                "t", "doc_comments", "doc_id")
    sql, a, ctable, ccol = base
    title_col = "t.summary" if kind == "ticket" else "t.title"
    body_col = {"ticket": "t.body", "task": "t.detail", "doc": "r.body"}[kind]
    clauses, params = [], []
    for term in terms:
        like = _like(term)
        m = _SEARCH_ID_RE.match(term)
        one = (f"({title_col} LIKE ? ESCAPE '\\' OR COALESCE({body_col}, '') LIKE ? ESCAPE '\\' "
               f"OR t.id IN (SELECT {ccol} FROM {ctable} WHERE body LIKE ? ESCAPE '\\')")
        params += [like, like, like]
        if kind == "doc":
            one += " OR t.slug LIKE ? ESCAPE '\\'"
            params.append(like)
        if m:
            one += " OR t.id = ?"
            params.append(int(m.group(1)))
        clauses.append(one + ")")
    if statuses:
        clauses.append(f"t.status IN ({','.join('?' * len(statuses))})")
        params += list(statuses)
    if assignee:
        clauses.append("COALESCE(t.owner, t.source) = ?" if kind == "doc" else "t.assignee = ?")
        params.append(assignee)
    if group_scope:
        if kind == "ticket":  # the same union as list_tickets (H2, #380)
            clauses.append("(t.group_scope = ? "
                           "OR t.device_id IN (SELECT device_id FROM group_devices WHERE group_id = ?) "
                           "OR t.service_id IN (SELECT service_id FROM group_services WHERE group_id = ?))")
            params += [group_scope] * 3
        else:
            clauses.append("p.group_scope = ?" if kind == "task" else "t.group_scope = ?")
            params.append(group_scope)
    if tag:
        if kind == "doc":
            return []                               # docs carry no tags
        c, p = _tag_clause(tag)
        clauses.append("t." + c)
        params.append(p)
    if since:
        clauses.append("datetime(t.updated_at) >= datetime('now', ?)")
        params.append(since)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return db.query(f"{sql}{where} ORDER BY t.updated_at DESC LIMIT 2000", tuple(params))


@app.get("/api/v1/search")
def search(q: str = "", kind: Optional[str] = None, status: Optional[str] = None,
           assignee: Optional[str] = None, group_scope: Optional[str] = None,
           tag: Optional[str] = None, updated_hours: Optional[int] = None,
           limit: int = 30, offset: int = 0, auth: AuthContext = Depends(get_auth)):
    """Search across planes (#1082): tickets, tasks and docs you can see whose
    title, body or comments hold every word of `q` (case-insensitive; `#1082`
    or `1082` also matches that id). The filters mean the same on every kind:
    `kind` (comma list of ticket, task, doc), `status` (comma list; `active`
    is open/triaged/in-progress tickets, todo/in-progress/blocked tasks and
    draft/active docs), `assignee` (a doc's owner; `:me` is you),
    `group_scope`, `tag` (tickets and tasks), `updated_hours`. With no `q`
    it is the same filters over everything, newest first. Ranked: an exact
    id, then title matches, then body, then comments; newest first within."""
    me = auth.identity()
    terms = _search_terms(q)
    kinds = [k.strip() for k in (kind or ",".join(_SEARCH_KINDS)).split(",") if k.strip()]
    if not kinds or set(kinds) - set(_SEARCH_KINDS):
        raise HTTPException(400, f"kind must be a comma list of {', '.join(_SEARCH_KINDS)}")
    wanted = [s.strip() for s in (status or "").split(",") if s.strip()]
    if assignee == ":me":
        assignee = me
    if updated_hours is not None and not 1 <= int(updated_hours) <= 87600:
        raise HTTPException(400, "updated_hours must be 1-87600")
    since = f"-{int(updated_hours)} hours" if updated_hours else None
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    seen_proj: dict = {}

    def _see_task(r):
        pid = r["project_id"]
        if pid not in seen_proj:
            proj = db.query_one("SELECT * FROM projects WHERE id = ?", (pid,))
            seen_proj[pid] = bool(proj) and _can_see_project(auth, proj)
        return seen_proj[pid]

    visible = {"ticket": lambda r: _can_see_ticket(auth, r), "task": _see_task,
               "doc": lambda r: _can_see_doc(auth, r)}
    path = {"ticket": "tickets", "task": "tasks", "doc": "docs"}
    results = []
    for k in kinds:
        statuses = []
        for s in wanted:
            statuses += list(_SEARCH_ACTIVE[k]) if s == "active" else [s]
        rows = [r for r in _search_kind(k, terms, statuses, assignee, group_scope, tag, since)
                if visible[k](r)]
        if not rows:
            continue
        # which matched where, for the rank and the snippet
        hits_by_id: dict = {}
        if terms:
            q_ids = ",".join("?" * len(rows))
            ctable, ccol = {"ticket": ("ticket_comments", "ticket_id"), "task": ("task_comments", "task_id"),
                            "doc": ("doc_comments", "doc_id")}[k]
            for c in db.query(
                    f"SELECT {ccol} AS oid, id, author, body, created_at FROM {ctable} "
                    f"WHERE {ccol} IN ({q_ids}) AND body LIKE ? ESCAPE '\\' ORDER BY id DESC",
                    tuple(r["id"] for r in rows) + (_like(terms[0]),)):
                hits_by_id.setdefault(c["oid"], c)
        for r in rows:
            title, text = r.get("title") or "", r.get("text") or ""
            low_t, low_b = title.lower(), text.lower()
            exact = any((m := _SEARCH_ID_RE.match(t)) and int(m.group(1)) == r["id"] for t in terms)
            in_title = bool(terms) and all(t in low_t for t in terms)
            in_body = bool(terms) and any(t in low_b for t in terms)
            comment = hits_by_id.get(r["id"])
            if exact:
                rank, where, snippet = 4, "id", _search_snippet(text, terms)
            elif in_title:
                rank, where, snippet = 3, "title", _search_snippet(text, terms)
            elif in_body or not comment:
                rank, where, snippet = (2 if in_body else 1), "body", _search_snippet(text, terms)
            else:
                rank, where = 1, "comment"
                snippet = f"{comment['author']}: " + _search_snippet(comment["body"], terms)
            results.append({
                "kind": k, "id": r["id"], "title": title, "status": r.get("status"),
                "href": f"/{path[k]}/{r['id']}", "ref": f"[{title}](/{path[k]}/{r['id']})",
                "assignee": r.get("assignee") if k != "doc" else (r.get("owner") or r.get("source")),
                "project": r.get("project"), "group_scope": r.get("project_scope") if k == "task" else r.get("group_scope"),
                "updated_at": r.get("updated_at"), "match": where if terms else None,
                "snippet": snippet, "_rank": rank if terms else 0,
            })
    results.sort(key=lambda x: (x["_rank"], _feed_ts(x["updated_at"])), reverse=True)
    counts = {k: sum(1 for x in results if x["kind"] == k) for k in kinds}
    page = results[offset:offset + limit]
    for x in page:
        x.pop("_rank", None)
    return {"q": q, "terms": terms, "items": page, "total": len(results), "counts": counts}


@app.get("/api/v1/users/me/briefing")
def my_briefing(window_hours: int = 24, auth: AuthContext = Depends(get_auth)):
    """One-call personal briefing (#203 — one person's inbox, but identity-generic):
    unread inbox rows, open items blocked on YOU, standing DECISION tasks,
    a small low-effort pick-me menu, and windowed movement. Powers the console
    /inbox page + nav badge. Net-negative-effort by design (the
    personal-assistant lesson): everything here is read-or-act, never homework
    the surface invents for you. Ack = handled, surface "console".

    How sections are sourced (#203 direction: 'only ever from data'):
    blocked_on_you = open tickets assigned to you; decisions = tasks TAGGED
    'decision' (first-class tags since v0.7.44 — the title convention is
    retired); menu = smallest-estimate todo per project; moved =
    ?window_hours (1-720). These fixed sections are themselves interim: the
    end state is user-defined saved views (Asana-style), spec in doc #34.
    """
    me = auth.identity()
    window_hours = max(1, min(int(window_hours or 24), 720))
    unread_total = db.query_one(
        "SELECT COUNT(*) AS c FROM notifications WHERE recipient = ? AND read = 0",
        (me,))["c"]
    inbox = db.query(
        "SELECT id, event_type, ticket_id, doc_id, task_id, actor, payload, "
        "seen_until, created_at FROM notifications "
        "WHERE recipient = ? AND read = 0 ORDER BY id DESC LIMIT 50", (me,))
    for n in inbox:
        try:
            n["payload"] = json.loads(n["payload"]) if n.get("payload") else {}
        except (ValueError, TypeError):
            n["payload"] = {}
    blocked = db.query(
        "SELECT id, summary, priority, type, status, source, created_at, "
        "SUBSTR(body, 1, 240) AS body "
        "FROM tickets WHERE assignee = ? AND status IN "
        "('open','triaged','in_progress') "
        "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'med' THEN 2 WHEN 'medium' THEN 2 ELSE 3 END, id DESC LIMIT 25",
        (me,))
    # DECISION convention: a task titled 'DECISION: ...' is a call only a
    # human can make; it surfaces here until decided (status done/cancelled).
    proj_cache: dict = {}

    def _see_project_id(pid):
        if pid not in proj_cache:
            p = db.query_one("SELECT * FROM projects WHERE id = ?", (pid,))
            proj_cache[pid] = bool(p) and _can_see_project(auth, p)
        return proj_cache[pid]

    decisions = [d for d in db.query(
        "SELECT t.id AS task_id, t.title, t.status, t.priority, t.project_id, "
        "SUBSTR(t.detail, 1, 240) AS detail, "
        "p.slug AS project FROM tasks t JOIN projects p ON p.id = t.project_id "
        "WHERE t.tags LIKE '%\"decision\"%' AND t.status NOT IN ('done','cancelled') "
        "ORDER BY t.id DESC LIMIT 20") if _see_project_id(d["project_id"])]
    # Menu: concrete, pick-up-able — up to one todo per project, smallest
    # estimate first. A suggestion shelf, not a nag.
    menu, seen_proj = [], set()
    for r in db.query(
            "SELECT t.id AS task_id, t.title, t.status, t.priority, "
            "t.estimate_min, t.project_id, SUBSTR(t.detail, 1, 240) AS detail, "
            "p.slug AS project FROM tasks t "
            "JOIN projects p ON p.id = t.project_id "
            "WHERE t.status = 'todo' AND t.title NOT LIKE 'DECISION%' "
            "ORDER BY COALESCE(t.estimate_min, 999999), t.id DESC LIMIT 80"):
        if r["project_id"] in seen_proj or not _see_project_id(r["project_id"]):
            continue
        seen_proj.add(r["project_id"])
        menu.append(r)
        if len(menu) >= 5:
            break
    win = f"-{window_hours} hours"
    moved = {k: db.query_one(
        f"SELECT COUNT(*) AS c FROM {t} WHERE updated_at >= datetime('now', ?)",
        (win,))["c"]
        for k, t in (("tickets", "tickets"), ("docs", "docs"), ("tasks", "tasks"))}
    return {"identity": me, "unread_total": unread_total, "inbox": inbox,
            "blocked_on_you": blocked, "decisions": decisions, "menu": menu,
            "moved_24h": moved, "window_hours": window_hours,
            "ack_hint": ("POST /api/v1/notifications/ack "
                         '{"ids":[...], "surface":"console"} — ack = handled')}


@app.get("/api/v1/users/me/guide")
def user_guide(auth: AuthContext = Depends(get_auth)):
    """Persistent operations guide for the authenticated user.

    Always available. Shows scope, capabilities, service management instructions.
    """
    # Look up user from auth
    owner_id = auth.owner_id  # e.g. "user:123"
    if not owner_id.startswith("user:"):
        raise HTTPException(400, "This endpoint is for user tokens")

    user_id = int(owner_id.split(":")[1])
    user = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(404, "User not found")

    scopes = json.loads(user.get("scopes", "[]"))
    public_url = os.environ.get("PUBLIC_URL", "http://localhost:7778")

    # Scope v1 (doc 261): who you are, who you are working with, the group you
    # are in — from membership and roles, the store the API applies, not the
    # token's scope string (codex, doc 260 §4: the guide once counted 18
    # services where /services answered 25).
    mine = groupscope.memberships(auth)
    scope_lines = []
    if mine is None:
        scope_lines.append("- **Admin** — owner of every group; your unnamed writes go to `samcloud-contributors`.")
        mine = {}
    for gid, role in mine.items():
        g = db.query_one("SELECT * FROM groups WHERE id = ?", (gid,)) or {}
        members = db.query("SELECT u.username, gm.role FROM group_members gm JOIN users u ON u.id = gm.user_id "
                           "WHERE gm.group_id = ? ORDER BY u.username", (gid,))
        who = ", ".join(f"{m['username']} ({m['role']})" for m in members if m["username"] != user["username"]) or "nobody else yet"
        purpose = f" — {g['description']}" if g.get("description") else ""
        wd = ""
        if g.get("doc_id"):
            drow = db.query_one("SELECT id, title FROM docs WHERE id = ?", (g["doc_id"],))
            if drow:
                wd = f"\n  - working doc: [{drow['title']}]({public_url}/docs/{drow['id']}) (`GET /api/v1/docs/{drow['id']}`)"
        can = {"viewer": "read, answer on threads, use its services",
               "editor": "file tickets, write docs, add and change tasks, register devices and routes, assign and mention members",
               "owner": "everything an editor can, and add or remove members and set their role"}.get(role, "read")
        scope_lines.append(f"- **{gid}** — you are a **{role}**{purpose}\n  - you can: {can}\n  - with: {who}{wd}")
    for scope in scopes:
        if scope.startswith("device:"):
            scope_lines.append(f"- Device: `{scope.split(':', 1)[1]}` — yours to manage")
    default = groupscope.default_group(auth)
    if default:
        scope_lines.append(f"- A write that names no `group_scope` goes to **{default}** — the group you were added to first "
                           f"among those you can edit (`GET /api/v1/auth/verify` names it as `default_group`). A ticket with an "
                           f"assignee goes to the first group you edit that they are in; an admin is a member of every group, "
                           f"so a ticket filed to the admin stays in **{default}**."),

    # Services you can use: every service on a device you can see — the same
    # answer as GET /services.
    visible = _visible_device_ids(auth)
    svc_rows = db.query("SELECT * FROM services ORDER BY device_id, name")
    services_in_scope = [_deserialize_service(x) for x in svc_rows
                         if visible is None or x.get("device_id") in visible]

    svc_table = "\n".join(
        f"| `{x['id']}` | `{x['status']}` | `{x.get('port', '-')}` | `{x.get('external_url', '-')}` |"
        for x in services_in_scope
    ) if services_in_scope else "| (none) | | | |"

    _op = _operator_now()
    guide = f"""# {user['username']} — your samcloud guide

You are **{user['username']}** (role: {user['role']}). The operator's clock reads
**{_op['human'] or '?'} ({_op['tz']})**; read "morning", "tonight" and "EOD" in that clock.

Your identity can run several conversations, each with its own history. A comment's
`session_fp` identifies its writer's conversation; `home_thread_id` names its birth
or first-create thread, not the object it writes on. A sibling's comment is not
your own. Unknown provenance stays null. `sc-say` stamps your writes.

## Your groups — the group is the scope

{chr(10).join(scope_lines) if scope_lines else "- You are in no group yet: you can read nothing and create nothing until an owner adds you (`POST /api/v1/groups/<id>/members`)."}

What you see is the union of your groups; an object outside them answers 404. `GET {public_url}/api/v1/groups/<id>` is the roster.

## Services you can use

| Service | Status | Port | External URL |
|---------|--------|------|-------------|
{svc_table}

Each has its own page: `GET {public_url}/api/v1/services/<device>/<service>/guide`.

## How you work here

Everything is in the served skill, which keeps itself current on your seat:

```
curl -s {public_url}/api/v1/skills/install.sh | bash        # installs into ~/.claude/skills
curl -s {public_url}/api/v1/skills/samcloud-agent/SKILL.md   # read it now
```

Auth is a 0600 header file, never the token on a command line:

```
curl -s -H @"$HOME/.samcloud/token.hdr" {public_url}/api/v1/auth/verify
```

Threads you are on reach you through the rail; install it once on this seat:

```
curl -s -H @"$HOME/.samcloud/token.hdr" {public_url}/api/v1/helpers/wake-install | bash
```

Reply on a thread with `sc-say` (`echo "…" | sc-say ticket 698`); the skill's `tools.md`
installs it. The API index is `curl -s {public_url}/api/v1/`.

## Managing a device or a service

```bash
H="$HOME/.samcloud/token.hdr"

# enroll a device into your group
curl -s -X POST "{public_url}/api/v1/enroll" -H @"$H" -H "Content-Type: application/json" \\
  -d '{{"device_id": "my-device", "name": "My Device", "type": "container"}}'

# register a service on a device in your scope
curl -s -X POST "{public_url}/api/v1/services" -H @"$H" -H "Content-Type: application/json" \\
  -d '{{"name": "service-name", "device_id": "<device-in-scope>", "port": 8080, "health_endpoint": "/health", "subdomain": "name"}}'

# update, report health, delete
curl -s -X PATCH  "{public_url}/api/v1/services/<device>/<service>" -H @"$H" -H "Content-Type: application/json" -d '{{"status": "online"}}'
curl -s -X POST   "{public_url}/api/v1/services/<device>/<service>/health" -H @"$H"
curl -s -X DELETE "{public_url}/api/v1/services/<device>/<service>" -H @"$H"

# invite an agent into your group (a role up to your own)
curl -s -X POST "{public_url}/api/v1/users/invite" -H @"$H" -H "Content-Type: application/json" \\
  -d '{{"username": "new-agent", "role": "agent", "scopes": ["group:work"]}}'

# a service verifying a caller's token
curl -s "{public_url}/api/v1/auth/verify?scope=device:my-device" -H @<(printf 'Authorization: Bearer %s\\n' "$CALLER_TOKEN")
```
"""

    return PlainTextResponse(guide, media_type="text/markdown")


# --- Resources (Compute) ---

VALID_RESOURCE_TYPES = {"gpu", "cpu", "memory", "storage", "accelerator"}
VALID_RESOURCE_STATUSES = {"online", "offline", "degraded", "reserved", "unknown"}
RESOURCE_HEALTH_THRESHOLDS = {"green": 60, "amber": 85}  # percentage


def _normalise_resource(res: dict) -> dict:
    """Add normalised stats for dashboard display."""
    if res and isinstance(res.get("specs"), str):
        try:
            res["specs"] = json.loads(res["specs"])
        except (json.JSONDecodeError, TypeError):
            pass
    if res and isinstance(res.get("utilisation"), str):
        try:
            res["utilisation"] = json.loads(res["utilisation"])
        except (json.JSONDecodeError, TypeError):
            pass

    # Compute normalised health
    util = res.get("utilisation") or {}
    mem_pct = util.get("memory_pct", 0)
    comp_pct = util.get("compute_pct", 0)
    peak = max(mem_pct, comp_pct)

    if not res.get("last_stats"):
        health = "grey"
    elif peak > RESOURCE_HEALTH_THRESHOLDS["amber"]:
        health = "red"
    elif peak > RESOURCE_HEALTH_THRESHOLDS["green"]:
        health = "amber"
    else:
        health = "green"

    # Check staleness
    if res.get("last_stats"):
        from datetime import datetime, timezone
        try:
            last = datetime.fromisoformat(res["last_stats"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - last).total_seconds()
            if age > 60:
                health = "grey"
        except (ValueError, TypeError):
            pass

    res["normalised"] = {
        "memory_pct": mem_pct,
        "compute_pct": comp_pct,
        "health": health,
    }
    return res


@app.post("/api/v1/resources")
def register_resource(req: ResourceRegister, auth: AuthContext = Depends(get_auth)):
    """Register a compute resource on a device."""
    if req.type not in VALID_RESOURCE_TYPES:
        raise HTTPException(422, f"Invalid type '{req.type}'. Must be one of: {', '.join(sorted(VALID_RESOURCE_TYPES))}")

    _validate_service_name(req.name)

    device = _require_managed_device(auth, req.device_id)

    resource_id = f"{req.device_id}/{req.name}"
    existing = db.query_one("SELECT id FROM resources WHERE id = ?", (resource_id,))
    if existing:
        raise HTTPException(409, f"Resource {resource_id} already exists")

    now = datetime.now(timezone.utc).isoformat()
    db.execute("""
        INSERT INTO resources (id, device_id, name, type, provider, model, allocation_mode,
            specs, status, created_by, registered_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?)
    """, (resource_id, req.device_id, req.name, req.type, req.provider, req.model,
          req.allocation_mode, json.dumps(req.specs) if req.specs else None,
          auth.owner_id, now, now))

    db.audit(auth.owner_id, "resource.register", resource_id,
             {"type": req.type, "provider": req.provider, "model": req.model})

    event_bus.emit("resource.registered", resource_id,
                   {"resource_id": resource_id, "type": req.type, "provider": req.provider,
                    "model": req.model},
                   scope=f"device:{req.device_id}")

    return {"resource_id": resource_id, "status": "online"}


@app.get("/api/v1/resources")
def list_resources(device_id: str = None, type: str = None, provider: str = None,
                   available: bool = None, min_memory_mb: int = None,
                   auth: Optional[AuthContext] = Depends(get_auth_optional)):
    """List resources with optional filters."""
    clauses, params = [], []
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if type:
        clauses.append("type = ?")
        params.append(type)
    if provider:
        clauses.append("provider = ?")
        params.append(provider)

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    resources = db.query(f"SELECT * FROM resources{where} ORDER BY device_id, name", tuple(params))
    # Scope filtering (fail closed): admin sees all; scoped users see only
    # resources on visible devices; an unauthenticated caller sees nothing.
    visible = _visible_device_ids(auth) if auth is not None else set()
    if visible is not None:
        resources = [r for r in resources if r["device_id"] in visible]
    result = [_normalise_resource(dict(r)) for r in resources]

    # Post-query filters
    if min_memory_mb is not None:
        result = [r for r in result if _get_available_memory(r) >= min_memory_mb]
    if available is not None:
        result = [r for r in result if (r.get("normalised", {}).get("health") != "grey") == available]

    return result


# An exclusive lease with no duration used to be granted with `expires_at` NULL.
# "null = indefinite" is honest for a memory reservation and catastrophic for a
# mutex: 2026-09-25 00:05Z, lease_2c50d13a01b120ac on
# claude-services/gpu-pool — exclusive, no expiry, so nothing could ever
# expire it. Had its holder died, the pool would have been closed to every caller
# until a person released it by hand, which is an unbounded outage rather than the
# bounded one #827 was filed about. A shared lease keeps "indefinite"; an
# exclusive one gets a TTL whether or not its caller thought to ask.
#
# 120s is short on purpose: it is the window a DEAD holder costs, and the holder
# is expected to renew (#827 P3) rather than to have asked for longer. The absolute
# ceiling on renewals is LEASE_MAX_TOTAL_S, just below.
# NAME IT CAREFULLY: this is the TTL applied when an exclusive lease asks for
# NONE. It is not "the default TTL of every exclusive lease" — that is stage (b)
# of #827 P3, which drops what callers who DO name a TTL get, and must not land
# before every holder renews. Measured on the gateway across four days to
# 2026-09-27: 35 of 514 pool leases ran over 120s (15 of 92 on the 27th alone,
# max 1500.3s), and every one of them names its own TTL, so this change touches
# none of them.
#
# The one behaviour it does change: a caller that took an exclusive lease with no
# expiry and worked for longer than 120s used to keep it forever and now loses it.
# That is the trade — an unbounded outage for a bounded interruption — and the
# `lease.ttl_defaulted` audit rows are how such a caller gets found rather than
# guessed at.
EXCLUSIVE_DEFAULT_TTL_S = 120

# The absolute limit on a lease, counted from `granted_at` and NOT extendable by
# renewing. Renewal without a ceiling turns "bounded by TTL" into "unbounded while
# the process lives", and a live-but-stalled holder is a measured shape rather than
# a hypothetical: 2026-09-27 14:17, a gateway held the pool for the full 1500s while
# its generation was wedged, and would have answered every heartbeat (#827 P3a).
#
# CONSTRAINT: at or above the gateway's EXO_GENERATE_TIMEOUT (1500s) plus margin. A
# real request ran 1500.3s on 2026-09-27 — one of 514 pool leases measured over
# four days, not imagined — so 1800 leaves 300s. If this has to come down,
# EXO_GENERATE_TIMEOUT comes down first, or a legitimate long generation loses its
# lease mid-answer.
LEASE_MAX_TOTAL_S = 1800

# And a hard limit on what a caller may ask for. Without one, `max_total_s` undoes
# the ceiling it implements: any caller could send 10**9 and renew forever, which is
# P3a's "a live-but-stalled holder cannot hold the resource indefinitely" defeated by
# one field. 21600 (6h) covers the longest legitimate hold we have — a 5400s turn —
# with room, and is clamped rather than refused so a caller asking for too much still
# works, audited as `lease.max_total_clamped` naming it.
LEASE_MAX_TOTAL_HARD_S = 21600


def _publish_lease_moves(moves: dict) -> None:
    """After commit: the waiters a reap or a release just granted, and the waits
    that ran out. Readers of the event stream see a promotion the moment the
    row is active, never before."""
    for row in moves.get("promoted") or []:
        db.audit(row.get("agent_id") or "registry", "lease.granted", row["id"],
                 {"resource_id": row["resource_id"], "service_id": row.get("service_id"),
                  "memory_mb": row.get("memory_mb"), "promoted": True,
                  "waited_s": row.get("waited_s")})
        # The same kind a direct grant publishes — a consumer waiting for its grant
        # listens for one thing — with `promoted` and `waited_s` saying it queued.
        event_bus.emit("lease.granted", row["id"],
                       {"lease_id": row["id"], "resource_id": row["resource_id"],
                        "service_id": row.get("service_id"), "memory_mb": row.get("memory_mb"),
                        "exclusive": bool(row.get("exclusive")), "expires_at": row.get("expires_at"),
                        "promoted": True, "waited_s": row.get("waited_s")})
    for row in moves.get("wait_expired") or []:
        event_bus.emit("lease.wait_expired", row["id"],
                       {"lease_id": row["id"], "resource_id": row["resource_id"],
                        "service_id": row.get("service_id")})
    for row in moves.get("expired") or []:
        # The registry gave up on a holder that stopped renewing. Whether its
        # processes are gone is not known, and the event says so rather than
        # reading like a release. On a `free` resource its bytes are free in the
        # ledger from here; on a `hold` resource they stay charged until settled.
        event_bus.emit("lease.expired", row["id"],
                       {"lease_id": row["id"], "resource_id": row["resource_id"],
                        "service_id": row.get("service_id"), "memory_mb": row.get("memory_mb"),
                        "exclusive": bool(row.get("exclusive")), "reason": "ttl_expired",
                        "note": "the holder stopped renewing; not proof its processes exited"})
    for row in moves.get("invalid") or []:
        # A queued row whose dimensions could not be trusted left the queue without
        # ever touching the ledger (codex, PR 100 review).
        event_bus.emit("lease.expired", row["id"],
                       {"lease_id": row["id"], "resource_id": row["resource_id"],
                        "service_id": row.get("service_id"), "memory_mb": row.get("memory_mb"),
                        "reason": "invalid_claim",
                        "note": "a queued claim with an untrusted size or TTL; never granted"})


def _expire_stale_leases():
    """Expire leases past their expires_at and waits past their deadline, then
    grant the waiters that now fit (0.12.98). Called on resource reads, so time
    passing is enough for the head of a queue to be promoted."""
    _publish_lease_moves(reap_leases(db, default_ttl=EXCLUSIVE_DEFAULT_TTL_S))


def _get_available_memory(resource: dict) -> int:
    """Get available memory for a resource (total - active leases)."""
    _expire_stale_leases()
    specs = resource.get("specs") or {}
    total = (
        specs.get("vram_mb")
        or specs.get("gpu_memory_mb")
        or specs.get("unified_memory_mb")
        or specs.get("ram_mb")
        or 0
    )

    # On a `hold` resource an expired holder nobody has settled still charges the
    # ledger: the registry cannot see that its memory is free (task 868).
    charged = "status = 'active'"
    if (resource.get("expiry_policy") or "free") == "hold":
        charged = "(status = 'active' OR (" + LEASE_UNSETTLED + "))"
    active_leases = db.query(
        f"SELECT COALESCE(SUM(memory_mb), 0) as leased FROM leases WHERE resource_id = ? AND {charged}",
        (resource["id"],),
    )
    leased = active_leases[0]["leased"] if active_leases else 0
    return total - leased


@app.get("/api/v1/resources/dashboard")
def resources_dashboard(auth: Optional[AuthContext] = Depends(get_auth_optional)):
    """Normalised at-a-glance view for resources (scope-enforced, fail closed).

    Admin sees all; scoped users see only resources on their visible devices;
    an unauthenticated caller sees an empty list — never the whole fleet's GPUs.
    """
    resources = db.query("SELECT * FROM resources ORDER BY device_id, name")
    visible = _visible_device_ids(auth) if auth is not None else set()
    if visible is not None:
        resources = [r for r in resources if r["device_id"] in visible]
    result = []
    for r in resources:
        r = _normalise_resource(dict(r))
        specs = r.get("specs") or {}
        util = r.get("utilisation") or {}
        norm = r.get("normalised", {})

        # Count active leases, and the waiters queued behind them (0.12.98)
        lease_count = db.query_one(
            "SELECT COUNT(*) as n FROM leases WHERE resource_id = ? AND status = 'active'",
            (r["id"],),
        )
        queued_count = db.query_one(
            "SELECT COUNT(*) as n FROM leases WHERE resource_id = ? AND status = 'queued'",
            (r["id"],),
        )

        # Stale check for display
        last_stats_display = "never"
        if r.get("last_stats"):
            try:
                last = datetime.fromisoformat(r["last_stats"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                age = int((datetime.now(timezone.utc) - last).total_seconds())
                if age < 10:
                    last_stats_display = f"{age}s ago"
                elif age < 60:
                    last_stats_display = f"{age}s ago"
                elif age < 3600:
                    last_stats_display = f"{age // 60}m ago"
                else:
                    last_stats_display = f"{age // 3600}h ago"
            except (ValueError, TypeError):
                pass

        result.append({
            "id": r["id"],
            "label": f"{r.get('model', r['name'])}",
            "device": r["device_id"],
            "type": r["type"],
            "provider": r["provider"],
            "health": norm.get("health", "grey"),
            "memory_pct": norm.get("memory_pct", 0),
            "compute_pct": norm.get("compute_pct", 0),
            "temperature_c": util.get("temperature_c"),
            "power_draw_w": util.get("power_draw_w"),
            "leases": lease_count["n"] if lease_count else 0,
            "queued": queued_count["n"] if queued_count else 0,
            "available_memory_mb": _get_available_memory(r),
            "last_stats": last_stats_display,
        })
    return result


@app.get("/api/v1/resources/{resource_id:path}")
def get_resource(resource_id: str, auth: AuthContext = Depends(get_auth)):
    """Get full resource detail including leases — hidden (404) outside the caller's scope."""
    res = _require_visible_resource(auth, resource_id)
    res = _normalise_resource(dict(res))
    # Reap BEFORE reporting. This used to read the lease rows and then call
    # _get_available_memory(), which is the only thing that reaps — so the
    # response carried status "active" for a lease the same request was about to
    # expire. A lease-aware guard reads that array to decide whether a pool is
    # leased, and fails closed on it: one stale "active" suppresses a wedge
    # recovery for a pass (#827).
    _expire_stale_leases()
    res["leases"] = db.query(
        "SELECT * FROM leases WHERE resource_id = ? AND status IN ('active', 'queued') ORDER BY created_at",
        (resource_id,),
    )
    # The queue, in the order it will be granted (0.12.98): who waits behind the
    # holders, for what, until when. A holder reads this to know it is wanted.
    res["queue"] = lease_queue_view(res["leases"])
    # Expired holders nobody has settled (task 868): on a `hold` resource these still
    # charge the ledger and block the queue until their holder or an admin DELETEs
    # them, so the view names them; on a `free` resource they charge nothing and the
    # list is empty.
    res["expiry_policy"] = res.get("expiry_policy") or "free"
    res["unsettled"] = lease_unsettled_view(db.query(
        f"SELECT * FROM leases WHERE resource_id = ? AND {LEASE_UNSETTLED} ORDER BY expires_at",
        (resource_id,))) if res["expiry_policy"] == "hold" else []
    res["available_memory_mb"] = _get_available_memory(res)
    return res


@app.post("/api/v1/resources/{resource_id:path}/stats")
def push_resource_stats(resource_id: str, req: ResourceStats, auth: AuthContext = Depends(get_auth)):
    """Push live utilisation stats for a resource — its device's manager."""
    res = _require_visible_resource(auth, resource_id, manage=True)

    # Build utilisation JSON
    util = {}
    if req.memory_used_mb is not None:
        util["memory_used_mb"] = req.memory_used_mb
    if req.memory_total_mb is not None:
        util["memory_total_mb"] = req.memory_total_mb
    if req.memory_used_mb is not None and req.memory_total_mb:
        util["memory_pct"] = round(req.memory_used_mb / req.memory_total_mb * 100, 1)
    if req.compute_pct is not None:
        util["compute_pct"] = req.compute_pct
    if req.temperature_c is not None:
        util["temperature_c"] = req.temperature_c
    if req.power_draw_w is not None:
        util["power_draw_w"] = req.power_draw_w
    if req.processes is not None:
        util["processes"] = req.processes

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE resources SET utilisation = ?, last_stats = ?, status = 'online', updated_at = ? WHERE id = ?",
        (json.dumps(util), now, now, resource_id),
    )

    # Compute normalised health for response
    mem_pct = util.get("memory_pct", 0)
    comp_pct = util.get("compute_pct", 0)
    peak = max(mem_pct, comp_pct)
    if peak > 85:
        health = "red"
    elif peak > 60:
        health = "amber"
    else:
        health = "green"

    event_bus.emit("resource.stats", resource_id,
                   {"resource_id": resource_id, "memory_pct": mem_pct,
                    "compute_pct": comp_pct, "health": health},
                   scope=f"device:{res['device_id']}")

    return {
        "status": "ok",
        "resource_id": resource_id,
        "normalised": {"memory_pct": mem_pct, "compute_pct": comp_pct, "health": health},
        "last_stats": now,
    }


@app.patch("/api/v1/resources/{resource_id:path}")
def update_resource(resource_id: str, req: ResourceUpdate, auth: AuthContext = Depends(get_auth)):
    """Update resource metadata — its device's manager."""
    res = _require_visible_resource(auth, resource_id, manage=True)

    if req.status is not None and req.status not in VALID_RESOURCE_STATUSES:
        raise HTTPException(422, f"Invalid status. Must be one of: {', '.join(sorted(VALID_RESOURCE_STATUSES))}")

    if req.expiry_policy is not None and req.expiry_policy not in ("free", "hold"):
        raise HTTPException(422, "expiry_policy is 'free' (an expired holder stops charging at once) "
                                 "or 'hold' (it charges until its holder or an admin settles it)")

    updates = {}
    if req.status is not None:
        updates["status"] = req.status
    if req.specs is not None:
        updates["specs"] = json.dumps(req.specs)
    if req.model is not None:
        updates["model"] = req.model
    if req.expiry_policy is not None:
        updates["expiry_policy"] = req.expiry_policy

    if updates:
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [resource_id]
        db.execute(f"UPDATE resources SET {set_clause} WHERE id = ?", tuple(values))

    return _normalise_resource(dict(db.query_one("SELECT * FROM resources WHERE id = ?", (resource_id,))))


@app.delete("/api/v1/resources/{resource_id:path}")
def delete_resource(resource_id: str, auth: AuthContext = Depends(get_auth)):
    """Delete a resource. Requires admin or ownership."""
    res = db.query_one("SELECT * FROM resources WHERE id = ?", (resource_id,))
    if not res:
        raise HTTPException(404, f"Resource {resource_id} not found")
    if not auth.is_admin() and not auth.owns(resource_id):
        raise HTTPException(403, "Access denied")

    with db.transaction() as conn:
        conn.execute("DELETE FROM leases WHERE resource_id = ?", (resource_id,))
        conn.execute("DELETE FROM resources WHERE id = ?", (resource_id,))

    db.audit(auth.owner_id, "resource.deleted", resource_id)
    event_bus.emit("resource.deleted", resource_id, {"resource_id": resource_id},
                   scope=f"device:{res['device_id']}")
    return {"status": "deleted", "resource_id": resource_id}


# --- Leases ---

def _can_see_lease_group(auth, group):
    if auth.is_admin() or group['owner_id'] == auth.owner_id:
        return True
    visible = _visible_device_ids(auth)
    members = db.query('SELECT resource_id FROM lease_group_members WHERE group_id=?',
                       (group['id'],))
    for member in members:
        resource = db.query_one('SELECT device_id FROM resources WHERE id=?',
                                (member['resource_id'],))
        if resource is None or (visible is not None and resource['device_id'] not in visible):
            return False
    return bool(members)


def _require_lease_group(auth, group_id, *, mutate=False, acquire=False):
    group = db.query_one('SELECT * FROM lease_groups WHERE id=?', (group_id,))
    if group is None or not _can_see_lease_group(auth, group):
        raise HTTPException(404, 'Lease group not found')
    if mutate and not auth.is_admin() and group['owner_id'] != auth.owner_id:
        raise HTTPException(403, 'A lease group is changed by its owner')
    if acquire:
        for member in db.query('SELECT resource_id FROM lease_group_members WHERE group_id=?',
                                (group_id,)):
            _require_visible_resource(auth, member['resource_id'])
    return group


def _publish_lease_group(result):
    """Only called after the entire group/member transaction has committed."""
    body, effects = result
    for moves in effects.moves:
        _publish_lease_moves(moves)
    for resource_id, req, lease, metadata in effects.reservations:
        _publish_lease_reservation(resource_id, req, lease, metadata, effects.actor)
    for lease, status in effects.releases:
        kind = {'cancelled': 'lease.cancelled', 'settled': 'lease.settled'}.get(
            status, 'lease.released')
        payload = {'lease_id': lease['id'], 'resource_id': lease['resource_id']}
        db.audit(effects.actor, kind, lease['id'], payload)
        event_bus.emit(kind, lease['id'], payload)
    for event in effects.events:
        payload = {**event['data'], 'sequence': event['sequence']}
        db.audit(effects.actor, event['kind'], event['group_id'], payload)
        event_bus.emit(event['kind'], event['group_id'], payload, scope=event['owner_id'])
    return body


@app.post('/api/v1/lease-groups')
def create_lease_group(req: LeaseGroupRequest, auth: AuthContext = Depends(get_auth)):
    for member in req.members:
        _require_visible_resource(auth, member.resource_id)
    return _publish_lease_group(lease_groups.create(
        db, req.model_dump(), auth.owner_id, default_ttl=EXCLUSIVE_DEFAULT_TTL_S,
        total_floor=LEASE_MAX_TOTAL_S, total_hard=LEASE_MAX_TOTAL_HARD_S))


@app.get('/api/v1/lease-groups/{group_id}')
def get_lease_group(group_id: str, auth: AuthContext = Depends(get_auth)):
    _require_lease_group(auth, group_id)
    return _publish_lease_group(lease_groups.get(
        db, group_id, auth.owner_id, default_ttl=EXCLUSIVE_DEFAULT_TTL_S))


@app.get('/api/v1/lease-groups/{group_id}/events')
def lease_group_events(group_id: str, after: int = 0, limit: int = 100,
                       auth: AuthContext = Depends(get_auth)):
    _require_lease_group(auth, group_id)
    _check_limit(limit)
    if after < 0:
        raise HTTPException(400, 'after must be non-negative')
    return [{**dict(row), 'data': json.loads(row['data'])} for row in db.query(
        'SELECT * FROM lease_group_events WHERE group_id=? AND sequence>? '
        'ORDER BY sequence LIMIT ?', (group_id, after, limit))]


@app.post('/api/v1/lease-groups/{group_id}/advance')
def advance_lease_group(group_id: str, req: LeaseGroupCommand,
                        auth: AuthContext = Depends(get_auth)):
    _require_lease_group(auth, group_id, mutate=True, acquire=True)
    return _publish_lease_group(lease_groups.advance(
        db, group_id, req.generation, auth.owner_id, default_ttl=EXCLUSIVE_DEFAULT_TTL_S,
        total_floor=LEASE_MAX_TOTAL_S, total_hard=LEASE_MAX_TOTAL_HARD_S))


@app.post('/api/v1/lease-groups/{group_id}/serving')
def serve_lease_group(group_id: str, req: LeaseGroupAttestation,
                      auth: AuthContext = Depends(get_auth)):
    _require_lease_group(auth, group_id, mutate=True, acquire=True)
    return _publish_lease_group(lease_groups.mark_serving(
        db, group_id, req.generation, req.model_dump(exclude={'generation'}), auth.owner_id,
        default_ttl=EXCLUSIVE_DEFAULT_TTL_S))


@app.post('/api/v1/lease-groups/{group_id}/release')
def release_lease_group(group_id: str, req: LeaseGroupCommand,
                        auth: AuthContext = Depends(get_auth)):
    _require_lease_group(auth, group_id, mutate=True)
    return _publish_lease_group(lease_groups.begin_release(
        db, group_id, req.generation, auth.owner_id, default_ttl=EXCLUSIVE_DEFAULT_TTL_S))


@app.post('/api/v1/lease-groups/{group_id}/members/{lease_id}/release')
def release_lease_group_member(group_id: str, lease_id: str, req: LeaseGroupAttestation,
                               auth: AuthContext = Depends(get_auth)):
    _require_lease_group(auth, group_id, mutate=True)
    return _publish_lease_group(lease_groups.release_member(
        db, group_id, lease_id, req.generation, req.model_dump(exclude={'generation'}),
        auth.owner_id, default_ttl=EXCLUSIVE_DEFAULT_TTL_S))


@app.post('/api/v1/lease-groups/{group_id}/fence')
def fence_lease_group(group_id: str, req: LeaseGroupFence,
                      auth: AuthContext = Depends(get_auth)):
    _require_lease_group(auth, group_id, mutate=True)
    return _publish_lease_group(lease_groups.fence(
        db, group_id, req.generation, req.reason, auth.owner_id,
        default_ttl=EXCLUSIVE_DEFAULT_TTL_S))

@app.post("/api/v1/resources/{resource_id:path}/leases")
def request_lease(resource_id: str, req: LeaseRequest, auth: AuthContext = Depends(get_auth)):
    """Reserve declared capacity atomically; queueing does not start execution."""
    _require_visible_resource(auth, resource_id)
    body, metadata = reserve_lease(
        db, resource_id, req, auth.owner_id,
        default_ttl=EXCLUSIVE_DEFAULT_TTL_S,
        total_floor=LEASE_MAX_TOTAL_S, total_hard=LEASE_MAX_TOTAL_HARD_S)
    return _publish_lease_reservation(resource_id, req, body, metadata, auth.owner_id)


def _publish_lease_reservation(resource_id, req, body, metadata, actor):
    """Publish ordinary admission effects after the allocating transaction commits."""
    lease_id = body['lease_id']
    # The allocation is committed before any audit/event consumer observes it.
    # Waiters this call's reap granted on the way in are published first: they
    # were ahead of us.
    _publish_lease_moves(metadata)
    if body['status'] == 'queued':
        db.audit(actor, "lease.queued", lease_id,
                 {"resource_id": resource_id, "memory_mb": req.memory_mb,
                  "queue_position": body['queue_position'], "wait_until": body['wait_until']})
        event_bus.emit('lease.queued', lease_id,
                       {'lease_id': lease_id, 'resource_id': resource_id,
                        'service_id': req.service_id, 'memory_mb': req.memory_mb,
                        'queue_position': body['queue_position'],
                        'wait_until': body['wait_until']})
        return body
    max_total_clamped = metadata['max_total_clamped']
    ttl_defaulted = metadata['ttl_defaulted']

    db.audit(actor, "lease.granted", lease_id,
             {"resource_id": resource_id, "memory_mb": req.memory_mb})
    if max_total_clamped:
        db.audit(actor, "lease.max_total_clamped", lease_id,
                 {"resource_id": resource_id, "service_id": req.service_id,
                  "asked_for_s": max_total_clamped,
                  "granted_s": LEASE_MAX_TOTAL_HARD_S,
                  "why": "max_total_s above the registry hard limit"})
    if ttl_defaulted:
        db.audit(actor, "lease.ttl_defaulted", lease_id,
                 {"resource_id": resource_id, "service_id": req.service_id,
                  "ttl_seconds": EXCLUSIVE_DEFAULT_TTL_S,
                  "why": "exclusive lease requested with no duration"})

    event_bus.emit("lease.granted", lease_id,
                   {"lease_id": lease_id, "resource_id": resource_id,
                    "service_id": req.service_id, "memory_mb": req.memory_mb})

    return body


@app.post("/api/v1/leases/{lease_id}/rollover")
def rollover_lease(lease_id: str, req: RolloverRequest, auth: AuthContext = Depends(get_auth)):
    return rollover_residency(lease_id, req, auth,
        require_manage=_require_visible_resource, default_max_total=LEASE_MAX_TOTAL_S,
        emit=event_bus.emit)


@app.post("/api/v1/leases/{lease_id}/renew")
def renew_lease(lease_id: str, req: LeaseRenew, auth: AuthContext = Depends(get_auth)):
    """Extend a lease IN PLACE — its holder or the admin (#827 P3).

    In place is the whole point: same row, same id, no release and no re-request.
    A release-then-reacquire is a window in which another caller can take the
    resource, which for an exclusive lease is the failure renewal exists to
    prevent — and is what the gateway's own `manager.py:_renew_leases()` does for
    memory leases, deliberately not reused for the pool.

    A lease past its expiry cannot be renewed back to life: the reap runs first
    and an expired lease answers 404. Otherwise a holder that stopped renewing
    could resurrect a claim another caller has since been granted. That holds
    across the read-then-write window too — the UPDATE is guarded on
    `status = 'active'` and answers 404 when it changes no rows, asserted by
    `test_a_renewal_cannot_resurrect_a_lease_expired_under_it`.
    """
    _expire_stale_leases()
    lease = db.query_one("SELECT * FROM leases WHERE id = ?", (lease_id,))
    if not lease:
        raise HTTPException(404, f"Lease {lease_id} not found")
    if not auth.is_admin() and lease.get("agent_id") != auth.owner_id:
        raise HTTPException(403, "A lease is renewed by its holder")
    if lease.get("status") == "queued":
        # Renewing a wait (0.12.98): the waiter is still here, so its deadline
        # moves out — never in — and its place in the queue is kept. Nothing is
        # granted by this; promotion happens when the bytes free.
        if lease.get("expires_at") is None:
            # An indefinite wait (`wait: true`) has no deadline to move. The waiter
            # saying "still here" is answered and changes nothing on the row.
            out = _lease_with_position(lease)
            out.update({"status": "queued", "wait_until": None, "extended_by_s": 0,
                        "reason": "waiting"})
            return out
        now = datetime.now(timezone.utc)
        ttl = max(1, int(req.ttl_seconds))
        current = datetime.fromisoformat(lease["expires_at"])
        new_until = max(current, now + timedelta(seconds=ttl))
        if not db.execute_count(
                "UPDATE leases SET expires_at = ? WHERE id = ? AND status = 'queued'",
                (new_until.isoformat(), lease_id)):
            lease = db.query_one("SELECT * FROM leases WHERE id = ?", (lease_id,)) or lease
            if lease.get("status") == "active":
                return _lease_with_position(lease) | {"reason": "promoted", "extended_by_s": 0}
            raise HTTPException(404, {
                "detail": f"Lease {lease_id} left the queue while its wait was renewed",
                "status": lease.get("status")})
        out = _lease_with_position(lease)
        out.update({"status": "queued", "wait_until": new_until.isoformat(),
                    "extended_by_s": math.ceil(max(0, (new_until - current).total_seconds())),
                    "reason": "waiting"})
        return out
    if lease.get("status") != "active":
        # The one case a holder cannot detect for itself: it was reaped mid-work
        # and whatever exclusivity it believed it had is already gone.
        raise HTTPException(404, {
            "detail": f"Lease {lease_id} is {lease.get('status')}, not active",
            "status": lease.get("status"),
            "expires_at": lease.get("expires_at")})

    now = datetime.now(timezone.utc)
    ttl = max(1, int(req.ttl_seconds))
    was = lease.get("expires_at")
    if not was:
        # An indefinite lease has nothing to renew, and a shared one may still be
        # indefinite. Saying so beats inventing an expiry nobody asked for.
        return {"lease_id": lease_id, "status": "active",
                "granted_at": lease.get("granted_at"), "expires_at": None,
                "extended_by_s": 0, "capped": False, "reason": "indefinite",
                "max_total_s": int(lease.get("max_total_s") or LEASE_MAX_TOTAL_S),
                "note": "indefinite lease — nothing to renew"}

    granted = datetime.fromisoformat(lease["granted_at"])
    # The row's own ceiling. NULL on any lease granted before the column existed, and
    # the registry default is the right answer for those.
    max_total = int(lease.get("max_total_s") or LEASE_MAX_TOTAL_S)
    ceiling = granted + timedelta(seconds=max_total)
    target = min(now + timedelta(seconds=ttl), ceiling)
    current = datetime.fromisoformat(was)
    # Never shorten. A caller passing a small ttl would otherwise hand an
    # exclusive resource away early, which is the failure this removes. A holder
    # that wants to give a lease back uses `release`.
    new_expires = max(current, target)
    # Decide on whether the expiry MOVED, not on the rounded number. `int()` of a
    # sub-second move is 0, so a renewal landing within a second of its grant
    # (a heartbeat can fire 0-60s after acquisition) used to skip this write and
    # fall through to `ttl_below_granted`: a caller misconfiguration that had not
    # happened, logged at ERROR on the normal path (2026-09-27 21:15:19Z, 486 ms
    # after the grant). A partial second reports as 1, so `extended_by_s > 0`
    # always agrees with `reason == "extended"`.
    # Asserted by test_a_renewal_within_a_second_of_its_grant_is_an_extension.
    moved_s = (new_expires - current).total_seconds()
    extended = math.ceil(moved_s) if moved_s > 0 else 0
    if extended:
        # GUARDED ON status, and this is the whole reason: handlers run in a
        # threadpool, so any other request's reap — a GET /leases, a grant, a
        # resource read — can expire this row between the SELECT above and this
        # write. Unguarded, the renewal answers 200 `active` with an extension for
        # a lease that is already `expired`, and the resource can meanwhile be
        # granted to somebody else. That is exactly the outcome the docstring
        # claims to prevent, so it is asserted rather than described:
        # test_a_renewal_cannot_resurrect_a_lease_expired_under_it.
        if not db.execute_count(
                "UPDATE leases SET expires_at = ? WHERE id = ? AND status = 'active'",
                (new_expires.isoformat(), lease_id)):
            raise HTTPException(404, {
                "detail": f"Lease {lease_id} expired while being renewed",
                "status": "expired"})

    db.audit(auth.owner_id, "lease.renewed", lease_id,
             {"resource_id": lease["resource_id"], "ttl_seconds": ttl,
              "extended_by_s": extended, "expires_at": new_expires.isoformat()})
    event_bus.emit("lease.renewed", lease_id,
                   {"lease_id": lease_id, "resource_id": lease["resource_id"],
                    "extended_by_s": extended})

    # WHY, not just what. `extended_by_s: 0` has three causes and a caller cannot
    # tell them apart from the numbers: the ceiling bound us, the lease has no
    # expiry, or its expiry is already further out than what we asked for. This
    # list was once presented as complete while a fourth cause existed: int()
    # truncation of a sub-second move, which made 0 out of a real extension and
    # labelled it ttl_below_granted (fixed in 0.12.56 by deciding on whether the
    # expiry moved). The list was wrong, not merely incomplete. One seat's renewal loop inferred "0 means ceiling" from
    # the contract and would have logged "capped at the 1800s ceiling" for an
    # indefinite lease — a reason the registry never gave. So the response names
    # its own reason and nobody has to derive one:
    #
    #   extended           the expiry moved
    #   at_ceiling         accepted, and this lease's own max_total_s bound it
    #   ttl_below_granted  the expiry is already beyond now + ttl, and the ceiling is
    #                      not what did it — so the renewal ttl is smaller than what
    #                      the lease was granted with. Every renewal will report this
    #                      and extend nothing, the lease will lapse on its original
    #                      expiry, and a loop reporting success the whole way down has
    #                      liveness switched off. A misconfiguration, named as one.
    #   indefinite         no expiry to extend (returned earlier)
    #
    # Before the stored ceiling existed these last two were one value, `already_later`,
    # which meant a >ceiling grant (the adapter's 5400s turn) was reported as a
    # too-small renewal ttl: a correct message for the wrong reason.
    if extended:
        reason = "extended"
    elif target >= ceiling:
        reason = "at_ceiling"
    else:
        reason = "ttl_below_granted"
    return {
        "lease_id": lease_id,
        "status": "active",
        "granted_at": lease.get("granted_at"),
        "expires_at": new_expires.isoformat(),
        "extended_by_s": extended,
        # True means the ceiling bound this renewal: accepted, extended nothing.
        # A caller logging a renewal has to be able to tell that from a real
        # extension, or a renewal that does nothing reads like one that worked —
        # which is the shape of every defect this crew found on 2026-09-27.
        "capped": target >= ceiling,
        "reason": reason,
        "max_total_s": max_total,
    }


@app.delete("/api/v1/leases/{lease_id}")
def release_lease(lease_id: str, auth: AuthContext = Depends(get_auth)):
    """Release a lease — its holder or the admin. On a `hold` resource the same call
    on an expired holder is the settlement: the explicit, authorized act that lets
    its bytes go back to the ledger (task 868)."""
    lease = db.query_one("SELECT * FROM leases WHERE id = ?", (lease_id,))
    if not lease:
        raise HTTPException(404, f"Lease {lease_id} not found")
    if not auth.is_admin() and lease.get("agent_id") != auth.owner_id:
        raise HTTPException(403, "A lease is released by its holder")

    # The release and the promotion of whoever now fits commit together (0.12.98):
    # the bytes never sit free while a waiter is queued for them. A queued row
    # released by its requester leaves the queue, and the rows behind it move up.
    # The reap runs first in that transaction, so a waiter whose deadline passed is
    # not resurrected by the release (codex, PR 100 review).
    moves = release_lease_rows(db, lease, default_ttl=EXCLUSIVE_DEFAULT_TTL_S)

    kind = {"cancelled": "lease.cancelled", "settled": "lease.settled"}.get(moves["status"], "lease.released")
    db.audit(auth.owner_id, kind, lease_id,
             {"resource_id": lease["resource_id"], "was": lease.get("status")})

    event_bus.emit(kind, lease_id,
                   {"lease_id": lease_id, "resource_id": lease["resource_id"]})
    _publish_lease_moves(moves)

    res = db.query_one("SELECT * FROM resources WHERE id = ?", (lease["resource_id"],))
    available = _get_available_memory(_normalise_resource(dict(res))) if res else 0

    return {"status": moves["status"], "resource_available_mb": available,
            "promoted": [row["id"] for row in moves["promoted"]]}


@app.get("/api/v1/leases/{lease_id}")
def get_lease(lease_id: str, auth: AuthContext = Depends(get_auth)):
    """One lease, with its live queue position while it waits. Hidden (404)
    outside the scope of its resource's device, like the resource itself."""
    _expire_stale_leases()
    lease = db.query_one("SELECT * FROM leases WHERE id = ?", (lease_id,))
    if not lease:
        raise HTTPException(404, f"Lease {lease_id} not found")
    _require_visible_resource(auth, lease["resource_id"])
    return _lease_with_position(lease)


def _lease_with_position(lease: dict) -> dict:
    out = dict(lease)
    out["exclusive"] = bool(out.get("exclusive"))
    out["queue_position"] = None
    if lease.get("status") == "queued":
        rows = db.query("SELECT * FROM leases WHERE resource_id = ? AND status = 'queued'",
                        (lease["resource_id"],))
        for entry in lease_queue_view(rows):
            if entry["lease_id"] == lease["id"]:
                out["queue_position"] = entry["position"]
        out["wait_until"] = lease.get("expires_at")
    return out


LEASE_WAIT_MAX_S = 120


@app.get("/api/v1/leases/{lease_id}/wait")
def wait_for_lease(lease_id: str, timeout_s: int = 30, auth: AuthContext = Depends(get_auth)):
    """Long-poll a queued lease: answers when it leaves the queue (granted,
    expired, released) or after `timeout_s` (at most 120), with the row as it
    is then. A shell client blocks on this instead of hammering the resource;
    a service that already reconciles against the resource needs nothing new.
    Each call occupies one request worker for its duration, so it is sized for
    a few waiters per host, not a fleet of pollers (0.12.98)."""
    deadline = time.monotonic() + max(0, min(int(timeout_s), LEASE_WAIT_MAX_S))
    checked = False
    while True:
        _expire_stale_leases()
        lease = db.query_one("SELECT * FROM leases WHERE id = ?", (lease_id,))
        if not lease:
            raise HTTPException(404, f"Lease {lease_id} not found")
        if not checked:
            _require_visible_resource(auth, lease["resource_id"])
            checked = True
        if lease.get("status") != "queued" or time.monotonic() >= deadline:
            out = _lease_with_position(lease)
            out["waiting"] = lease.get("status") == "queued"
            return out
        time.sleep(1)


@app.get("/api/v1/leases")
def list_leases(resource_id: str = None, service_id: str = None, status: str = None,
                auth: Optional[AuthContext] = Depends(get_auth_optional)):
    """List leases with optional filters (scope-enforced, fail closed).

    A lease is visible iff its resource's parent device is in the caller's
    scope. Admin sees all; an unauthenticated caller sees none.
    """
    # Nothing else reaps on this path, so `?status=active` could list leases that
    # expired hours ago until some resource read or grant happened to clear them
    # (#827).
    _expire_stale_leases()
    clauses, params = [], []
    if resource_id:
        clauses.append("resource_id = ?")
        params.append(resource_id)
    if service_id:
        clauses.append("service_id = ?")
        params.append(service_id)
    if status:
        clauses.append("status = ?")
        params.append(status)

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    leases = db.query(f"SELECT * FROM leases{where} ORDER BY created_at DESC", tuple(params))
    visible_resources = _visible_resource_ids(auth)
    if visible_resources is not None:
        leases = [l for l in leases if l["resource_id"] in visible_resources]
    return leases


# --- Group Management ---


def _require_group_write(auth: AuthContext, group_id: str, min_role: str = "editor"):
    """Require admin, or group membership at min_role or higher. Raises 403 on failure."""
    if auth.is_admin():
        return
    role = _get_group_role(auth, group_id)
    if role is None:
        raise HTTPException(403, f"Not a member of group {group_id}")
    if ROLE_HIERARCHY.get(role, 0) < ROLE_HIERARCHY.get(min_role, 0):
        raise HTTPException(403, f"Requires {min_role} role or higher in group {group_id}")


@app.get("/api/v1/admin/scope/unscoped")
def scope_unscoped(auth: AuthContext = Depends(require_admin)):
    """Scope v1, condition C2 (doc 263): what still has no group. Empty once the
    v20 backfill has run; anything here is a row written around the API."""
    out = {}
    for tbl in ("tickets", "docs", "projects", "files"):
        rows = db.query(f"SELECT id FROM {tbl} WHERE group_scope IS NULL OR group_scope = ''")
        if rows:
            out[tbl] = [r["id"] for r in rows]
    return {"unscoped": out, "count": sum(len(v) for v in out.values())}


@app.get("/api/v1/groups")
def list_groups(auth: AuthContext = Depends(get_auth)):
    """List all groups. Admin sees all; others see groups they belong to."""
    groups = db.query("SELECT * FROM groups ORDER BY name")
    if not auth.is_admin():
        member_groups = set(g for g in auth.get_group_scopes())
        # Also check group_members table
        if auth.owner_id.startswith("user:"):
            user_id = int(auth.owner_id.split(":")[1])
            for row in db.query("SELECT group_id FROM group_members WHERE user_id = ?", (user_id,)):
                member_groups.add(row["group_id"])
        groups = [g for g in groups if g["id"] in member_groups]
    return groups


_GROUP_ID_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")


@app.post("/api/v1/groups")
def create_group(req: GroupCreate, auth: AuthContext = Depends(get_auth)):
    """Create a group. Any user may (scope v1, doc 261: *any user should be able
    to create their own group where they can have environments and docs and
    control access*) — the creator becomes its owner; an admin creating one is
    owner of everything already and is not written as a member."""
    if not _GROUP_ID_RE.match(req.id or ""):
        raise HTTPException(422, "A group id is letters, digits, - or _, up to 48 characters.")
    existing = db.query_one("SELECT id FROM groups WHERE id = ?", (req.id,))
    if existing:
        raise HTTPException(409, f"Group {req.id} already exists")
    if req.doc_id is not None and not db.query_one("SELECT id FROM docs WHERE id = ?", (req.doc_id,)):
        raise HTTPException(400, f"Unknown doc_id {req.doc_id}.")
    if not auth.is_admin() and not auth.owner_id.startswith("user:"):
        raise HTTPException(403, "A group is made by a person or an agent, not a device or service token.")

    now = datetime.now(timezone.utc).isoformat()
    uid = int(auth.owner_id.split(":")[1]) if auth.owner_id.startswith("user:") else None
    db.execute("""
        INSERT INTO groups (id, name, description, route_path, doc_id, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (req.id, req.name, req.description, req.route_path, req.doc_id,
          str(uid) if uid is not None else None, now, now))    # created_by references users(id)
    if not auth.is_admin() and uid is not None:
        db.execute("INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES (?, ?, 'owner')",
                   (req.id, uid))

    db.audit(auth.identity(), "group.create", req.id,
             {"name": req.name, "route_path": req.route_path, "owner": auth.identity()})

    return {"status": "created", "group_id": req.id, "owner": auth.identity(),
            "roster": f"GET /api/v1/groups/{req.id}", "add_member": f"POST /api/v1/groups/{req.id}/members"}


@app.get("/api/v1/groups/{group_id}")
def get_group(group_id: str, auth: AuthContext = Depends(get_auth)):
    """Get group with services, devices, and members."""
    group = db.query_one("SELECT * FROM groups WHERE id = ?", (group_id,))
    if not group:
        raise HTTPException(404, f"Group {group_id} not found")

    # Non-admin must be a member or have group scope
    if not auth.is_admin():
        is_member = _get_group_role(auth, group_id) is not None
        has_scope = group_id in auth.get_group_scopes()
        if not is_member and not has_scope:
            raise HTTPException(404, f"Group {group_id} not found")   # hidden outside scope (doc 261, decision 4)

    services = db.query("""
        SELECT s.* FROM group_services gs
        JOIN services s ON s.id = gs.service_id
        WHERE gs.group_id = ?
        ORDER BY s.device_id, s.name
    """, (group_id,))
    group["services"] = services

    devices = db.query("""
        SELECT d.* FROM group_devices gd
        JOIN devices d ON d.id = gd.device_id
        WHERE gd.group_id = ?
        ORDER BY d.id
    """, (group_id,))
    group["devices"] = devices

    members = db.query("""
        SELECT u.id, u.username, u.role as user_role, gm.role as group_role, gm.added_at
        FROM group_members gm
        JOIN users u ON u.id = gm.user_id
        WHERE gm.group_id = ?
        ORDER BY u.username
    """, (group_id,))
    group["members"] = members
    # Scope v1 (doc 261): what the group is for and where its knowledge lives —
    # the two things a newcomer's guide draws on — plus the caller's own role.
    group["your_role"] = groupscope.role_in(auth, group_id)
    if group.get("doc_id"):
        drow = db.query_one("SELECT id, slug, title FROM docs WHERE id = ?", (group["doc_id"],))
        group["working_doc"] = ({"id": drow["id"], "slug": drow["slug"], "title": drow["title"],
                                 "read": f"GET /api/v1/docs/{drow['id']}"} if drow else None)
    if group.get("created_by") and str(group["created_by"]).isdigit():
        crow = db.query_one("SELECT username FROM users WHERE id = ?", (int(group["created_by"]),))
        group["created_by"] = crow["username"] if crow else group["created_by"]

    return group


@app.patch("/api/v1/groups/{group_id}")
def update_group(group_id: str, req: GroupUpdate, auth: AuthContext = Depends(get_auth)):
    """Update group details. Requires owner or admin."""
    group = db.query_one("SELECT id FROM groups WHERE id = ?", (group_id,))
    if not group:
        raise HTTPException(404, f"Group {group_id} not found")
    _require_group_write(auth, group_id, "owner")

    updates, params = [], []
    if req.name is not None:
        updates.append("name = ?")
        params.append(req.name)
    if req.description is not None:
        updates.append("description = ?")
        params.append(req.description)
    if req.route_path is not None:
        updates.append("route_path = ?")
        params.append(req.route_path)
    if req.doc_id is not None:
        # the working doc — the group's knowledge base entry; 0 clears; it must be readable to the caller
        if req.doc_id == 0:
            updates.append("doc_id = NULL")
        else:
            drow = db.query_one("SELECT * FROM docs WHERE id = ?", (req.doc_id,))
            if not drow or not _can_see_doc(auth, drow):
                raise HTTPException(400, f"Unknown doc_id {req.doc_id}.")
            updates.append("doc_id = ?")
            params.append(req.doc_id)

    if updates:
        updates.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(group_id)
        db.execute(f"UPDATE groups SET {', '.join(updates)} WHERE id = ?", tuple(params))

    return {"status": "updated", "group_id": group_id}


@app.post("/api/v1/groups/{group_id}/services")
def add_group_service(group_id: str, req: GroupServiceAdd, auth: AuthContext = Depends(get_auth)):
    """Add a service to a group. Requires editor or higher."""
    group = db.query_one("SELECT id FROM groups WHERE id = ?", (group_id,))
    if not group:
        raise HTTPException(404, f"Group {group_id} not found")
    _require_group_write(auth, group_id, "editor")

    # Scope v1: a service joins a group by someone who may change it — its
    # owner or its device's manager (doc 262 §gaps 13).
    service = _require_service_manager(auth, req.service_id)

    existing = db.query_one(
        "SELECT group_id FROM group_services WHERE group_id = ? AND service_id = ?",
        (group_id, req.service_id),
    )
    if existing:
        raise HTTPException(409, f"Service {req.service_id} already in group {group_id}")

    db.execute(
        "INSERT INTO group_services (group_id, service_id) VALUES (?, ?)",
        (group_id, req.service_id),
    )

    db.audit(auth.owner_id, "group.service.add", group_id,
             {"service_id": req.service_id})

    return {"status": "added", "group_id": group_id, "service_id": req.service_id}


@app.delete("/api/v1/groups/{group_id}/services/{service_id:path}")
def remove_group_service(group_id: str, service_id: str, auth: AuthContext = Depends(get_auth)):
    """Remove a service from a group. Requires owner or admin."""
    _require_group_write(auth, group_id, "owner")

    existing = db.query_one(
        "SELECT group_id FROM group_services WHERE group_id = ? AND service_id = ?",
        (group_id, service_id),
    )
    if not existing:
        raise HTTPException(404, f"Service {service_id} not in group {group_id}")

    db.execute(
        "DELETE FROM group_services WHERE group_id = ? AND service_id = ?",
        (group_id, service_id),
    )

    db.audit(auth.owner_id, "group.service.remove", group_id,
             {"service_id": service_id})

    return {"status": "removed", "group_id": group_id, "service_id": service_id}


# --- Group Device Management ---

class GroupDeviceAdd(StrictModel):
    device_id: str


@app.get("/api/v1/groups/{group_id}/devices")
def list_group_devices(group_id: str, auth: AuthContext = Depends(get_auth)):
    """List devices in a group."""
    group = db.query_one("SELECT id FROM groups WHERE id = ?", (group_id,))
    if not group:
        raise HTTPException(404, f"Group {group_id} not found")
    # Non-admin must be a member or have group scope
    if not auth.is_admin():
        is_member = _get_group_role(auth, group_id) is not None
        has_scope = group_id in auth.get_group_scopes()
        if not is_member and not has_scope:
            raise HTTPException(403, f"Not a member of group {group_id}")
    return db.query("""
        SELECT d.* FROM group_devices gd
        JOIN devices d ON d.id = gd.device_id
        WHERE gd.group_id = ?
        ORDER BY d.id
    """, (group_id,))


@app.post("/api/v1/groups/{group_id}/devices")
def add_device_to_group(group_id: str, req: GroupDeviceAdd, auth: AuthContext = Depends(get_auth)):
    """Add a device to a group. Requires editor or higher, or admin."""
    group = db.query_one("SELECT id FROM groups WHERE id = ?", (group_id,))
    if not group:
        raise HTTPException(404, f"Group {group_id} not found")
    _require_group_write(auth, group_id, "editor")

    # Scope v1: bringing a device into a group needs the device to be the
    # caller's to manage (doc 262 §gaps 13: an editor could annex any device
    # and hand the whole group its services, tickets and resources).
    device = _require_managed_device(auth, req.device_id)

    existing = db.query_one(
        "SELECT group_id FROM group_devices WHERE group_id = ? AND device_id = ?",
        (group_id, req.device_id),
    )
    if existing:
        raise HTTPException(409, f"Device {req.device_id} already in group {group_id}")

    db.execute(
        "INSERT INTO group_devices (group_id, device_id) VALUES (?, ?)",
        (group_id, req.device_id),
    )

    db.audit(auth.owner_id, "group.device.add", group_id,
             {"device_id": req.device_id})

    return {"status": "added", "group_id": group_id, "device_id": req.device_id}


@app.delete("/api/v1/groups/{group_id}/devices/{device_id}")
def remove_device_from_group(group_id: str, device_id: str, auth: AuthContext = Depends(get_auth)):
    """Remove a device from a group. Requires owner or admin."""
    _require_group_write(auth, group_id, "owner")

    existing = db.query_one(
        "SELECT group_id FROM group_devices WHERE group_id = ? AND device_id = ?",
        (group_id, device_id),
    )
    if not existing:
        raise HTTPException(404, f"Device {device_id} not in group {group_id}")

    db.execute(
        "DELETE FROM group_devices WHERE group_id = ? AND device_id = ?",
        (group_id, device_id),
    )

    db.audit(auth.owner_id, "group.device.remove", group_id,
             {"device_id": device_id})

    return {"status": "removed", "group_id": group_id, "device_id": device_id}


# --- Group Members ---

@app.post("/api/v1/groups/{group_id}/members")
def add_group_member(group_id: str, req: GroupMemberAdd, auth: AuthContext = Depends(get_auth)):
    """Add a member to a group. Requires owner or admin."""
    group = db.query_one("SELECT id FROM groups WHERE id = ?", (group_id,))
    if not group:
        raise HTTPException(404, f"Group {group_id} not found")
    _require_group_write(auth, group_id, "owner")

    user = db.query_one("SELECT id FROM users WHERE id = ?", (req.user_id,))
    if not user:
        raise HTTPException(404, f"User {req.user_id} not found")

    existing = db.query_one(
        "SELECT group_id FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, req.user_id),
    )
    if existing:
        raise HTTPException(409, f"User {req.user_id} already in group {group_id}")

    try:
        req.role = groupscope.normalise_role(req.role)     # viewer | editor | owner; `admin` reads as owner
    except ValueError as e:
        raise HTTPException(422, str(e))
    # Non-admin can't grant a role higher than their own
    if not auth.is_admin():
        caller_role = _get_group_role(auth, group_id)
        if ROLE_HIERARCHY.get(req.role, 0) > ROLE_HIERARCHY.get(caller_role, 0):
            raise HTTPException(403, f"Cannot grant role '{req.role}' — your role is '{caller_role}'")

    db.execute(
        "INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)",
        (group_id, req.user_id, req.role),
    )

    db.audit(auth.owner_id, "group.member.add", group_id,
             {"user_id": req.user_id, "role": req.role})

    return {"status": "added", "group_id": group_id, "user_id": req.user_id, "role": req.role}


@app.delete("/api/v1/groups/{group_id}/members/{user_id}")
def remove_group_member(group_id: str, user_id: int, auth: AuthContext = Depends(get_auth)):
    """Remove a member from a group. Requires owner or admin."""
    _require_group_write(auth, group_id, "owner")

    existing = db.query_one(
        "SELECT group_id FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )
    if not existing:
        raise HTTPException(404, f"User {user_id} not in group {group_id}")

    db.execute(
        "DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )

    db.audit(auth.owner_id, "group.member.remove", group_id,
             {"user_id": user_id})

    return {"status": "removed", "group_id": group_id, "user_id": user_id}


# --- Step 5: Member Role PATCH ---

class GroupMemberRoleUpdate(StrictModel):
    role: str


@app.patch("/api/v1/groups/{group_id}/members/{user_id}")
def update_member_role(group_id: str, user_id: int, req: GroupMemberRoleUpdate,
                       auth: AuthContext = Depends(get_auth)):
    """Update a member's role in a group. Only owner or admin can change roles."""
    group = db.query_one("SELECT id FROM groups WHERE id = ?", (group_id,))
    if not group:
        raise HTTPException(404, f"Group {group_id} not found")

    try:
        req.role = groupscope.normalise_role(req.role)
    except ValueError as e:
        raise HTTPException(422, str(e))

    _require_group_write(auth, group_id, "owner")

    existing = db.query_one(
        "SELECT role FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )
    if not existing:
        raise HTTPException(404, f"User {user_id} not in group {group_id}")

    # Non-admin can't set role higher than their own
    if not auth.is_admin():
        caller_role = _get_group_role(auth, group_id)
        if ROLE_HIERARCHY.get(req.role, 0) > ROLE_HIERARCHY.get(caller_role, 0):
            raise HTTPException(403, f"Cannot set role '{req.role}' — your role is '{caller_role}'")

    db.execute(
        "UPDATE group_members SET role = ? WHERE group_id = ? AND user_id = ?",
        (req.role, group_id, user_id),
    )

    db.audit(auth.owner_id, "group.member.role.update", group_id,
             {"user_id": user_id, "old_role": existing["role"], "new_role": req.role})

    return {"status": "updated", "group_id": group_id, "user_id": user_id, "role": req.role}


# --- Group-Scoped Status ---

@app.get("/api/v1/groups/{group_id}/status")
def group_status(group_id: str, auth: AuthContext = Depends(get_auth)):
    """System status filtered to services in this group only."""
    group = db.query_one("SELECT * FROM groups WHERE id = ?", (group_id,))
    if not group:
        raise HTTPException(404, f"Group {group_id} not found")
    # Non-admin must be a member or have group scope
    if not auth.is_admin():
        is_member = _get_group_role(auth, group_id) is not None
        has_scope = group_id in auth.get_group_scopes()
        if not is_member and not has_scope:
            raise HTTPException(403, f"Not a member of group {group_id}")

    services = db.query("""
        SELECT s.* FROM group_services gs
        JOIN services s ON s.id = gs.service_id
        WHERE gs.group_id = ?
        ORDER BY s.device_id, s.name
    """, (group_id,))

    # Get unique devices for these services
    device_ids = list(set(s["device_id"] for s in services))
    devices = []
    for did in sorted(device_ids):
        dev = db.query_one("SELECT * FROM devices WHERE id = ?", (did,))
        if dev:
            devices.append(dev)

    # Get routes for these services
    service_ids = [s["id"] for s in services]
    routes = []
    for sid in service_ids:
        svc_routes = db.query("SELECT * FROM routes WHERE service_id = ?", (sid,))
        routes.extend(svc_routes)

    active_services = sum(1 for s in services if s["status"] == "active")
    online_devices = sum(1 for d in devices if d["status"] == "online")

    return {
        "group": {
            "id": group["id"],
            "name": group["name"],
            "description": group["description"],
        },
        "devices": devices,
        "services": services,
        "routes": routes,
        "counts": {
            "devices": len(devices),
            "services": len(services),
            "active_services": active_services,
            "online_devices": online_devices,
            "routes": len(routes),
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7777)


# ---------------------------------------------------------------------------
# Files (#305) — a top-level primitive, flat sibling to docs/tickets/tasks.
#
# Deliberately not childed to whatever created them: upload standalone, keep
# unassociated, share directly, attach to many docs/tickets at once. Deleting a
# doc does not delete its images.

FILE_ROOT = Path(os.environ.get("REGISTRY_FILE_ROOT", "/var/lib/samcloud/files"))

# One ceiling for every type, not a per-type table (#889). The cap exists to
# protect this volume -- it also holds registry.db, the Caddyfile and the
# gateway keys, so filling it takes the control plane down, which is a worse
# failure than a rejected upload -- and the volume does not care what the bytes
# depict. The old table said video 800 MB / everything else 100 MB, which meant
# a Gaussian splat capture (.ply, which every browser sends as
# application/octet-stream, the generic fallback) got the strictest tier while a
# screen recording got 8x: splat captures bounced at 100 MB while a 101 MB
# screencap of them went through. Keeping the headroom reserve below as the
# thing that actually guards the volume lets the size cap be sized for real work
# instead of standing in for a disk check.
FILE_MAX_BYTES = int(os.environ.get("REGISTRY_FILE_MAX_MB", "1024")) * 1024 * 1024

# Refuse an upload that would leave the volume with less than this free, so the
# writes the control plane cannot survive losing -- sqlite, the Caddyfile -- keep
# their room. This is what the 100 MB cap was really for, said directly: on
# 2026-09-24 an unbounded container log filled the same disk, sqlite failed every
# write, and because `verify_token` writes `last_used` on every authenticated
# request the whole plane 500'd. A size cap cannot prevent that (enough uploads
# under any cap still fill a disk) and a reserve can.
FILE_FREE_RESERVE_BYTES = int(os.environ.get("REGISTRY_FILE_RESERVE_MB", "2048")) * 1024 * 1024

# Read size for the streaming upload. Peak memory per upload is this, not the
# file: see upload_file.
FILE_CHUNK_BYTES = 4 * 1024 * 1024
FILE_ALLOWED_PREFIXES = ("image/", "video/", "audio/", "text/")
FILE_ALLOWED_EXACT = {"application/pdf", "application/json", "application/octet-stream"}


class _BlobWriter:
    """A streamed write into the blob store: hash while the bytes land on disk.

    Peak memory is one chunk rather than the whole upload, which is what makes
    a cap above a few hundred MB safe to offer -- the registry runs in a 7.7 GB
    Colima VM, so the previous read-all-then-join cost ~2x the file size in RAM
    at the moment of joining and put a 1 GB upload within reach of the OOM
    killer (#889).

    The temp file sits on the blob volume, so committing is a rename on one
    filesystem rather than a copy, and `abort` removes it -- an upload that is
    refused, fails or disconnects leaves nothing behind.
    """

    def __init__(self, store: "_LocalBlobStore", tmp: Path, fh):
        self._store = store
        self._tmp = tmp
        self._fh = fh
        self._digest = hashlib.sha256()
        self._closed = False

    def write(self, chunk: bytes) -> None:
        self._fh.write(chunk)
        self._digest.update(chunk)

    def _close(self) -> None:
        if not self._closed:
            self._closed = True
            self._fh.close()

    def commit(self) -> str:
        """Move the blob into place under its content hash and return the hash."""
        self._close()
        sha = self._digest.hexdigest()
        dst = self._store.path(sha)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            self._tmp.unlink(missing_ok=True)   # identical content already stored
        else:
            self._tmp.replace(dst)              # atomic: no reader sees a partial blob
        return sha

    def abort(self) -> None:
        self._close()
        self._tmp.unlink(missing_ok=True)


class _LocalBlobStore:
    """Content-addressed blobs on the registry-data volume.

    Kept behind writer/path/stat/delete so the backing store can move to a NAS
    or object store later as a config change plus a migration rather than a
    rewrite -- and because the address is the content hash, that migration is
    idempotent and safely re-runnable. Nothing here takes or returns a whole
    file: a caller streams in through `writer()` and streams out from `path()`.

    The uploaded filename NEVER touches a path; it is metadata in the DB only.
    """

    def __init__(self, root: Path):
        self.root = root

    def path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    def writer(self) -> _BlobWriter:
        """A streaming write. May raise ENOSPC: the caller maps that to 507."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.root, prefix=".incoming-")
        # The descriptor is handed over rather than closed and reopened by
        # name: nothing can swap the path under us in between, and there is no
        # second open to fail after mkstemp has already created the file.
        return _BlobWriter(self, Path(name), os.fdopen(fd, "wb"))

    def free_bytes(self) -> int:
        """Space left on the volume the blobs live on."""
        self.root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root).free

    def stat(self, sha: str) -> bool:
        return self.path(sha).exists()

    def delete(self, sha: str) -> None:
        self.path(sha).unlink(missing_ok=True)


blobs = _LocalBlobStore(FILE_ROOT)


def _file_type_ok(content_type: str) -> bool:
    return (content_type in FILE_ALLOWED_EXACT
            or any(content_type.startswith(p) for p in FILE_ALLOWED_PREFIXES))


def _resolve_file_scope(auth: AuthContext, group_scope, current=None) -> str:
    """Write-side twin of the read predicate — one resolver for every kind (scope v1).

    Unnamed RESOLVES; it does not fall through to NULL. That fall-through is
    what this function used to do, and `auth.md` has always said the opposite --
    "Every ticket, doc, project (and its tasks) and file is in exactly one
    group" -- so a file uploaded without `group_scope` was the one object on the
    plane in no group, visible to its owner alone. On 2026-10-14 a concept deck
    for a `projects` task was posted that way and it read 404 to every
    projects reader, including the seat the deck was for (#975).

    Every uploader on the plane today is a user or agent identity with group
    scopes, so the 422 `_resolve_group` raises for a writer who edits nothing
    is an edge rather than a live caller -- checked against the owners of all
    200 stored files before this changed.
    """
    return _resolve_group(auth, group_scope, "file", current=current)


def _can_see_link_target(auth: AuthContext, ttype: str, tid) -> bool:
    """Can the caller read the doc, ticket or task a file is (or would be) linked to?

    One predicate for both sides: the read union in `_can_see_file`, and the
    link write, which refuses a target the linker cannot read (#975).

    A task carries no group of its own: it is scoped through its project, which
    is what `_load_task_and_project` enforces on every task read, so a file link
    is never more permissive than reading the task it hangs on.
    """
    if ttype == "doc":
        doc = db.query_one("SELECT * FROM docs WHERE id = ?", (tid,))
        return bool(doc) and _can_see_doc(auth, doc)
    if ttype == "ticket":
        t = db.query_one("SELECT * FROM tickets WHERE id = ?", (tid,))
        return bool(t) and _can_see_ticket(auth, t)
    if ttype == "task":
        tk = db.query_one("SELECT project_id FROM tasks WHERE id = ?", (tid,))
        if not tk:
            return False
        proj = db.query_one("SELECT * FROM projects WHERE id = ?", (tk["project_id"],))
        return bool(proj) and _can_see_project(auth, proj)
    return False


def _can_see_file(auth: AuthContext, f: dict) -> bool:
    """Visibility is the UNION of the file's own scope and anything it is linked to.

    Union rather than intersection on purpose: attaching a commons image to a
    scoped doc must not leak that doc, and a scoped file linked into a commons
    doc must stay scoped for callers without the scope.
    """
    if groupscope.object_readable(auth, f, owner_cols=("owner",)):
        return True
    for ln in db.query("SELECT target_type, target_id FROM file_links WHERE file_id = ?",
                       (f["id"],)):
        if _can_see_link_target(auth, ln["target_type"], ln["target_id"]):
            return True
    return False


def _file_view(f: dict) -> dict:
    f = dict(f)
    f["url"] = f"/api/v1/files/{f['id']}/raw"
    f["links"] = db.query(
        "SELECT id, target_type, target_id FROM file_links WHERE file_id = ?", (f["id"],))
    return f


@app.post("/api/v1/files")
async def upload_file(file: UploadFile = File(...), group_scope: str = Form(None),
                      auth: AuthContext = Depends(get_auth)):
    """Upload a file. Owner is bearer-derived; the stored name is metadata only."""
    group_scope = _resolve_file_scope(auth, group_scope)
    ctype = (file.content_type or "application/octet-stream").split(";")[0].strip()
    if not _file_type_ok(ctype):
        raise HTTPException(415, f"content_type '{ctype}' not allowed")

    # Stream to the blob volume, hashing as the bytes land: an oversized upload
    # is refused mid-flight and nothing is ever held whole in memory.
    #
    # `free` is read once, before the first byte, and the room left is tracked
    # against it. That is deliberately not a reservation -- two uploads can
    # both pass this and then compete for the same bytes -- so it buys a
    # legible refusal, not a guarantee. The guarantee is the ENOSPC branch
    # below, where the write itself fails and we keep nothing.
    free = blobs.free_bytes()
    writer = None
    total = 0
    committed = False
    try:
        # Inside the try: on a volume that is ALREADY full, mkstemp itself
        # raises ENOSPC, and that deserves the same 507 as filling it mid-write
        # rather than an unhandled 500.
        writer = blobs.writer()
        while True:
            chunk = await file.read(FILE_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > FILE_MAX_BYTES:
                raise HTTPException(
                    413, f"file exceeds the {FILE_MAX_BYTES // (1024*1024)} MB upload limit")
            if free - total < FILE_FREE_RESERVE_BYTES:
                raise HTTPException(
                    507, f"not enough room on the file volume: {free // (1024*1024)} MB free, "
                         f"of which {FILE_FREE_RESERVE_BYTES // (1024*1024)} MB is reserved for "
                         f"the control plane's own writes. Delete some files, or raise "
                         f"REGISTRY_FILE_RESERVE_MB if the volume has grown.")
            writer.write(chunk)
        if total == 0:
            raise HTTPException(400, "empty upload")
        sha = writer.commit()
        committed = True
    except OSError as exc:
        # The volume filled under us -- or another writer won the race the
        # check above cannot settle. Asserted by
        # TestFileUploadHeadroom::test_a_volume_that_fills_mid_upload_keeps_nothing.
        if exc.errno == errno.ENOSPC:
            raise HTTPException(
                507, "the file volume filled while the upload was in flight; nothing was kept"
            ) from exc
        raise
    finally:
        # Covers the refusals above and a client that disconnects mid-upload
        # (CancelledError), neither of which should leave a part-file behind.
        # `writer is None` is the case where mkstemp never got that far.
        if writer is not None and not committed:
            writer.abort()

    name = os.path.basename(file.filename or sha)[:200]
    fid = db.execute(
        "INSERT INTO files (sha256, name, content_type, bytes, group_scope, owner) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sha, name, ctype, total, group_scope, auth.identity()))
    row = db.query_one("SELECT * FROM files WHERE id = ?", (fid,))
    event_bus.emit("file.uploaded", f"file:{fid}",
                   {"id": fid, "name": name, "bytes": total, "content_type": ctype},
                   scope=f"file:{fid}")
    return _file_view(row)


@app.get("/api/v1/files")
def list_files(kind: str = None, q: str = None, mine: bool = False,
               auth: AuthContext = Depends(get_auth)):
    """List files visible to the caller. ?kind=image filters by content-type prefix."""
    clauses, params = [], []
    if kind:
        clauses.append("content_type LIKE ?"); params.append(f"{kind}%")
    if q:
        clauses.append("name LIKE ?"); params.append(f"%{q}%")
    if mine:
        clauses.append("owner = ?"); params.append(auth.identity())
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = db.query(f"SELECT * FROM files{where} ORDER BY created_at DESC LIMIT 200",
                    tuple(params))
    return {"files": [_file_view(f) for f in rows if _can_see_file(auth, f)]}


@app.get("/api/v1/files/{file_id}")
def get_file(file_id: int, auth: AuthContext = Depends(get_auth)):
    f = db.query_one("SELECT * FROM files WHERE id = ?", (file_id,))
    if not f:
        raise HTTPException(404, f"File {file_id} not found")
    if not _can_see_file(auth, f):
        raise HTTPException(404, "File not found")
    return _file_view(f)


@app.get("/api/v1/files/{file_id}/raw")
def get_file_raw(file_id: int, auth: AuthContext = Depends(get_auth)):
    """Serve the blob with its declared type. Never sniffed, so no execution surface."""
    f = db.query_one("SELECT * FROM files WHERE id = ?", (file_id,))
    if not f:
        raise HTTPException(404, f"File {file_id} not found")
    if not _can_see_file(auth, f):
        raise HTTPException(404, "File not found")
    if not blobs.stat(f["sha256"]):
        raise HTTPException(410, "Blob missing from store")
    safe = f["name"].replace('"', "")
    try:
        disposition = f'inline; filename="{safe}"'
        disposition.encode("latin-1")
    except UnicodeEncodeError:
        # Names straight off macOS carry U+202F etc. — headers are latin-1, so
        # RFC 5987 it: ASCII fallback + filename* with the real name (files #241).
        from urllib.parse import quote
        ascii_name = safe.encode("ascii", "replace").decode().replace("?", "_")
        disposition = f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(safe)}'
    # FileResponse, not Response(content=...): it streams off the volume, so
    # serving a 1 GB capture costs one buffer rather than a 1 GB allocation in
    # a 7.7 GB VM (#889). Content-Disposition is passed in rather than via
    # `filename=` because the RFC 5987 form above is already built; Starlette
    # only synthesises that header when `filename` is given.
    return FileResponse(
        blobs.path(f["sha256"]),
        media_type=f["content_type"],
        headers={"Content-Disposition": disposition,
                 "X-Content-Type-Options": "nosniff",
                 "Cache-Control": "private, max-age=3600"})


@app.patch("/api/v1/files/{file_id}")
def update_file(file_id: int, req: dict, auth: AuthContext = Depends(get_auth)):
    """Re-scope a file. Owner or admin, the same guard as delete.

    Without this a mis-scoped file was mis-scoped forever: there was no PATCH,
    so the only way to change the group was to upload the bytes again. With a
    1 GB cap that means re-sending a gigabyte to change one string, and it is
    what the uploader had to do on #975 -- file 623 stayed unreadable and became file
    625 instead.

    Only `group_scope` is writable. The name, the content type and the sha are
    what was uploaded; a file that needs a different body is a different file.
    """
    f = db.query_one("SELECT * FROM files WHERE id = ?", (file_id,))
    if not f:
        raise HTTPException(404, f"File {file_id} not found")
    if not _can_see_file(auth, f):
        raise HTTPException(404, "File not found")
    if not (auth.is_admin() or f["owner"] in (auth.owner_id, auth.identity())):
        raise HTTPException(403, "Only the owner or an admin may re-scope a file")
    unknown = set(req or {}) - {"group_scope"}
    if unknown:
        raise HTTPException(400, f"only group_scope is writable; got {sorted(unknown)}")
    if "group_scope" not in (req or {}):
        raise HTTPException(400, "nothing to change: pass group_scope")
    # Same resolver as the upload, with the current value in hand so an
    # unnamed PATCH is a no-op rather than a silent move.
    resolved = _resolve_file_scope(auth, req.get("group_scope"), current=f["group_scope"])
    db.execute("UPDATE files SET group_scope = ? WHERE id = ?", (resolved, file_id))
    row = db.query_one("SELECT * FROM files WHERE id = ?", (file_id,))
    db.audit(auth.identity(), "file.rescoped", str(file_id),
             {"from": f["group_scope"], "to": resolved})
    return _file_view(row)


@app.delete("/api/v1/files/{file_id}")
def delete_file(file_id: int, auth: AuthContext = Depends(get_auth)):
    f = db.query_one("SELECT * FROM files WHERE id = ?", (file_id,))
    if not f:
        raise HTTPException(404, f"File {file_id} not found")
    if not (auth.is_admin() or f["owner"] in (auth.owner_id, auth.identity())):
        raise HTTPException(403, "Only the owner or an admin may delete a file")
    db.execute("DELETE FROM file_links WHERE file_id = ?", (file_id,))
    db.execute("DELETE FROM files WHERE id = ?", (file_id,))
    # Only drop the blob when no other row still references that content --
    # de-duplication means two files can share one sha256.
    if not db.query_one("SELECT 1 FROM files WHERE sha256 = ?", (f["sha256"],)):
        blobs.delete(f["sha256"])
    return {"status": "deleted", "id": file_id}


@app.post("/api/v1/files/{file_id}/links")
def add_file_link(file_id: int, req: dict, auth: AuthContext = Depends(get_auth)):
    """Attach a file to a doc/ticket/task. A relation, not ownership."""
    f = db.query_one("SELECT * FROM files WHERE id = ?", (file_id,))
    if not f:
        raise HTTPException(404, f"File {file_id} not found")
    if not _can_see_file(auth, f):
        raise HTTPException(404, "File not found")
    ttype = (req or {}).get("target_type")
    tid = str((req or {}).get("target_id", ""))
    if ttype not in ("doc", "ticket", "task") or not tid:
        raise HTTPException(400, "target_type must be doc|ticket|task with a target_id")
    # A link widens who reads the file, so the linker must read the target:
    # otherwise a link is a way to publish into a doc, ticket or task you cannot
    # see, and the 404 below would be an existence oracle for them.
    if not _can_see_link_target(auth, ttype, tid):
        raise HTTPException(404, f"{ttype} {tid} not found")
    try:
        lid = db.execute(
            "INSERT INTO file_links (file_id, target_type, target_id, created_by) "
            "VALUES (?, ?, ?, ?)", (file_id, ttype, tid, auth.identity()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "already linked")
    return {"status": "linked", "link_id": lid, "file_id": file_id,
            "target_type": ttype, "target_id": tid}


@app.delete("/api/v1/files/{file_id}/links/{link_id}")
def delete_file_link(file_id: int, link_id: int, auth: AuthContext = Depends(get_auth)):
    ln = db.query_one("SELECT * FROM file_links WHERE id = ? AND file_id = ?", (link_id, file_id))
    if not ln:
        raise HTTPException(404, "Link not found")
    f = db.query_one("SELECT * FROM files WHERE id = ?", (file_id,))
    if not (auth.is_admin() or ln["created_by"] in (auth.owner_id, auth.identity())
            or (f and f["owner"] in (auth.owner_id, auth.identity()))):
        raise HTTPException(403, "Only the linker, the file owner, or an admin may unlink")
    db.execute("DELETE FROM file_links WHERE id = ?", (link_id,))
    return {"status": "unlinked", "link_id": link_id}
