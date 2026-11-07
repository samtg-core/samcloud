"""Token-based enrollment and service registration module.

Handles:
- Enrollment token generation (single-use, time-limited)
- Device token generation (long-lived, for ongoing API auth)
- Service token generation (scoped, for service-to-service)
- Token validation and lookup
"""

import hashlib
import json
import os
import secrets
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta

import db


# --- Token Generation ---

def _random_token(prefix: str) -> str:
    """Generate a random token with a prefix. 32 random hex chars."""
    return f"{prefix}{secrets.token_hex(16)}"


def _hash_token(token: str) -> str:
    """SHA-256 hash of a token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_enrollment_token(device_id: str, context: str, ttl_seconds: int = 3600) -> str:
    """Create a single-use enrollment token for a device.

    Stores the hash in the DB tokens table. Returns the plaintext token.

    Args:
        device_id: ID of the device being enrolled
        context: Device context (samcloud, tunnel)
        ttl_seconds: Time-to-live in seconds (default 1 hour)

    Returns:
        Plaintext token string (sc_enroll_...)
    """
    token = _random_token("sc_enroll_")
    token_hash = _hash_token(token)
    token_id = f"enroll:{device_id}"
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()

    with db.transaction() as conn:
        # Remove any existing enrollment token for this device
        conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
        conn.execute("""
            INSERT INTO tokens (id, token_hash, type, owner_id, scopes, expires_at)
            VALUES (?, ?, 'enrollment', ?, ?, ?)
        """, (token_id, token_hash, device_id,
              json.dumps(["enroll"]), expires_at))

    db.audit("system:enrollment", "token.enrollment.created", device_id,
             {"context": context, "ttl_seconds": ttl_seconds})

    return token


def generate_device_token(device_id: str, scopes: list[str] = None) -> str:
    """Generate a long-lived device token for ongoing API auth.

    One per owner: its id is fixed, so making a new one replaces the last. A
    login doesn't come here; it gets its own session token (generate_session_token).
    """
    if scopes is None:
        scopes = ["device:self", "service:register", "service:heartbeat"]

    token = _random_token("sc_device_")
    token_hash = _hash_token(token)
    token_id = f"device:{device_id}"

    with db.transaction() as conn:
        conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
        conn.execute("""
            INSERT INTO tokens (id, token_hash, type, owner_id, scopes, expires_at)
            VALUES (?, ?, 'device', ?, ?, NULL)
        """, (token_id, token_hash, device_id, json.dumps(scopes)))

    db.audit("system:enrollment", "token.device.created", device_id,
             {"scopes": scopes})

    return token


SESSION_TTL_SECONDS = 7 * 24 * 3600   # the console's cookie lives a week too


def generate_session_token(owner_id: str, scopes: list[str], ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """A login's own token: it expires, and it leaves the owner's other tokens alone (#1010).

    A login used to go through generate_device_token, whose id is fixed per
    owner, so each login revoked the token before it: a dashboard login killed
    the init token in an admin's header file, and one browser's login ended
    another's session. Each login now gets its own id. Expired session tokens
    are removed when a new one is made.
    """
    token = _random_token("sc_device_")
    now = datetime.now(timezone.utc)
    token_id = f"session:{owner_id}:{secrets.token_hex(6)}"
    with db.transaction() as conn:
        conn.execute("DELETE FROM tokens WHERE id LIKE 'session:%' AND expires_at < ?", (now.isoformat(),))
        conn.execute("""
            INSERT INTO tokens (id, token_hash, type, owner_id, scopes, expires_at)
            VALUES (?, ?, 'device', ?, ?, ?)
        """, (token_id, _hash_token(token), owner_id, json.dumps(scopes),
              (now + timedelta(seconds=ttl_seconds)).isoformat()))
    db.audit("system:enrollment", "token.session.created", owner_id, {"ttl_seconds": ttl_seconds})
    return token


def revoke_session_token(token: str) -> bool:
    """Revoke a login's own token, by its value. Long-lived tokens are not touched."""
    row = db.query_one("SELECT id, owner_id FROM tokens WHERE token_hash = ? AND id LIKE 'session:%'",
                       (_hash_token(token),))
    if not row:
        return False
    db.execute("DELETE FROM tokens WHERE id = ?", (row["id"],))
    db.audit("system:enrollment", "token.session.revoked", row["owner_id"])
    return True


def revoke_owner_sessions(owner_id: str) -> int:
    """Revoke every login's token an owner has. Rotating a user's token on purpose
    has to end their logins too: a login keeps the scopes it was made with, so
    otherwise a demoted admin's open login stays admin (#1010)."""
    with db.transaction() as conn:
        n = conn.execute("DELETE FROM tokens WHERE owner_id = ? AND id LIKE 'session:%'", (owner_id,)).rowcount
    if n:
        db.audit("system:enrollment", "token.session.revoked_all", owner_id, {"count": n})
    return n


def generate_agent_invite(agent_id: str, scopes: list[str] = None, ttl_seconds: int = 3600) -> str:
    """Create a single-use invite token for an agent.

    Args:
        agent_id: Agent user ID (e.g. 'agent:claude-home')
        scopes: Agent's permission scopes (e.g. ['device:test-host'])
        ttl_seconds: Time-to-live in seconds (default 1 hour)

    Returns:
        Plaintext token string (sc_invite_...)
    """
    token = _random_token("sc_invite_")
    token_hash = _hash_token(token)
    token_id = f"invite:{agent_id}"
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()

    with db.transaction() as conn:
        conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
        conn.execute("""
            INSERT INTO tokens (id, token_hash, type, owner_id, scopes, expires_at)
            VALUES (?, ?, 'enrollment', ?, ?, ?)
        """, (token_id, token_hash, agent_id,
              json.dumps(scopes or []), expires_at))

    db.audit("system:enrollment", "token.agent_invite.created", agent_id,
             {"scopes": scopes, "ttl_seconds": ttl_seconds})

    return token


def generate_agent_token(agent_id: str, scopes: list[str] = None) -> str:
    """Generate a permanent agent token for ongoing auth.

    Multiple tokens can coexist — login doesn't invalidate existing tokens.
    """
    token = _random_token("sc_agent_")
    token_hash = _hash_token(token)
    token_id = f"agent:{agent_id}"

    with db.transaction() as conn:
        conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
        conn.execute("""
            INSERT INTO tokens (id, token_hash, type, owner_id, scopes, expires_at)
            VALUES (?, ?, 'agent', ?, ?, NULL)
        """, (token_id, token_hash, agent_id, json.dumps(scopes or [])))

    db.audit("system:enrollment", "token.agent.created", agent_id,
             {"scopes": scopes})

    return token


def generate_service_token(service_id: str, scopes: list[str] = None) -> str:
    """Generate a scoped service token for service-to-service auth.

    Args:
        service_id: Service ID (e.g. 'device-id/service-name')
        scopes: Permission scopes (default: service-level access)

    Returns:
        Plaintext token string (sc_service_...)
    """
    if scopes is None:
        scopes = ["service:self", "service:heartbeat"]

    token = _random_token("sc_service_")
    token_hash = _hash_token(token)
    token_id = f"service:{service_id}"

    with db.transaction() as conn:
        # Remove any existing service token
        conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
        conn.execute("""
            INSERT INTO tokens (id, token_hash, type, owner_id, scopes, expires_at)
            VALUES (?, ?, 'service', ?, ?, NULL)
        """, (token_id, token_hash, service_id, json.dumps(scopes)))

    db.audit("system:enrollment", "token.service.created", service_id,
             {"scopes": scopes})

    return token


# --- Token Validation ---

def validate_enrollment_token(token: str) -> dict | None:
    """Validate an enrollment token. Single-use: deletes on successful validation.

    Args:
        token: Plaintext token string

    Returns:
        Token record dict (with owner_id, scopes, type) if valid, None if invalid.
    """
    if not token or not (token.startswith("sc_enroll_") or token.startswith("sc_invite_")):
        return None

    token_hash = _hash_token(token)
    record = db.query_one(
        "SELECT * FROM tokens WHERE token_hash = ? AND type = 'enrollment'",
        (token_hash,),
    )

    if not record:
        return None

    # Check expiry
    if record.get("expires_at"):
        expires = datetime.fromisoformat(record["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            # Expired — clean up
            db.execute("DELETE FROM tokens WHERE id = ?", (record["id"],))
            return None

    # Single-use: delete on validation
    db.execute("DELETE FROM tokens WHERE id = ?", (record["id"],))

    db.audit("system:enrollment", "token.enrollment.validated", record["owner_id"])

    return dict(record)


def verify_token(token: str) -> dict | None:
    """Verify any token (device, service, agent). Does NOT consume the token.

    Used for ongoing authentication (not enrollment).

    Args:
        token: Plaintext token string (sc_device_... or sc_service_...)

    Returns:
        Token record dict if valid, None if invalid or expired.
    """
    if not token:
        return None

    token_hash = _hash_token(token)
    record = db.query_one(
        "SELECT * FROM tokens WHERE token_hash = ? AND type IN ('device', 'service', 'agent')",
        (token_hash,),
    )

    if not record:
        return None

    # Check expiry (device tokens typically don't expire, but check anyway)
    if record.get("expires_at"):
        expires = datetime.fromisoformat(record["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            return None

    # Update last_used
    now = datetime.now(timezone.utc).isoformat()
    db.execute("UPDATE tokens SET last_used = ? WHERE id = ?", (now, record["id"]))

    return dict(record)


def extract_bearer_token(authorization: str | None) -> str | None:
    """Extract token from Authorization header.

    Args:
        authorization: Authorization header value (e.g. 'Bearer sc_device_...')

    Returns:
        Token string or None
    """
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


# --- Keypair Generation (Type 1: Access Device Enrollment) ---

def generate_access_keypair(username: str, device_name: str) -> dict:
    """Generate a unique ed25519 keypair for a user+device combo.

    This is for Type 1 enrollment (access devices like phones/laptops).
    The private key is returned ONCE for the user to download.
    The public key is stored in the registry for adding to authorized_keys.

    Args:
        username: User who will use this key
        device_name: Human-readable device name (e.g. 'alices-phone')

    Returns:
        dict with: private_key, public_key, key_id, comment
    """
    key_id = f"{username}@{device_name}"
    comment = f"samcloud:{key_id}"

    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = os.path.join(tmpdir, "key")
        # Generate ed25519 keypair
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", "", "-C", comment],
            capture_output=True, check=True,
        )
        private_key = open(key_path).read()
        public_key = open(f"{key_path}.pub").read().strip()

    # Store in DB
    now = datetime.now(timezone.utc).isoformat()
    token_id = f"access_key:{key_id}"

    with db.transaction() as conn:
        # Remove any existing key for this user+device
        conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
        conn.execute("""
            INSERT INTO tokens (id, token_hash, type, owner_id, scopes, created_at)
            VALUES (?, ?, 'access_key', ?, ?, ?)
        """, (token_id, public_key, username,
              json.dumps({"device": device_name, "pubkey": public_key}), now))

    db.audit(f"user:{username}", "access_key.generated", key_id,
             {"device": device_name})

    return {
        "private_key": private_key,
        "public_key": public_key,
        "key_id": key_id,
        "comment": comment,
    }


def generate_pair_token(username: str, device_name: str, ttl_seconds: int = 3600) -> str:
    """Create a single-use pair token: a link that mints one access key.

    An access route (a phone or laptop) has no samcloud token of its own, so it
    cannot call POST /access-keys/setup-script. A signed-in user mints this token
    for themselves (or an admin for anyone) and runs `curl <pair_url> | bash` on
    the new device. Type 'pair' is not accepted by verify_token, so the token
    authenticates nothing else. Minting again for the same user+device replaces it.

    Returns:
        Plaintext token string (sc_pair_...)
    """
    token = _random_token("sc_pair_")
    token_hash = _hash_token(token)
    token_id = f"pair:{username}@{device_name}"
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()

    with db.transaction() as conn:
        conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
        conn.execute("""
            INSERT INTO tokens (id, token_hash, type, owner_id, scopes, expires_at)
            VALUES (?, ?, 'pair', ?, ?, ?)
        """, (token_id, token_hash, username,
              json.dumps({"device": device_name}), expires_at))

    db.audit(f"user:{username}", "token.pair.created", f"{username}@{device_name}",
             {"ttl_seconds": ttl_seconds})

    return token


def redeem_pair_token(token: str) -> dict | None:
    """Spend a pair token. Single-use: deleted on the first redeem, valid or expired.

    Returns:
        {"username", "device_name"} if valid, None otherwise.
    """
    if not token or not token.startswith("sc_pair_"):
        return None

    token_hash = _hash_token(token)
    with db.transaction() as conn:
        record = conn.execute(
            "SELECT * FROM tokens WHERE token_hash = ? AND type = 'pair'",
            (token_hash,),
        ).fetchone()
        if not record:
            return None
        conn.execute("DELETE FROM tokens WHERE id = ?", (record["id"],))

    record = dict(record)
    if record.get("expires_at"):
        expires = datetime.fromisoformat(record["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            return None

    device_name = json.loads(record.get("scopes") or "{}").get("device", "")
    db.audit(f"user:{record['owner_id']}", "token.pair.redeemed",
             f"{record['owner_id']}@{device_name}")
    return {"username": record["owner_id"], "device_name": device_name}


def list_access_keys(username: str = None) -> list[dict]:
    """List all access keys, optionally filtered by username."""
    if username:
        keys = db.query(
            "SELECT id, owner_id, scopes, created_at, last_used FROM tokens WHERE type = 'access_key' AND owner_id = ?",
            (username,),
        )
    else:
        keys = db.query(
            "SELECT id, owner_id, scopes, created_at, last_used FROM tokens WHERE type = 'access_key'",
        )

    result = []
    for k in keys:
        scopes = json.loads(k["scopes"]) if k.get("scopes") else {}
        result.append({
            "key_id": k["id"].replace("access_key:", ""),
            "username": k["owner_id"],
            "device": scopes.get("device", "unknown"),
            "public_key": scopes.get("pubkey", ""),
            "created_at": k["created_at"],
            "last_used": k["last_used"],
        })
    return result


def revoke_access_key(key_id: str) -> bool:
    """Revoke an access key by key_id (e.g. 'alice@alices-phone')."""
    token_id = f"access_key:{key_id}"
    existing = db.query_one("SELECT id FROM tokens WHERE id = ?", (token_id,))
    if not existing:
        return False
    db.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
    db.audit("system:enrollment", "access_key.revoked", key_id)
    return True
