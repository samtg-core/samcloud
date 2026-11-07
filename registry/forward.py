"""Port forward management — sets up SSH local forwards inside the sshd container.

The registry container can't host these forwards itself: reverse-tunnel ports
land on the sshd container's loopback (127.0.0.1) because devices request them
without GatewayPorts, so they're unreachable from any other container. The
forwards have to live in sshd's network namespace.

Flow:
  1. Registry container ssh's into the sshd container as `samcloud` using the
     gateway key (authorized via /etc/ssh/keys/control_authorized_keys — written
     by sshd-entrypoint.sh, no ForceCommand wrapper).
  2. Inside sshd, spawn `ssh -fN -L 0.0.0.0:slice_port:localhost:service_port
     -p tunnel_port <device_ssh_user>@localhost`. The -L listener binds sshd's
     0.0.0.0:slice_port, reachable from caddy via docker DNS (`sshd:slice_port`).
  3. caddy_target is built as http://<FORWARD_SSH_HOST>:slice_port — caddy hits
     the sshd container directly.

Env vars (set in deploy/docker-compose.yml):
  FORWARD_SSH_HOST  — name of the sshd container on the docker network (default: sshd)
  FORWARD_SSH_USER  — user to log into sshd as (default: samcloud)
  FORWARD_SSH_KEY   — private key for control-plane ssh (default: ~/.ssh/samcloud)
"""

import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

import db
from db import audit

# --- Host-key verification for the two hops this module makes (#822) ---
#
# Outer hop, registry container -> sshd container. The anchor is the PUBLIC half of
# the sshd container's host key, readable at SSHD_HOST_KEYS_DIR because
# deploy/docker-compose.yml mounts the `sshd-hostpub` volume here read-only.
# sshd-entrypoint.sh copies the two .pub files into it by name on every boot, so the
# anchor follows a regenerated key and there is nothing to clear.
#
# Not the `sshd-keys` volume, which is where the PRIVATE host keys live. Mounting
# that here, even read-only, would let a compromised registry present itself AS the
# gateway to every device that dials in — the impersonation the published gateway
# host key is meant to prevent. The registry needs the public half and nothing more.
#
# Inner hop, inside sshd -> a device through its reverse tunnel. Same hop the
# gateway makes, so it uses the same anchor: devices.host_pubkey, keyed on the
# device id via HostKeyAlias. This module only reads that pin — the gateway owns
# establishing it.
#
# Both degrade rather than refuse when their anchor is missing, which is the
# opposite of the choice a gate should make and deliberate here: these calls are
# how every service forward is established, and failing closed on a missing mount
# or an unpinned device would take working forwards down across the fleet. A
# missing anchor is a misconfiguration or a device the gateway has not reached
# yet, not evidence of an attack. Each case logs what is missing and what to do.

SSHD_HOST_KEYS_DIR = os.environ.get("SSHD_HOST_KEYS_DIR", "/sshd-keys")
_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Log a configuration gap once per process, not once per forward."""
    if key not in _warned:
        _warned.add(key)
        print(f"[forward] {message}", flush=True)


def _warn_clear(key: str) -> None:
    """Re-arm a warning once its cause is gone.

    sshd starts after the registry (`depends_on`), so the first probe after a deploy
    can run before sshd has published its host key — one "cannot verify" line, then
    it appears. Without this, that transient line is the last word for the life of
    the process and reads as a standing misconfiguration. Re-arming also means a
    later disappearance is reported rather than swallowed.
    """
    _warned.discard(key)


def _ssh_key() -> str:
    return os.environ.get("FORWARD_SSH_KEY", str(Path.home() / ".ssh" / "samcloud"))


def sshd_host_pubkeys() -> list[str]:
    """Every published host key of the sshd container, as `<type> <base64>`.

    Both types, so a device can pin whichever the gateway negotiates and a gateway
    that drops one does not strand it. Read from the shared volume on every call: the
    files are the anchor, so a regenerated key is followed with no pin to clear.
    """
    out = []
    for name in ("ssh_host_ed25519_key.pub", "ssh_host_rsa_key.pub"):
        path = Path(SSHD_HOST_KEYS_DIR) / name
        try:
            fields = path.read_text().split()
        except OSError:
            continue
        if len(fields) >= 2:
            out.append(f"{fields[0]} {fields[1]}")
    return out


def sshd_host_pubkey() -> str | None:
    """The sshd container's host key, as `<type> <base64>`, or None.

    Read from the shared volume on every call: the file is the anchor, so a
    regenerated host key is picked up with no pin to clear.
    """
    keys = sshd_host_pubkeys()
    return keys[0] if keys else None


def _outer_hostkey_args(tmpdir: str) -> list[str]:
    """ssh options that verify the sshd container, or the old unchecked pair."""
    pubkey = sshd_host_pubkey()
    if not pubkey:
        _warn_once(
            "outer",
            f"cannot verify the sshd container's host key: no readable key in "
            f"{SSHD_HOST_KEYS_DIR}. Mount the sshd-keys volume here read-only "
            f"(deploy/docker-compose.yml) — connecting unverified.",
        )
        return ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]

    _warn_clear("outer")
    known_hosts = Path(tmpdir) / "known_hosts"
    known_hosts.write_text(f"samcloud-sshd {pubkey}\n")
    return [
        "-o", "HostKeyAlias=samcloud-sshd",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "HashKnownHosts=no",
    ]


def probe_hostkey_args() -> list[str]:
    """Host-key options for a one-off probe of the sshd container.

    Same anchor as `_outer_ssh`, but the caller has no temp dir to hang the
    known_hosts file on, so this owns a fixed one and rewrites it each call —
    which also means a regenerated sshd host key is picked up on the next probe.
    """
    pubkey = sshd_host_pubkey()
    if not pubkey:
        _warn_once(
            "probe",
            f"cannot verify the sshd container's host key for the readiness probe: "
            f"no readable key in {SSHD_HOST_KEYS_DIR} — probing unverified.",
        )
        return ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]

    _warn_clear("probe")
    known_hosts = Path(tempfile.gettempdir()) / "samcloud-sshd-known-hosts"
    try:
        known_hosts.write_text(f"samcloud-sshd {pubkey}\n")
    except OSError:
        return ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    return [
        "-o", "HostKeyAlias=samcloud-sshd",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "HashKnownHosts=no",
    ]


def device_host_pubkey(device_id: str) -> str | None:
    """The pin the gateway holds for a device, or None if it has none yet."""
    if not device_id:
        return None
    try:
        row = db.query_one("SELECT host_pubkey FROM devices WHERE id = ?", (device_id,))
    except Exception:
        return None
    if not row:
        return None
    pubkey = (row.get("host_pubkey") or "").strip()
    return pubkey or None


def _ssh_user() -> str:
    return os.environ.get("FORWARD_SSH_USER", "samcloud")


def _ssh_host() -> str:
    """Host where reverse tunnels terminate. In Docker, this is the sshd container."""
    return os.environ.get("FORWARD_SSH_HOST", "localhost")


def _outer_ssh(remote_cmd: str, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a command inside the sshd container via control-plane ssh.

    The sshd container's host key is verified against the shared `sshd-keys`
    volume. The known_hosts file is written per call and removed with the temp
    dir, so nothing accumulates and concurrent calls share no state.
    """
    with tempfile.TemporaryDirectory(prefix="samcloud-forward-") as tmpdir:
        return subprocess.run(
            [
                "ssh",
                *_outer_hostkey_args(tmpdir),
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5",
                "-o", "IdentitiesOnly=yes",
                "-i", _ssh_key(),
                f"{_ssh_user()}@{_ssh_host()}",
                remote_cmd,
            ],
            capture_output=True, text=True, timeout=timeout,
        )


def _is_listening_on_sshd(slice_port: int) -> bool:
    """Check whether slice_port is bound inside the sshd container."""
    # ss isn't installed in the sshd image; parse /proc/net/tcp directly.
    # Lines look like: "   0: 00000000:4682 00000000:0000 0A ..."
    # local_address (col 2): ":HEX_PORT$"; state (col 4): "0A" = LISTEN.
    hex_port = f"{slice_port:04X}"
    cmd = (
        f"grep -qE '^[[:space:]]*[0-9]+:[[:space:]]+[0-9A-F]+:{hex_port}"
        f"[[:space:]]+[0-9A-F:]+[[:space:]]+0A' /proc/net/tcp"
    )
    try:
        return _outer_ssh(cmd, timeout=8).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def start_forward(
    slice_port: int,
    service_port: int,
    tunnel_port: int,
    service_id: str,
    device_ssh_user: str,
) -> tuple[bool, str]:
    """Start an SSH local forward inside sshd: slice_port → service_port via tunnel.

    Args:
        slice_port:      Port on sshd to listen on (caddy targets this)
        service_port:    Port the service runs on the device
        tunnel_port:     Device's reverse-tunnel landing port inside sshd
        service_id:      For audit trail
        device_ssh_user: OS user on the device to ssh as (from device.outbound_ssh_user)

    Returns:
        (success, message)
    """
    if not device_ssh_user:
        return False, "device_ssh_user required (device.outbound_ssh_user is null?)"

    if _is_listening_on_sshd(slice_port):
        return True, f"Port {slice_port} already bound on sshd"

    # Inner ssh runs inside the sshd container. The gateway key is mounted at
    # /etc/ssh/keys/gateway by sshd-entrypoint.sh; the device's authorized_keys
    # for `device_ssh_user` includes this pubkey (added by bootstrap.sh).
    #
    # This is the gateway's own hop — sshd to a device through its reverse tunnel —
    # so it verifies against the gateway's anchor: devices.host_pubkey, keyed on the
    # device id. `service_id` is `<device_id>/<name>`, which is where the id comes
    # from. Without a pin the hop stays unverified and says so; the gateway pins the
    # device the next time a seat reaches it, and this forward is verified after.
    device_id = service_id.split("/")[0] if service_id else ""
    pinned = device_host_pubkey(device_id)

    stage_known_hosts = ""
    cleanup_known_hosts = ""
    if pinned:
        alias = f"samcloud-{device_id}"
        kh = f"/tmp/samcloud-kh-{slice_port}"
        # printf with a quoted single argument: the pin is validated on the way into
        # the registry, and shlex.quote keeps it one shell word regardless.
        stage_known_hosts = (
            f"printf '%s\\n' {shlex.quote(f'{alias} {pinned}')} > {kh} && "
        )
        # Keep ssh's exit status: a bare `ssh ...; rm ...` would report rm's, so a
        # failed inner ssh would look like a started forward.
        cleanup_known_hosts = f"; rc=$?; rm -f {kh}; exit $rc"
        hostkey_opts = (
            f"-o HostKeyAlias={shlex.quote(alias)} "
            "-o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={kh} "
            "-o HashKnownHosts=no "
        )
    else:
        _warn_once(
            f"device:{device_id}",
            f"{device_id or 'device'} has no pinned host key, so this forward is "
            f"unverified; the gateway pins it on the next hop "
            f"(GET /api/v1/devices/{device_id or '<id>'} → host_pubkey).",
        )
        hostkey_opts = (
            "-o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null "
        )

    inner_ssh = (
        stage_known_hosts
        + "ssh -fN "
        + hostkey_opts
        + "-o ExitOnForwardFailure=yes "
        "-o ServerAliveInterval=15 "
        "-o ServerAliveCountMax=3 "
        "-o ConnectTimeout=5 "
        "-o IdentitiesOnly=yes "
        "-i /etc/ssh/keys/gateway "
        f"-L 0.0.0.0:{slice_port}:localhost:{service_port} "
        f"-p {tunnel_port} "
        f"{shlex.quote(device_ssh_user)}@localhost"
        # ssh -fN forks only after the forward is established, so known_hosts has
        # already been read by the time this runs.
        + cleanup_known_hosts
    )

    try:
        spawn = _outer_ssh(inner_ssh, timeout=20)
        if spawn.returncode != 0:
            return False, f"Inner ssh failed: {(spawn.stderr or spawn.stdout).strip()}"

        # Verify listener is up — inner ssh exits 0 immediately due to -fN, so
        # we poll briefly to confirm it actually bound the port.
        for _ in range(6):
            time.sleep(0.5)
            if _is_listening_on_sshd(slice_port):
                audit("system:forward", "forward.started", service_id,
                      {"slice_port": slice_port, "service_port": service_port,
                       "tunnel_port": tunnel_port, "device_ssh_user": device_ssh_user})
                return True, f"Forward sshd:{slice_port} → tunnel:{tunnel_port} → :{service_port}"

        return False, f"Inner ssh spawned but :{slice_port} not listening on sshd after 3s"

    except subprocess.TimeoutExpired:
        return False, "SSH outer call timed out"
    except FileNotFoundError:
        return False, "ssh binary not found in registry container"


def stop_forward(slice_port: int, service_id: str) -> tuple[bool, str]:
    """Kill the inner ssh -fN process inside sshd that holds slice_port."""
    # Match on the -L bind we wrote; pgrep -af inspects full argv.
    find_kill = (
        f"pgrep -af 'ssh -fN.* -L 0.0.0.0:{slice_port}:' | "
        f"awk '{{print $1}}' | xargs -r kill 2>/dev/null; "
        f"echo done"
    )
    try:
        _outer_ssh(find_kill, timeout=10)
        audit("system:forward", "forward.stopped", service_id, {"slice_port": slice_port})
        return True, f"Stopped forward(s) on sshd:{slice_port}"
    except subprocess.TimeoutExpired:
        return False, "SSH outer call timed out"
    except Exception as e:
        return False, f"Failed to stop forward: {e}"


def reconcile_all(desired: list[dict]) -> dict:
    """Restart any forwards from `desired` that aren't currently listening.

    Called on registry startup. `desired` is a list of dicts:
        {slice_port, service_port, tunnel_port, service_id, device_ssh_user}

    Returns a summary dict for logging.
    """
    started = []
    skipped = []
    failed = []
    for d in desired:
        if not d.get("tunnel_port") or not d.get("slice_port"):
            continue
        if _is_listening_on_sshd(d["slice_port"]):
            skipped.append(d["service_id"])
            continue
        ok, msg = start_forward(
            slice_port=d["slice_port"],
            service_port=d["service_port"],
            tunnel_port=d["tunnel_port"],
            service_id=d["service_id"],
            device_ssh_user=d["device_ssh_user"],
        )
        (started if ok else failed).append({"service_id": d["service_id"], "msg": msg})
    return {"started": started, "skipped": skipped, "failed": failed}
