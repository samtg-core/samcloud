#!/usr/bin/env bash
# new-seat.sh — stand up a new agent seat (one identity, one macOS account, one
# gateway target) on this Mac.
#
# Until now this sequence existed only as untracked .scratch-*sudo*.sh files at
# the repo root; every seat on the plane host was born from one of them and the recipe was
# folklore. This is that recipe, committed.
#
#   sudo ./scripts/new-seat.sh <seat-name> --device-ip IP [--api URL] [--no-rail]
#
# --device-ip: the address the gateway reaches this Mac at (required).
# --api: the plane's API base, e.g. https://cloud.example.org/api/v1, for the
#        commands it prints at the end. Unset, they say <API>.
#
# --no-rail: this seat will not run a rail at all. It gets an account, gateway
# trust, a device row and an identity, and takes part by polling — registering a
# surface if it can actually hear, long-polling ?for_conversation=, and stamping
# X-Session-Id on every write (doc 265).
#
# It is NOT the flag for "not a Claude runtime". Since rail 3.12.0 the rail runs
# those too: the seat declares RAIL_RUNTIME and each delivery becomes one turn
# (`docs/Rail Clients.md`). Use --no-rail only when nothing
# on the box should launch the runtime.
#
# What this does NOT do, deliberately:
#   - activate the identity. The seat activates itself from its own account with
#     the brief_url the invite returns; that is the onboarding path, and minting
#     a token on its behalf skips the guide it is supposed to read.
#   - grant sudo. No sudoers entry is written for a seat.
set -euo pipefail

prog=${0##*/}
die() { printf '%s: %s\n' "$prog" "$1" >&2; exit "${2:-1}"; }

[ "$(id -u)" -eq 0 ] || die "run me with sudo (I create a user account)" 2

SEAT=""; DEV_IP=""; API="<API>"; WANT_RAIL=1
while [ $# -gt 0 ]; do
  case "$1" in
    --device-ip) DEV_IP="${2:-}"; shift 2 ;;
    --api)       API="${2:-}"; API="${API%/}"; shift 2 ;;
    --no-rail)   WANT_RAIL=0; shift ;;
    -*) die "unknown option '$1'" 2 ;;
    *)  SEAT="$1"; shift ;;
  esac
done
[ -n "$SEAT" ] || die "usage: sudo $prog <seat-name> --device-ip IP [--api URL] [--no-rail]" 2
[ -n "$DEV_IP" ] || die "--device-ip IP is required: the address the gateway reaches this Mac at" 2
case "$SEAT" in (*[!a-z0-9-]*) die "seat name must be lowercase letters, digits and dashes" 2 ;; esac

HOMEDIR="/Users/$SEAT"

# ── 1. the unix account ───────────────────────────────────────────────────────
if dscl . -read "/Users/$SEAT" >/dev/null 2>&1; then
  echo "[skip] unix account $SEAT already exists"
else
  PW=$(openssl rand -base64 24)
  sysadminctl -addUser "$SEAT" -fullName "$SEAT (samcloud agent)" \
              -password "$PW" -home "$HOMEDIR" -shell /bin/zsh
  unset PW                     # never written down: the seat logs in by key, not password
  echo "[ok] created unix account $SEAT"
fi
createhomedir -c -u "$SEAT" >/dev/null 2>&1 || true
dseditgroup -o edit -a "$SEAT" -t user com.apple.access_ssh
dseditgroup -o edit -a "$SEAT" -t user _developer
echo "[ok] $SEAT is in com.apple.access_ssh and _developer"

# ── 2. gateway trust ──────────────────────────────────────────────────────────
# The gateway enters a seat as its own account, with the key the RUNNING
# gateway holds. That key is the only one that matters, and it is not the file
# on this disk: /var/lib/samcloud/gateway.pub has been stale since the stack
# was containerised (April), so every seat born from this script trusted a key
# the gateway does not use. The symptom was not a denial — ssh fell through to
# keyboard-interactive and waited forever for a password nobody could type, so
# `ssh samcloud "<seat> -- cmd"` simply never returned (one local-model seat;
# the gateway now passes BatchMode for one-off commands, so a repeat of this
# mistake denies in a second instead).
gw_pub=""
if command -v docker >/dev/null 2>&1; then
  gw_pub=$(docker exec samcloud-prod-sshd-1 ssh-keygen -yf /etc/ssh/keys/gateway 2>/dev/null | head -1)
  [ -n "$gw_pub" ] && echo "[ok] read the gateway's public key from the running gateway"
fi
if [ -z "$gw_pub" ] && [ -r /var/lib/samcloud/gateway.pub ]; then
  gw_pub=$(cat /var/lib/samcloud/gateway.pub)
  echo "[warn] could not reach the running gateway — falling back to /var/lib/samcloud/gateway.pub."
  echo "       Verify before trusting it: docker exec samcloud-prod-sshd-1 ssh-keygen -yf /etc/ssh/keys/gateway"
fi
if [ -n "$gw_pub" ]; then
  install -d -m 700 -o "$SEAT" -g staff "$HOMEDIR/.ssh"
  touch "$HOMEDIR/.ssh/authorized_keys"
  if ! grep -qF "$gw_pub" "$HOMEDIR/.ssh/authorized_keys" 2>/dev/null; then
    printf '%s samcloud-gateway\n' "$gw_pub" >> "$HOMEDIR/.ssh/authorized_keys"
  fi
  chown "$SEAT":staff "$HOMEDIR/.ssh/authorized_keys"
  chmod 600 "$HOMEDIR/.ssh/authorized_keys"
  echo "[ok] gateway public key installed for $SEAT"
else
  echo "[warn] no gateway public key found — the gateway cannot enter this seat until one is added"
fi

# ── 3. what the admin must still do, on the plane ─────────────────────────────
# Printed rather than performed: these need an admin bearer, which does not
# belong in a root-run script on a shared box.
cat <<EOF

[next] Two calls, as an admin, from your own session:

  # the device row — the gateway target name IS the device id; the unix account
  # is a separate field, which is what makes one Mac many seats
  curl -s -X POST -H @"\$HOME/.samcloud/token.hdr" -H 'Content-Type: application/json' \\
    $API/devices \\
    -d '{"id":"$SEAT","name":"$SEAT","type":"device","outbound_method":"direct-ssh",
         "outbound_ip":"$DEV_IP","outbound_ssh_user":"$SEAT","outbound_ssh_port":22}'

  # the identity — then give the seat the brief_url it returns and let IT activate
  curl -s -X POST -H @"\$HOME/.samcloud/token.hdr" -H 'Content-Type: application/json' \\
    $API/users/invite \\
    -d '{"username":"$SEAT","role":"agent","scopes":["group:services"]}'

[then] as the seat itself (sudo -H -u $SEAT env HOME=$HOMEDIR ...):
  - read the brief_url, POST /users/activate with a password it chooses
  - write ~/.samcloud/token 0600 and token.hdr beside it
EOF

if [ "$WANT_RAIL" -eq 1 ]; then
  cat <<EOF
  - curl -s -H @"\$HOME/.samcloud/token.hdr" $API/helpers/wake-install | RAIL_DRY=1 bash
  - then the one privileged step it prints (the LaunchDaemon), and remove RAIL_DRY when ready
EOF
else
  cat <<EOF
  - NO wake-install: nothing here will launch this seat. It takes part by polling
    (doc 265): declare a receive profile honestly, register a surface only if it
    can actually hear, long-poll ?for_conversation=<id>&undelivered=true, receipt
    what it actually delivered, and stamp X-Session-Id on every write (sc-say /
    sc-new do this for you).
    If you DO want the rail to run it, drop --no-rail and install with its runtime:
      RAIL_RUNTIME=<name> RAIL_RUNTIME_CMD="..." curl -s -H @"\$HOME/.samcloud/token.hdr" \\
        $API/helpers/wake-install | RAIL_DRY=1 bash
EOF
fi
echo
echo "[verify] ssh samcloud \"$SEAT -- whoami\"   should print $SEAT"
