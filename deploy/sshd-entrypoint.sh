#!/bin/bash
set -e

KEYS_DIR="/etc/ssh/keys"
# Public halves of the host keys only, for readers that must verify this container
# without being able to impersonate it. KEYS_DIR holds the PRIVATE host keys, so it
# is never shared: anything holding those can present itself as the gateway to every
# device that dials in.
HOSTPUB_DIR="/etc/ssh/hostpub"
REGISTRY_DATA="/registry-data"

# Generate host keys if missing
if [ ! -f "$KEYS_DIR/ssh_host_ed25519_key" ]; then
    ssh-keygen -t ed25519 -f "$KEYS_DIR/ssh_host_ed25519_key" -N "" -q
    echo "[sshd] Generated ed25519 host key"
fi
if [ ! -f "$KEYS_DIR/ssh_host_rsa_key" ]; then
    ssh-keygen -t rsa -b 4096 -f "$KEYS_DIR/ssh_host_rsa_key" -N "" -q
    echo "[sshd] Generated RSA host key"
fi

# Publish the public halves, by name rather than by glob, so nothing else in
# KEYS_DIR can be published by accident. Re-copied on every boot, not only on
# generation, so a regenerated key is followed and a reader never verifies against
# a key this container no longer presents.
mkdir -p "$HOSTPUB_DIR"
for k in ssh_host_ed25519_key ssh_host_rsa_key; do
    if [ -f "$KEYS_DIR/$k.pub" ]; then
        cp "$KEYS_DIR/$k.pub" "$HOSTPUB_DIR/$k.pub"
        chmod 644 "$HOSTPUB_DIR/$k.pub"
    fi
done
echo "[sshd] Host public keys published to $HOSTPUB_DIR"

# Create samcloud user for SSH connections
if ! id -u samcloud &>/dev/null; then
    useradd -m -s /bin/bash samcloud
    # Must set a password to unlock the account — sshd rejects locked accounts
    # even with valid pubkey. Password login is disabled in sshd_config.
    echo "samcloud:$(head -c 32 /dev/urandom | base64)" | chpasswd
    echo "[sshd] Created samcloud user"
fi

# Copy authorized_keys from registry data volume (not symlink — StrictModes
# requires the file to be owned by the target user, and the volume is root-owned).
# A background loop keeps it in sync.
mkdir -p /home/samcloud/.ssh
chown -R samcloud:samcloud /home/samcloud/.ssh
chmod 700 /home/samcloud/.ssh
# Start only on a file the registry wrote under this release's key policy
# (KEYS_POLICY in registry/authorized_keys.py; a test holds them equal). On an
# older file, sessions would authenticate with options this release removed,
# and keep them for as long as they stay connected. The registry writes the
# file at every start.
KEYS_POLICY=2
echo "[sshd] Waiting for authorized_keys at keys-policy=$KEYS_POLICY..."
until grep -q "^# samcloud-managed keys-policy=$KEYS_POLICY\$" "$REGISTRY_DATA/authorized_keys" 2>/dev/null; do
    sleep 1
done
echo "[sshd] authorized_keys at keys-policy=$KEYS_POLICY"
sync_keys() {
    while true; do
        if [ -f "$REGISTRY_DATA/authorized_keys" ]; then
            cp "$REGISTRY_DATA/authorized_keys" /home/samcloud/.ssh/authorized_keys
            chown samcloud:samcloud /home/samcloud/.ssh/authorized_keys
            chmod 600 /home/samcloud/.ssh/authorized_keys
        fi
        sleep 5
    done
}
sync_keys &

# Wait for gateway key (registry auto-generates on first boot)
echo "[sshd] Waiting for gateway key..."
for i in $(seq 1 30); do
    if [ -f "$REGISTRY_DATA/keys/gateway" ]; then
        cp "$REGISTRY_DATA/keys/gateway" /etc/ssh/keys/gateway
        chmod 600 /etc/ssh/keys/gateway
        chown samcloud:samcloud /etc/ssh/keys/gateway
        echo "[sshd] Gateway key installed"
        break
    fi
    sleep 1
done
if [ ! -f /etc/ssh/keys/gateway ]; then
    echo "[sshd] WARNING: Gateway key not found after 30s — gateway routing will fail"
fi

# Authorize the gateway pubkey for unrestricted samcloud login from the registry
# container. The same key is in /registry-data/authorized_keys with ForceCommand
# wrappers for device sessions; here we add a second file with NO restriction so
# the registry can ssh in and spawn nested port-forwards inside sshd's network
# namespace (forwards must live here because tunnel ports bind 127.0.0.1 only —
# unreachable from other containers).
if [ -f /etc/ssh/keys/gateway ]; then
    ssh-keygen -y -f /etc/ssh/keys/gateway > /etc/ssh/keys/control_authorized_keys
    # Owner must be the user (samcloud) for StrictModes to accept this file.
    chown samcloud:samcloud /etc/ssh/keys/control_authorized_keys
    chmod 600 /etc/ssh/keys/control_authorized_keys
    echo "[sshd] Control authorized_keys written (gateway pub, unrestricted samcloud)"
fi

# Write sshd_config
cat > /etc/ssh/sshd_config << 'EOF'
Port 22
ListenAddress 0.0.0.0
HostKey /etc/ssh/keys/ssh_host_ed25519_key
HostKey /etc/ssh/keys/ssh_host_rsa_key

PubkeyAuthentication yes
# Two key sources: device sessions go through the registry-managed file (with
# ForceCommand routing); the control-plane file lets the registry's gateway key
# log in unrestricted to manage port forwards from inside this container.
AuthorizedKeysFile /home/samcloud/.ssh/authorized_keys /etc/ssh/keys/control_authorized_keys
PasswordAuthentication no
PermitRootLogin no

AllowUsers samcloud
# remote: devices bind their reverse tunnels (-R). No key opens local forwards
# (-L, -W) through this sshd: they would reach every service forward and tunnel
# port on its localhost and the compose network, past groups and route auth.
# The registry's forwards run ssh against the device's sshd, not this one.
AllowTcpForwarding remote
GatewayPorts no
PermitTTY yes
X11Forwarding no
PrintMotd no

# Reap dead client sessions ~90s after they go silent. Without this, an
# ungraceful disconnect (device reboot, network drop) leaves the TCP
# session in ESTABLISHED forever, holding any reverse-tunnel ports it
# bound. New autossh from the same device fails with "remote port
# forwarding failed for listen port <N>" until manually reaped — see
# prod ticket #32. Pairs with autossh's own ServerAliveInterval=30.
ClientAliveInterval 30
ClientAliveCountMax 3

LogLevel INFO
EOF

echo "[sshd] Starting on :22"
exec /usr/sbin/sshd -D -e
