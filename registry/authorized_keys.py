"""Manage ~/.ssh/authorized_keys from registry access keys.

When an access key is generated or revoked, this module syncs
the authorized_keys file to match the registry state.

Each managed key gets a ForceCommand pointing to the gateway,
with the user and device as arguments.
"""

import os
from pathlib import Path

import db

AUTHORIZED_KEYS_PATH = Path(os.environ.get(
    "AUTHORIZED_KEYS_PATH",
    str(Path.home() / ".ssh" / "authorized_keys")
))
GATEWAY_BINARY = os.environ.get("GATEWAY_BINARY", "/usr/local/bin/samcloud-gateway")
# GATEWAY_KEY: where the key is stored (registry writes here)
# GATEWAY_KEY_FORCECOMMAND: path written into ForceCommand (must exist in sshd container)
GATEWAY_KEY = os.environ.get("GATEWAY_KEY", "/var/lib/samcloud/gateway")
GATEWAY_KEY_FC = os.environ.get("GATEWAY_KEY_FORCECOMMAND", GATEWAY_KEY)
MANAGED_MARKER = "# samcloud-managed"
# The shape of the managed lines. Bump it whenever their options change, together
# with KEYS_POLICY in deploy/sshd-entrypoint.sh (a test holds them equal). sshd
# starts only once the file carries this stamp, so a release that changes the
# options never serves the old lines, and the entrypoint change recreates sshd,
# which ends every session authenticated under them.
KEYS_POLICY = 2
POLICY_LINE = f"{MANAGED_MARKER} keys-policy={KEYS_POLICY}"
# Gateway needs to know which registry to query
REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://localhost:7777")
# Gateway token for authenticated registry queries (sees tunnel_port, ssh_user)
GATEWAY_TOKEN_PATH = os.environ.get("GATEWAY_TOKEN_PATH",
    str(Path.home() / ".local" / "samcloud" / "staging" / "keys" / "gateway_token"))


def sync():
    """Sync authorized_keys with registry access keys.

    Preserves any non-managed keys (lines without the samcloud marker).
    Replaces all managed keys with current registry state.
    """
    # Read existing file
    existing_lines = []
    if AUTHORIZED_KEYS_PATH.exists():
        existing_lines = AUTHORIZED_KEYS_PATH.read_text().splitlines()

    # Separate managed vs unmanaged lines
    unmanaged = [line for line in existing_lines
                 if MANAGED_MARKER not in line and line.strip()]

    # Get all access keys from registry (user SSH keys → gateway)
    keys = db.query(
        "SELECT id, owner_id, scopes FROM tokens WHERE type = 'access_key'"
    )

    # Build managed lines
    managed = []
    for key in keys:
        import json
        scopes = json.loads(key["scopes"]) if key.get("scopes") else {}
        pubkey = scopes.get("pubkey", "")
        device = scopes.get("device", "unknown")
        username = key["owner_id"]

        if not pubkey:
            continue

        # ForceCommand restricts to gateway with user+device identity
        # restrict disables everything, then we re-enable what's needed:
        # pty = interactive terminal. No port-forwarding: the gateway runs the
        # user's command on the device, and nothing a user does needs -L or -R
        # here. With it, an access key could -R bind any free port in sshd (an
        # offline device's tunnel port, say) and stand in for that device.
        # REGISTRY_URL env tells gateway which registry to query
        # Read gateway token from file (persistent across restarts)
        gw_token = ""
        try:
            gw_token_path = Path(GATEWAY_TOKEN_PATH)
            if gw_token_path.exists():
                gw_token = gw_token_path.read_text().strip()
        except Exception:
            pass

        gw_token_env = f" GATEWAY_TOKEN={gw_token}" if gw_token else ""
        line = (
            f'command="REGISTRY_URL={REGISTRY_URL}{gw_token_env} GATEWAY_KEY={GATEWAY_KEY_FC} {GATEWAY_BINARY} {username} {device}",'
            f'restrict,pty '
            f'{pubkey} '
            f'{MANAGED_MARKER}'
        )
        managed.append(line)

    # Get all device tunnel keys (devices with ssh_pubkey → tunnel-only access)
    devices = db.query(
        "SELECT id, ssh_pubkey, tunnel_port FROM devices "
        "WHERE ssh_pubkey IS NOT NULL AND ssh_pubkey != ''"
    )
    for dev in devices:
        pubkey = dev["ssh_pubkey"].strip()
        if not pubkey:
            continue
        tunnel_port = dev.get("tunnel_port")
        # Device keys: restrict to tunnel forwarding only (no shell, no pty)
        # permitlisten allows reverse tunnel binding on their assigned port
        #
        # `restrict` disables PTY, agent, X11 and user-rc -- it does NOT disable
        # command execution (#757). Without a forced command these keys get
        # arbitrary one-shot exec in the gateway container, which is 35 of the
        # 54 keys on the live file. `command="/bin/false"` closes that; the
        # user keys above have carried the same shape since they were written.
        #
        # Tunnels are unaffected: devices dial with `ssh -N`, which requests no
        # session channel, so the forced command is never executed -- and
        # port-forwarding and permitlisten are options, not commands. Asserted
        # by TestAuthorizedKeysForcedCommand.
        #
        # Forwarding only with a tunnel port: `port-forwarding` without a
        # permitlisten lets the key -R bind any free port, so a device with no
        # tunnel gets none. Local forwards (-L, -W) are off for every key in
        # sshd itself (`AllowTcpForwarding remote`, sshd-entrypoint.sh).
        opts = 'command="/bin/false",restrict'
        if tunnel_port:
            opts += f',port-forwarding,permitlisten="localhost:{tunnel_port}"'
        line = f'{opts} {pubkey} {MANAGED_MARKER}:device:{dev["id"]}'
        managed.append(line)

    # Write back
    all_lines = unmanaged + [POLICY_LINE] + managed
    AUTHORIZED_KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTHORIZED_KEYS_PATH.write_text("\n".join(all_lines) + "\n")
    # 0644: sshd reads authorized_keys as the target user, not root.
    # In Docker the file is on a shared volume — needs to be readable
    # by the sshd container's SSH user.
    os.chmod(str(AUTHORIZED_KEYS_PATH), 0o644)

    db.audit("system:gateway", "authorized_keys.synced", None,
             {"managed_keys": len(managed), "unmanaged_keys": len(unmanaged)})

    return {"managed": len(managed), "unmanaged": len(unmanaged)}


def get_status():
    """Return current authorized_keys state."""
    if not AUTHORIZED_KEYS_PATH.exists():
        return {"managed": 0, "unmanaged": 0, "total": 0}

    lines = [l for l in AUTHORIZED_KEYS_PATH.read_text().splitlines() if l.strip()]
    managed = sum(1 for l in lines if MANAGED_MARKER in l)
    return {
        "managed": managed,
        "unmanaged": len(lines) - managed,
        "total": len(lines),
    }
