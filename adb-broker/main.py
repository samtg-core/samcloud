"""
adb-broker — leased adb access for multi-seat boxes (ticket #538, doc 148).

One broker per device-hosting box. Seats never talk to the adb server
directly: every adb command goes through POST /exec under a granted lease,
with a hard deadline, guarded verbs, an APK-identity gate at the install
door, displaced-APK auto-backup, and a JSONL audit line per operation.

Shape mirrors aws-broker/gh-broker: standalone FastAPI, samcloud Bearer
verified per call, JSONL audit, health loop. The registry stays the
identity/scope source — this is a leaf service, not core.

Run: uvicorn main:app --host 127.0.0.1 --port 7791
"""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


# The plane this broker verifies tokens against (and, where it posts health, reports
# to). Required: the broker serves no instance by default.
SAMCLOUD_API = os.environ.get("SAMCLOUD_API", "").strip().rstrip("/")
if not SAMCLOUD_API:
    raise SystemExit("adb-broker: set SAMCLOUD_API to the plane's API base, e.g. https://cloud.example.org/api/v1")
CONFIG_DIR = Path(os.environ.get("ADB_BROKER_CONFIG", str(Path.home() / ".config/samcloud/adb-broker")))
CONFIG_FILE = CONFIG_DIR / "config.json"
SAMCLOUD_FILE = CONFIG_DIR / "samcloud.json"
STATE_DIR = Path(os.environ.get("ADB_BROKER_STATE", str(Path.home() / ".local/samcloud/adb-broker")))
AUDIT_LOG = STATE_DIR / "audit.jsonl"
STATE_FILE = STATE_DIR / "state.json"
HEALTH_INTERVAL = int(os.environ.get("ADB_BROKER_HEALTH_INTERVAL", "30"))

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULTS = {
    "adb_path": "adb",
    # M1a: front the existing server. M1b: own_server=true + a dedicated
    # socket — the broker's user identity then owns server + adbkey.
    "adb_server_socket": "tcp:5037",
    "own_server": False,
    "aapt_path": "aapt",
    "backups_dir": str(STATE_DIR / "backups"),
    "backups_max_gb": 30,
    "staging_dir": "/Users/Shared/samadb-staging",
    # applicationIds that need validation-grade (canonical) lease rights to
    # install — the "real" bundle ids on this box.
    "canonical_ids": [],
    "default_lease_ttl": 600,
    "max_lease_ttl": 3600,
    "queue_ttl": 900,
    "default_exec_timeout": 120,
    "max_exec_timeout": 600,
    # Streaming logcat captures land here so any seat can read its own file
    # (broker state dirs are private to the broker's user).
    "captures_dir": "/Users/Shared/samadb-captures",
    "output_cap_bytes": 512 * 1024,
    "device_poll_interval": 10,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    return cfg


CFG = load_config()

# Verbs that reshape the server/transport for everyone on the box.
GUARDED_VERBS = {
    "kill-server", "start-server", "disconnect", "connect", "reconnect",
    "tcpip", "usb", "root", "unroot", "sideload", "reboot-bootloader",
}
# Client global flags we always set ourselves; caller-supplied ones are refused.
FORBIDDEN_FLAGS = {"-s", "-t", "-d", "-e", "-H", "-P", "-L", "--one-device"}


# ---------------------------------------------------------------- audit

def audit(kind: str, **fields) -> None:
    entry = {"ts": int(time.time()), "kind": kind}
    entry.update(fields)
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------- auth (aws-broker idiom)

async def verify_samcloud(authorization: Optional[str]) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    fake = os.environ.get("ADB_BROKER_FAKE_AUTH")
    if fake and "PYTEST_CURRENT_TEST" not in os.environ:
        raise HTTPException(503, "ADB_BROKER_FAKE_AUTH is set outside pytest — refusing to serve")
    if fake:  # test hook: token -> identity mapping from a local file
        mapping = json.loads(Path(fake).read_text())
        ident = mapping.get(authorization.split(" ", 1)[1])
        if not ident:
            raise HTTPException(401, "unknown test token")
        return ident
    # follow_redirects: the gateway 307s the bare host to the /api/v1 base,
    # and httpx drops the Authorization header only on cross-host redirects.
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        r = await c.get(f"{SAMCLOUD_API}/auth/verify", headers={"Authorization": authorization})
    if r.status_code != 200:
        raise HTTPException(401, f"samcloud rejected token: {r.status_code}")
    return r.json()


def caller_identity(identity: dict) -> str:
    # Agent-type tokens carry username=None — identity lives in owner_id
    # (the unattributable-audit lesson from aws-broker #317).
    return (identity.get("username") or identity.get("owner_id")
            or f"user-{identity.get('user_id') or 'unknown'}")


def is_admin(identity: dict) -> bool:
    return identity.get("role") == "admin" or "*" in (identity.get("scopes") or [])


# ---------------------------------------------------------------- adb plumbing

def adb_env() -> dict:
    env = dict(os.environ)
    env["ADB_SERVER_SOCKET"] = CFG["adb_server_socket"]
    return env


async def server_alive() -> bool:
    """Probe the adb server socket without invoking adb — an adb client
    auto-starts a missing server as the broker's user, which under M1a
    would silently flip server/keystore ownership (the F3 hazard via a
    side door). A raw TCP connect has no such side effect."""
    sock = CFG["adb_server_socket"]
    if not sock.startswith("tcp:"):
        return True  # unknown scheme — don't block, adb will report
    rest = sock[4:]
    host, _, port = rest.rpartition(":")
    host = host or "127.0.0.1"
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=2)
        w.close()
        with contextlib.suppress(Exception):
            await w.wait_closed()
        return True
    except Exception:
        return False


async def run_adb(argv: list, timeout: float, serial: Optional[str] = None):
    """Run one adb command with a hard deadline. Returns (exit, out, err, timed_out)."""
    if not CFG["own_server"] and argv[:1] != ["start-server"] and not await server_alive():
        return 125, b"", (b"adb server is down; the broker will not auto-start it under "
                          b"own_server=false (ownership-flip guard) - escalate on #538"), False
    cmd = [CFG["adb_path"]]
    if serial:
        cmd += ["-s", serial]
    cmd += argv
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=adb_env(), start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out, err, False
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.communicate()
        return 124, b"", b"deadline exceeded", True


def cap(data: bytes) -> tuple:
    limit = CFG["output_cap_bytes"]
    if len(data) > limit:
        return data[:limit].decode(errors="replace"), True
    return data.decode(errors="replace"), False


APK_BADGING_RE = re.compile(r"package: name='([^']+)'.*?versionName='([^']*)'", re.S)


async def apk_identity(apk: Path) -> tuple:
    """(applicationId, versionName) via aapt dump badging."""
    aapt = CFG["aapt_path"]
    if not shutil.which(aapt) and not os.access(aapt, os.X_OK):
        raise HTTPException(503, f"aapt not available at '{aapt}' — set aapt_path in config")
    proc = await asyncio.create_subprocess_exec(
        aapt, "dump", "badging", str(apk),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, "aapt timed out")
    m = APK_BADGING_RE.search(out.decode(errors="replace"))
    if proc.returncode != 0 or not m:
        raise HTTPException(422, f"could not read applicationId from {apk.name}: {err.decode(errors='replace')[:300]}")
    return m.group(1), m.group(2)


# ---------------------------------------------------------------- state

class Lease:
    def __init__(self, holder, requested, purpose, ttl, priority, app_tag, canonical):
        self.id = uuid.uuid4().hex[:12]
        self.holder = holder
        self.requested = requested          # serial | "any"
        self.serial = None                  # set at grant
        self.purpose = purpose
        self.ttl = ttl
        self.priority = bool(priority)
        self.app_tag = app_tag
        self.canonical = bool(canonical)
        self.created_at = time.time()
        self.granted_at = None
        self.expires_at = None
        self.status = "queued"              # queued|granted|released|expired

    def to_dict(self):
        return {k: getattr(self, k) for k in (
            "id", "holder", "requested", "serial", "purpose", "ttl", "priority",
            "app_tag", "canonical", "created_at", "granted_at", "expires_at", "status")}


LEASES: dict = {}          # id -> Lease
DEVICES: dict = {}         # serial -> {state, model, last_seen}
STATE_LOCK = asyncio.Lock()


def persist_state():
    STATE_FILE.write_text(json.dumps({
        "leases": [l.to_dict() for l in LEASES.values()
                   if l.status in ("granted", "queued")],
    }))


def load_state():
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text())
    except Exception:
        return
    now = time.time()
    for d in data.get("leases", []):
        l = Lease(d["holder"], d["requested"], d["purpose"], d["ttl"],
                  d["priority"], d.get("app_tag"), d.get("canonical"))
        for k in ("id", "serial", "created_at", "granted_at", "expires_at", "status"):
            setattr(l, k, d.get(k))
        if l.status == "granted" and (l.expires_at or 0) < now:
            l.status = "expired"
            audit("lease.expired", lease=l.id, holder=l.holder, device=l.serial, at="reload")
            continue
        if l.status in ("granted", "queued"):
            LEASES[l.id] = l


def granted_on(serial: str) -> Optional[Lease]:
    for l in LEASES.values():
        if l.status == "granted" and l.serial == serial:
            return l
    return None


def waiters() -> list:
    ws = [l for l in LEASES.values() if l.status == "queued"]
    return sorted(ws, key=lambda l: (not l.priority, l.created_at))


def queue_position(lease: Lease) -> int:
    ahead = [l for l in waiters()
             if l.created_at < lease.created_at or (l.priority and not lease.priority)]
    return len([l for l in ahead if l.requested in (lease.requested, "any")
                or lease.requested == "any"]) + 1


def free_devices() -> list:
    return [s for s, d in DEVICES.items()
            if d.get("state") == "device" and granted_on(s) is None]


def try_grants():
    """Match queued leases to free devices. Call under STATE_LOCK.
    persist_state() must run on EVERY path — an early return before it once
    dropped queued leases on broker restart (live incident, 2026-08-27)."""
    try:
        for lease in waiters():
            free = free_devices()
            if not free:
                return
            serial = None
            if lease.requested == "any":
                serial = free[0]
            elif lease.requested in free:
                serial = lease.requested
            if serial:
                lease.serial = serial
                lease.status = "granted"
                lease.granted_at = time.time()
                lease.expires_at = lease.granted_at + lease.ttl
                audit("lease.granted", lease=lease.id, holder=lease.holder,
                      device=serial, purpose=lease.purpose, ttl=lease.ttl)
    finally:
        persist_state()


def sweep():
    """Expire granted + queued leases. Call under STATE_LOCK."""
    now = time.time()
    for l in list(LEASES.values()):
        if l.status == "granted" and (l.expires_at or 0) < now:
            l.status = "expired"
            audit("lease.expired", lease=l.id, holder=l.holder, device=l.serial)
        elif l.status == "queued" and l.created_at + CFG["queue_ttl"] < now:
            l.status = "expired"
            audit("lease.queue_expired", lease=l.id, holder=l.holder)
    stop_dead_captures()
    try_grants()


async def poll_devices():
    code, out, _err, _t = await run_adb(["devices", "-l"], timeout=15)
    if code != 0:
        return
    seen = set()
    for line in out.decode(errors="replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = next((p.split(":", 1)[1] for p in parts if p.startswith("model:")), None)
        DEVICES[serial] = {"state": state, "model": model, "last_seen": time.time()}
        seen.add(serial)
    for serial in list(DEVICES):
        if serial not in seen:
            DEVICES[serial]["state"] = "absent"


# ---------------------------------------------------------------- background loops

async def _sweeper_loop():
    while True:
        try:
            await poll_devices()
            async with STATE_LOCK:
                sweep()
        except Exception:
            pass
        await asyncio.sleep(min(5, CFG["device_poll_interval"]))


async def _health_loop():
    if not SAMCLOUD_FILE.exists():
        return
    sc = json.loads(SAMCLOUD_FILE.read_text())
    service_id, token = sc.get("service_id"), sc.get("service_token")
    if not (service_id and token):
        return
    url = f"{SAMCLOUD_API}/services/{service_id}/health"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            with contextlib.suppress(Exception):
                await client.post(url, headers=headers)
            await asyncio.sleep(HEALTH_INTERVAL)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    if CFG["own_server"]:
        # M1b: the broker's own user identity starts and owns the server
        # (and therefore ~/.android/adbkey — the owner-flip problem gone).
        await run_adb(["start-server"], timeout=30)
    tasks = [asyncio.create_task(_sweeper_loop()), asyncio.create_task(_health_loop())]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t


app = FastAPI(title="adb-broker", version="0.2.0", lifespan=lifespan)


# ---------------------------------------------------------------- endpoints

@app.get("/")
async def root():
    return {"service": "adb-broker", "version": "0.1.0", "docs": "/docs"}


@app.get("/health")
async def health():
    code, _o, _e, _t = await run_adb(["devices"], timeout=10)
    return {
        "status": "healthy" if code == 0 else "degraded",
        "adb_server": CFG["adb_server_socket"],
        "own_server": CFG["own_server"],
        "devices": {s: d.get("state") for s, d in DEVICES.items()},
        "granted": len([l for l in LEASES.values() if l.status == "granted"]),
        "queued": len(waiters()),
    }


class LeaseRequest(BaseModel):
    device: str = Field(default="any", description="serial | 'any'")
    ttl: int = Field(default=0, ge=0, description="seconds; 0 -> default")
    purpose: str = Field(..., max_length=200)
    priority: bool = False
    app_tag: Optional[str] = Field(default=None, max_length=64,
                                   description="expected applicationId tag for installs under this lease")
    canonical: bool = Field(default=False,
                            description="request validation-grade rights (canonical applicationId installs)")


@app.post("/leases")
async def create_lease(req: LeaseRequest, authorization: Optional[str] = Header(None)):
    identity = await verify_samcloud(authorization)
    holder = caller_identity(identity)
    ttl = min(req.ttl or CFG["default_lease_ttl"], CFG["max_lease_ttl"])
    if req.canonical and not is_admin(identity):
        # Validation-grade rights ride on the bearer, not on asking nicely.
        # Non-admin canonical use needs an admin-granted lease (M1: file on #538).
        raise HTTPException(403, "canonical (validation-grade) leases require admin scope in M1")
    async with STATE_LOCK:
        await poll_devices()
        if req.device != "any" and req.device not in DEVICES:
            raise HTTPException(404, f"unknown device '{req.device}'. Known: {list(DEVICES)}")
        existing = [l for l in LEASES.values()
                    if l.holder == holder and l.status in ("granted", "queued")]
        if existing:
            l = existing[0]  # idempotent: one live lease per holder in M1
            return {"lease": l.to_dict(),
                    "position": queue_position(l) if l.status == "queued" else 0}
        lease = Lease(holder, req.device, req.purpose, ttl, req.priority,
                      req.app_tag, req.canonical)
        LEASES[lease.id] = lease
        audit("lease.requested", lease=lease.id, holder=holder, requested=req.device,
              purpose=req.purpose, app_tag=req.app_tag, canonical=req.canonical)
        try_grants()
        return {"lease": lease.to_dict(),
                "position": queue_position(lease) if lease.status == "queued" else 0}


@app.get("/leases")
async def list_leases(authorization: Optional[str] = Header(None)):
    await verify_samcloud(authorization)
    return {"leases": [l.to_dict() for l in LEASES.values()
                       if l.status in ("granted", "queued")]}


@app.get("/leases/{lease_id}")
async def get_lease(lease_id: str, authorization: Optional[str] = Header(None)):
    await verify_samcloud(authorization)
    lease = LEASES.get(lease_id)
    if not lease:
        raise HTTPException(404, "no such lease")
    async with STATE_LOCK:
        sweep()
    return {"lease": lease.to_dict(),
            "position": queue_position(lease) if lease.status == "queued" else 0}


@app.post("/leases/{lease_id}/renew")
async def renew_lease(lease_id: str, authorization: Optional[str] = Header(None)):
    identity = await verify_samcloud(authorization)
    holder = caller_identity(identity)
    lease = LEASES.get(lease_id)
    if not lease or lease.status not in ("granted", "queued"):
        raise HTTPException(404, "no live lease")
    if lease.holder != holder and not is_admin(identity):
        raise HTTPException(403, "not your lease")
    async with STATE_LOCK:
        if lease.status == "granted":
            lease.expires_at = time.time() + lease.ttl
        persist_state()
    audit("lease.renewed", lease=lease.id, holder=holder)
    return {"lease": lease.to_dict()}


@app.delete("/leases/{lease_id}")
async def release_lease(lease_id: str, authorization: Optional[str] = Header(None)):
    identity = await verify_samcloud(authorization)
    holder = caller_identity(identity)
    lease = LEASES.get(lease_id)
    if not lease or lease.status not in ("granted", "queued"):
        raise HTTPException(404, "no live lease")
    if lease.holder != holder and not is_admin(identity):
        raise HTTPException(403, "not your lease")
    async with STATE_LOCK:
        lease.status = "released"
        audit("lease.released", lease=lease.id, holder=lease.holder,
              device=lease.serial, by=holder)
        try_grants()
    return {"lease": lease.to_dict()}


@app.get("/version")
async def adb_version(authorization: Optional[str] = Header(None)):
    """Raw `adb version` text — server-level and read-only, so no lease."""
    await verify_samcloud(authorization)
    code, out, err, _t = await run_adb(["version"], timeout=10)
    return {"exit": code, "text": out.decode(errors="replace") or err.decode(errors="replace")}


@app.get("/device-state")
async def device_state(serial: Optional[str] = None, authorization: Optional[str] = Header(None)):
    """`adb get-state` without a lease — it's a pure query, and leasing it
    was a trap: `devices`/`version` are lease-free, so operators probing
    "is the device there?" left silent 10-minute holds behind (#538 c4175)."""
    await verify_samcloud(authorization)
    if not serial:
        online = [s for s, d in DEVICES.items() if d.get("state") != "absent"]
        if len(online) != 1:
            return {"exit": 1, "text": "", "error": "error: more than one device/emulator"
                    if online else "error: no devices/emulators found"}
        serial = online[0]
    code, out, err, _t = await run_adb(["get-state"], timeout=10, serial=serial)
    return {"exit": code, "text": out.decode(errors="replace"),
            "error": err.decode(errors="replace")}


@app.get("/devices")
async def list_devices(authorization: Optional[str] = Header(None)):
    await verify_samcloud(authorization)
    await poll_devices()
    out = {}
    for serial, d in DEVICES.items():
        g = granted_on(serial)
        out[serial] = dict(d, lease=(g.to_dict() if g else None),
                           queue_depth=len([w for w in waiters()
                                            if w.requested in (serial, "any")]))
    return {"devices": out}


# ---------------------------------------------------------------- exec

class ExecRequest(BaseModel):
    lease_id: str
    argv: list
    timeout_s: int = Field(default=0, ge=0)
    package: Optional[str] = Field(default=None, description="applicationId hint for install")
    no_backup: bool = Field(default=False, description="admin-only: skip displaced-APK backup")


async def _backup_installed(serial: str, package: str, lease: Lease) -> Optional[str]:
    """Pull every APK of an installed package before it gets displaced.
    Returns the backup dir path, or None if the package is not installed.

    Probe with `pm list packages` (exit 0 whether or not present) so a
    transport failure is distinguishable from genuinely-not-installed —
    a flaky `pm path` must abort, not silently skip the backup (review F1;
    the b130 class). No verify, no displace."""
    code, out, _e, timed = await run_adb(["shell", "pm", "list", "packages", package],
                                         timeout=30, serial=serial)
    if code != 0 or timed:
        raise HTTPException(409, f"cannot verify installed state of {package} "
                                 f"(pm list exit {code}{', timed out' if timed else ''}) "
                                 f"— no backup, no displace")
    installed = any(l.strip() == f"package:{package}"
                    for l in out.decode(errors="replace").splitlines())
    if not installed:
        return None
    code, out, _e, timed = await run_adb(["shell", "pm", "path", package], timeout=30, serial=serial)
    paths = [l.split(":", 1)[1].strip() for l in out.decode(errors="replace").splitlines()
             if l.startswith("package:")]
    if code != 0 or timed or not paths:
        raise HTTPException(409, f"cannot resolve APK paths for installed {package} "
                                 f"— no backup, no displace")
    version = "unknown"
    code, out, _e, _t = await run_adb(["shell", "dumpsys", "package", package], timeout=30, serial=serial)
    m = re.search(r"versionName=(\S+)", out.decode(errors="replace"))
    if m:
        version = m.group(1)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = Path(CFG["backups_dir"]) / package / f"{version}_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    metas = []
    for remote in paths:
        local = dest / Path(remote).name
        code, _o, err, timed = await run_adb(["pull", remote, str(local)], timeout=300, serial=serial)
        if code != 0 or timed or not local.exists():
            shutil.rmtree(dest, ignore_errors=True)
            raise HTTPException(409, f"backup pull of {remote} failed "
                                     f"({err.decode(errors='replace')[:200]}) — no backup, no uninstall")
        metas.append({"apk": local.name, "bytes": local.stat().st_size,
                      "sha256": hashlib.sha256(local.read_bytes()).hexdigest()})
    (dest / "meta.json").write_text(json.dumps({
        "package": package, "versionName": version, "device": serial,
        "lease": lease.id, "holder": lease.holder, "ts": int(time.time()),
        "apks": metas}, indent=2))
    _prune_backups()
    return str(dest)


def _prune_backups():
    root = Path(CFG["backups_dir"])
    if not root.exists():
        return
    quota = CFG["backups_max_gb"] * (1 << 30)
    dirs = sorted([d for d in root.glob("*/*") if d.is_dir()], key=lambda d: d.stat().st_mtime)
    total = sum(f.stat().st_size for d in dirs for f in d.rglob("*") if f.is_file())
    while total > quota and len(dirs) > 1:
        victim = dirs.pop(0)
        size = sum(f.stat().st_size for f in victim.rglob("*") if f.is_file())
        shutil.rmtree(victim, ignore_errors=True)
        total -= size
        audit("backup.pruned", path=str(victim), bytes=size)


@app.get("/backups")
async def list_backups(package: Optional[str] = None, authorization: Optional[str] = Header(None)):
    await verify_samcloud(authorization)
    root = Path(CFG["backups_dir"])
    out = []
    for meta in sorted(root.glob("*/*/meta.json")):
        with contextlib.suppress(Exception):
            m = json.loads(meta.read_text())
            if not package or m.get("package") == package:
                m["path"] = str(meta.parent)
                out.append(m)
    return {"backups": out}


@app.post("/exec")
async def exec_adb(req: ExecRequest, authorization: Optional[str] = Header(None)):
    identity = await verify_samcloud(authorization)
    holder = caller_identity(identity)
    admin = is_admin(identity)

    lease = LEASES.get(req.lease_id)
    if not lease:
        raise HTTPException(404, "no such lease")
    async with STATE_LOCK:
        sweep()
    if lease.status != "granted":
        raise HTTPException(409, f"lease is {lease.status}"
                            + (f" (position {queue_position(lease)})" if lease.status == "queued" else ""))
    if lease.holder != holder and not admin:
        raise HTTPException(403, "not your lease")

    argv = [str(a) for a in req.argv]
    if not argv:
        raise HTTPException(400, "empty argv")
    verb = next((a for a in argv if not a.startswith("-")), argv[0])
    # Global transport flags precede the verb; the same letters after the
    # verb are subcommand flags (logcat -t, install -t) and stay legal.
    if any(a in FORBIDDEN_FLAGS for a in argv[:argv.index(verb)]):
        raise HTTPException(400, f"client-side transport flags are set by the broker: {sorted(FORBIDDEN_FLAGS)}")
    if verb in GUARDED_VERBS and not admin:
        audit("exec.guarded_refused", lease=lease.id, holder=holder, argv=argv)
        raise HTTPException(403, f"'{verb}' reshapes the shared adb server/transport — admin only "
                                 f"(the 12:20 incident class). File on #538 if you need it.")
    if verb == "shell" and len([a for a in argv if not a.startswith("-")]) == 1:
        raise HTTPException(400, "interactive 'adb shell' is not supported — pass a command")

    timeout = min(req.timeout_s or CFG["default_exec_timeout"], CFG["max_exec_timeout"])
    warnings = []
    backup_path = None
    apk_id = None

    # wait-for-* becomes a bounded poll — never the raw unbounded verb.
    if verb.startswith("wait-for"):
        deadline = time.time() + timeout
        state = None
        while time.time() < deadline:
            _c, out, _e, _t = await run_adb(["get-state"], timeout=10, serial=lease.serial)
            state = out.decode(errors="replace").strip()
            if state == "device":
                break
            await asyncio.sleep(2)
        timed_out = state != "device"
        audit("exec", lease=lease.id, holder=holder, device=lease.serial, verb=verb,
              argv=argv, exit=(1 if timed_out else 0), timed_out=timed_out)
        if timed_out:
            raise HTTPException(504, f"device not ready within {timeout}s (state={state or 'unknown'})")
        # Real adb wait-for-device prints nothing on success — stay
        # bit-compatible with harnesses that parse the output (#538 c4165).
        return {"exit": 0, "stdout": "", "stderr": "", "timed_out": False,
                "warnings": warnings}

    # Streaming logcat would hold the lease forever; force the dump form.
    if verb == "logcat" and not ({"-d", "-t"} & set(argv)):
        argv.insert(argv.index(verb) + 1, "-d")
        warnings.append("logcat forced to -d (bounded); use -t <n> for tails")

    if verb in ("install", "install-multiple"):
        apk_args = [a for a in argv if a.endswith(".apk")]
        if not apk_args:
            raise HTTPException(400, "install requires an .apk path")
        apk = Path(apk_args[0])  # base APK carries the applicationId
        if not apk.is_file() or not os.access(apk, os.R_OK):
            raise HTTPException(400, f"broker cannot read {apk} — stage it under {CFG['staging_dir']} "
                                     f"(samadb does this automatically)")
        apk_id, apk_version = await apk_identity(apk)
        if req.package and req.package != apk_id:
            warnings.append(f"package hint {req.package} != APK id {apk_id}; trusting the APK")
        # The device door (c4149): canonical ids need validation-grade rights.
        if apk_id in CFG["canonical_ids"] and not lease.canonical:
            audit("exec.canonical_refused", lease=lease.id, holder=holder, apk_id=apk_id)
            raise HTTPException(403, f"'{apk_id}' is a canonical applicationId on this box — "
                                     f"install needs a validation-grade (canonical) lease")
        if lease.app_tag and lease.app_tag not in apk_id:
            warnings.append(f"APK id '{apk_id}' does not carry this lease's app_tag "
                            f"'{lease.app_tag}' (M1: warn only)")
        if not req.no_backup:
            backup_path = await _backup_installed(lease.serial, apk_id, lease)
        elif not admin:
            raise HTTPException(403, "no_backup is admin-only (the b130 lesson: no backup, no displace)")

    if verb == "uninstall":
        pkg = next((a for a in reversed(argv) if not a.startswith("-") and a != verb), None)
        if not pkg:
            raise HTTPException(400, "uninstall requires a package id")
        if not req.no_backup:
            backup_path = await _backup_installed(lease.serial, pkg, lease)
        elif not admin:
            raise HTTPException(403, "no_backup is admin-only (the b130 lesson: no backup, no displace)")

    start = time.time()
    code, out, err, timed_out = await run_adb(argv, timeout=timeout, serial=lease.serial)
    stdout, out_trunc = cap(out)
    stderr, err_trunc = cap(err)
    audit("exec", lease=lease.id, holder=holder, device=lease.serial, verb=verb,
          argv=argv, exit=code, duration_ms=int((time.time() - start) * 1000),
          timed_out=timed_out, backup=backup_path, apk_id=apk_id,
          warnings=warnings or None)
    return {"exit": code, "stdout": stdout, "stderr": stderr, "timed_out": timed_out,
            "stdout_truncated": out_trunc, "stderr_truncated": err_trunc,
            "backup": backup_path, "apk_id": apk_id, "warnings": warnings}


# ---------------------------------------------------------------- logcat captures
#
# Streaming logcat doesn't fit the bounded-exec model (cold-boot sweeps run
# for minutes), so the broker runs the stream itself, bound to the lease:
# renewals keep it alive, release/expiry stops it. Never unbounded.

CAPTURES: dict = {}  # id -> {lease, holder, device, proc, file, started, stopped}


def _capture_dict(c):
    return {"id": c["id"], "lease": c["lease"], "holder": c["holder"],
            "device": c["device"], "file": c["file"], "started": c["started"],
            "stopped": c.get("stopped"),
            "bytes": Path(c["file"]).stat().st_size if Path(c["file"]).exists() else 0,
            "running": c["proc"].returncode is None if c.get("proc") else False}


def _stop_capture(c, reason):
    proc = c.get("proc")
    if proc and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    c["stopped"] = time.time()
    audit("logcat.stopped", capture=c["id"], lease=c["lease"], holder=c["holder"],
          file=c["file"], reason=reason)


def stop_dead_captures():
    """Stop captures whose lease is no longer granted. Called from sweep()."""
    for c in CAPTURES.values():
        if c.get("stopped"):
            continue
        lease = LEASES.get(c["lease"])
        if not lease or lease.status != "granted":
            _stop_capture(c, "lease_" + (lease.status if lease else "gone"))


class CaptureRequest(BaseModel):
    lease_id: str
    argv: list = Field(default_factory=list, description="extra logcat args, e.g. ['-v','time']")


@app.post("/logcat/start")
async def logcat_start(req: CaptureRequest, authorization: Optional[str] = Header(None)):
    identity = await verify_samcloud(authorization)
    holder = caller_identity(identity)
    lease = LEASES.get(req.lease_id)
    async with STATE_LOCK:
        sweep()
    if not lease or lease.status != "granted":
        raise HTTPException(409, "capture needs a granted lease")
    if lease.holder != holder and not is_admin(identity):
        raise HTTPException(403, "not your lease")
    live = [c for c in CAPTURES.values()
            if c["lease"] == lease.id and not c.get("stopped")]
    if live:
        return {"capture": _capture_dict(live[0]), "note": "already running"}
    argv = [str(a) for a in req.argv]
    bad = {"-f", "-d", "-c", "--clear"} & set(argv)
    if bad:
        raise HTTPException(400, f"not valid in a capture: {sorted(bad)}")
    cap_id = uuid.uuid4().hex[:12]
    cap_dir = Path(CFG["captures_dir"])
    cap_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(cap_dir, 0o1777)
    out_file = cap_dir / f"{cap_id}.log"
    fh = out_file.open("wb")
    os.chmod(out_file, 0o644)
    proc = await asyncio.create_subprocess_exec(
        CFG["adb_path"], "-s", lease.serial, "logcat", *argv,
        stdout=fh, stderr=asyncio.subprocess.STDOUT,
        env=adb_env(), start_new_session=True,
    )
    fh.close()
    c = {"id": cap_id, "lease": lease.id, "holder": lease.holder,
         "device": lease.serial, "proc": proc, "file": str(out_file),
         "started": time.time()}
    CAPTURES[cap_id] = c
    audit("logcat.started", capture=cap_id, lease=lease.id, holder=lease.holder,
          device=lease.serial, argv=argv, file=str(out_file))
    return {"capture": _capture_dict(c),
            "note": "bound to the lease — keep renewing; release/expiry stops it"}


@app.post("/logcat/{capture_id}/stop")
async def logcat_stop(capture_id: str, authorization: Optional[str] = Header(None)):
    identity = await verify_samcloud(authorization)
    holder = caller_identity(identity)
    c = CAPTURES.get(capture_id)
    if not c:
        raise HTTPException(404, "no such capture")
    if c["holder"] != holder and not is_admin(identity):
        raise HTTPException(403, "not your capture")
    if not c.get("stopped"):
        _stop_capture(c, "requested")
    return {"capture": _capture_dict(c)}


@app.get("/logcat")
async def logcat_list(authorization: Optional[str] = Header(None)):
    await verify_samcloud(authorization)
    return {"captures": [_capture_dict(c) for c in CAPTURES.values()]}


@app.get("/audit/recent")
async def recent_audit(limit: int = 100, authorization: Optional[str] = Header(None)):
    await verify_samcloud(authorization)
    if not AUDIT_LOG.exists():
        return []
    lines = AUDIT_LOG.read_text().splitlines()[-min(limit, 1000):]
    return [json.loads(l) for l in lines]


RECOVER_LOCK = asyncio.Lock()


class RecoverRequest(BaseModel):
    reason: str = Field(..., max_length=200, description="what looked wedged (audited)")


@app.post("/server/recover")
async def server_recover(req: RecoverRequest, authorization: Optional[str] = Header(None)):
    """Arbitrated replacement for seat-side `adb kill-server; adb start-server`
    recovery loops (unity-batch's daemon-glitch path). Any authenticated seat
    may ask; the broker probes first and restarts only a genuinely unhealthy
    server it owns — a healthy one is never bounced, and under M1a
    (own_server=false) recovery escalates instead of flipping ownership."""
    identity = await verify_samcloud(authorization)
    holder = caller_identity(identity)
    async with RECOVER_LOCK:
        healthy = False
        if await server_alive():
            code, _o, _e, timed = await run_adb(["devices"], timeout=10)
            healthy = code == 0 and not timed
        if healthy:
            audit("server.recover_noop", by=holder, reason=req.reason)
            return {"status": "healthy", "action": "none",
                    "note": "server answered the probe — nothing restarted"}
        if not CFG["own_server"]:
            audit("server.recover_refused", by=holder, reason=req.reason)
            raise HTTPException(409, "adb server is unhealthy but the broker does not own it "
                                     "(own_server=false) — restarting would flip server/keystore "
                                     "ownership (the M1b cutover); escalate on #538")
        await run_adb(["kill-server"], timeout=30)
        code, _o, err, _t = await run_adb(["start-server"], timeout=30)
        ok = code == 0
        audit("server.recovered" if ok else "server.recover_failed",
              by=holder, reason=req.reason, exit=code)
        if not ok:
            raise HTTPException(502, f"server restart failed: {err.decode(errors='replace')[:200]}")
        return {"status": "recovered", "action": "restart"}


class ServerAction(BaseModel):
    action: str = Field(..., description="restart | kill")


@app.post("/admin/server")
async def admin_server(req: ServerAction, authorization: Optional[str] = Header(None)):
    identity = await verify_samcloud(authorization)
    if not is_admin(identity):
        raise HTTPException(403, "admin only")
    if req.action not in ("restart", "kill"):
        raise HTTPException(400, "action must be restart|kill")
    if req.action == "restart" and not CFG["own_server"]:
        # Under M1a start-server would run as the broker's user and silently
        # flip server+adbkey ownership — that IS the M1b cutover (review F3),
        # never a side effect. kill is still available to an admin.
        raise HTTPException(409, "restart under own_server=false would flip adb server "
                                 "ownership to the broker's user (the M1b cutover) — "
                                 "set own_server=true deliberately first")
    await run_adb(["kill-server"], timeout=30)
    if req.action == "restart":
        await run_adb(["start-server"], timeout=30)
    audit("server." + req.action, identity=caller_identity(identity))
    return {"status": req.action}
