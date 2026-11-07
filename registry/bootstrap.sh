#!/usr/bin/env bash
# samcloud bootstrap — self-extracting installer
# When piped via curl | bash, saves to temp and re-runs with real stdin
# so sudo prompts and apt-get work properly.

MARKER="__SAMCLOUD_BOOTSTRAP_MAIN__"

# If we're being piped, save to file and re-exec
if [[ ! -t 0 ]]; then
    TMPSCRIPT=$(mktemp /tmp/samcloud-bootstrap-XXXXXX)
    # Read the rest of stdin (the script from curl) into the temp file
    cat > "$TMPSCRIPT"
    chmod +x "$TMPSCRIPT"
    exec bash "$TMPSCRIPT"
    exit $?
fi

# If we get here, we have a real terminal — run the main script
# (This also handles the case where the user downloaded and ran manually)

#__SAMCLOUD_BOOTSTRAP_MAIN__

set -euo pipefail

# ── Config (injected by API) ─────────────────────────────
TOKEN="__ENROLLMENT_TOKEN__"
API="__REGISTRY_URL__"
DEVICE="__DEVICE_ID__"
TUNNEL="__TUNNEL_PORT__"
CONTEXT="__CONTEXT__"
GW_HOST="__PLANE_HOST__"
GW_PORT=__PLANE_SSH_PORT__
GW_USER="__PLANE_SSH_USER__"
ENV="__ENV__"

# ── Env-namespacing (ticket #106) ────────────────────────
# prod keeps the original BARE names so existing devices need no migration.
# Any other env (e.g. staging) gets an <env>-scoped namespace, so one
# machine/user can hold prod + staging routes at once and re-enrolling one
# env never clobbers the other. Cleanup below is scoped to this env only.
if [[ -z "$ENV" || "$ENV" == "prod" ]]; then
    SSH_HOST="samcloud-gw"
    KEY="$HOME/.ssh/samcloud_${DEVICE}"
    CFG_DIR="$HOME/.config/samcloud"
    TUN_LABEL="com.samcloud.tunnel-${DEVICE}"     # macOS LaunchAgent label
    TUN_GLOB="com.samcloud.tunnel-"               # cleanup scope (this env only)
    UNIT="samcloud-tunnel-${DEVICE}"              # linux/wsl systemd unit
    UNIT_GLOB="samcloud-tunnel-"
    CFG_MARK="# samcloud — bootstrap (${DEVICE})"
    CFG_SED="# samcloud.*bootstrap"
    LOGBASE="samcloud-tunnel-${DEVICE}"
else
    SSH_HOST="${ENV}-samcloud-gw"
    KEY="$HOME/.ssh/samcloud_${ENV}_${DEVICE}"
    CFG_DIR="$HOME/.config/samcloud/${ENV}"
    TUN_LABEL="com.samcloud.${ENV}.tunnel-${DEVICE}"
    TUN_GLOB="com.samcloud.${ENV}.tunnel-"
    UNIT="samcloud-${ENV}-tunnel-${DEVICE}"
    UNIT_GLOB="samcloud-${ENV}-tunnel-"
    CFG_MARK="# samcloud ${ENV}-route (${DEVICE})"
    CFG_SED="# samcloud ${ENV}-route"
    LOGBASE="samcloud-${ENV}-tunnel-${DEVICE}"
fi
TOKEN_FILE="${CFG_DIR}/device_token"

# ── UI ───────────────────────────────────────────────────
R='\033[0;31m' G='\033[0;32m' Y='\033[1;33m' B='\033[0;34m'
C='\033[0;36m' M='\033[0;35m' BD='\033[1m' DM='\033[2m' NC='\033[0m'
S=0
step() { S=$((S+1)); echo ""; echo -e "${C}━━━ ${S}. $1 ━━━${NC}"; }
ok()   { echo -e "  ${G}✓${NC} $*"; }
info() { echo -e "  ${DM}$*${NC}"; }
warn() { echo -e "  ${Y}!${NC} $*"; }
fail() { echo -e "  ${R}✗${NC} $*" >&2; }

# ── OS Detection ─────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Darwin)                          echo "macos" ;;
        Linux)
            grep -qi microsoft /proc/version 2>/dev/null && echo "wsl" || echo "linux" ;;
        MINGW*|MSYS*|CYGWIN*)           echo "gitbash" ;;
        *)                               echo "unknown" ;;
    esac
}

OS=$(detect_os)

# ── Header ───────────────────────────────────────────────
echo ""
echo -e "${M}        *  .  *       .        *    .${NC}"
echo -e "${M}     .    *        .        .       *${NC}"
echo -e "${M}   *   .    *    .    *  .    * ${NC}"
echo -e "${B}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${B}║${NC}  ${BD}samcloud${NC}  bootstrap wizard                  ${B}║${NC}"
echo -e "${B}╚═══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Device:     ${BD}${DEVICE}${NC}"
echo -e "  OS:         ${OS}"
[[ -n "$TUNNEL" ]] && echo -e "  Tunnel:     port ${BD}${TUNNEL}${NC}"

# Git Bash on Windows → redirect to PowerShell
if [[ "$OS" == "gitbash" ]]; then
    warn "Git Bash detected — use the PowerShell bootstrap instead:"
    echo -e "  ${BD}irm ${API}/api/v1/bootstrap/${TOKEN}/ps1 | iex${NC}"
    exit 1
fi

# ══════════════════════════════════════════════════════════
# 0. Prerequisites (before consuming the token)
# ══════════════════════════════════════════════════════════

if [[ -n "$TUNNEL" && ("$OS" == "wsl" || "$OS" == "linux") ]]; then
    step "Prerequisites"

    # ── Ticket #14: fail loud on sudo-no-tty rather than silently no-op ──
    # All subsequent steps in this block (apt-get install, ssh-keygen -A,
    # service ssh start, autossh install) need sudo. Cleanup later (step 1)
    # also calls `sudo -n systemctl disable / rm`. When this script is run
    # as `curl ... | bash`, stdin is the closed curl pipe, no TTY, and any
    # sudo prompt is invisible — the script appears to "succeed" while
    # silently skipping every sudo-gated action. Verified on a WSL device
    # 2026-04-26: unit drafted in /tmp/, never copied to /etc/systemd/system.
    #
    # Detect sudo availability up front; if unobtainable, exit BEFORE the
    # token is consumed by /devices/activate so the user can re-run cleanly.
    ensure_sudo_or_fail() {
        if sudo -n true 2>/dev/null; then
            ok "sudo cached"
            return 0
        fi
        if [[ -t 0 || -r /dev/tty ]]; then
            info "Caching sudo for systemd unit install (one-time prompt)..."
            if sudo -v 2>/dev/null; then
                ok "sudo cached"
                return 0
            fi
        fi
        echo ""
        fail "samcloud bootstrap needs sudo to install systemd unit + openssh-server."
        fail "Detected non-interactive context (curl | bash without cached sudo)."
        echo ""
        fail "Fix: re-run with sudo cached first."
        echo ""
        info "  sudo -v"
        info "  curl -s ${API}/api/v1/bootstrap/${TOKEN} | bash"
        echo ""
        info "Or save and run:"
        echo ""
        info "  curl -s ${API}/api/v1/bootstrap/${TOKEN} -o /tmp/sc-bootstrap.sh"
        info "  sudo -v && bash /tmp/sc-bootstrap.sh"
        echo ""
        fail "Your enrollment token is still valid — re-run after caching sudo."
        exit 1
    }
    ensure_sudo_or_fail

    # sshd is required for gateway to route to this device
    if ! command -v sshd >/dev/null 2>&1; then
        info "openssh-server is required for gateway access"
        info "Installing... (sudo password may be required)"
        if sudo apt-get update -qq && sudo apt-get install -y openssh-server; then
            ok "openssh-server installed"
        else
            fail "Could not install openssh-server"
            fail "Install manually and re-run: sudo apt install openssh-server"
            info "Your enrollment token is still valid — re-run this command after installing."
            exit 1
        fi
    else
        ok "openssh-server present"
    fi

    # Generate host keys if missing
    [[ -f /etc/ssh/ssh_host_ed25519_key ]] || sudo ssh-keygen -A 2>/dev/null || true

    # Start sshd if not running
    if ! pgrep -x sshd >/dev/null 2>&1; then
        sudo service ssh start && ok "sshd started" || {
            fail "Could not start sshd"
            info "Start manually: sudo service ssh start"
            info "Your enrollment token is still valid — re-run after starting sshd."
            exit 1
        }
    else
        ok "sshd running"
    fi

    # autossh (nice to have, not blocking)
    if ! command -v autossh >/dev/null 2>&1; then
        sudo apt-get install -y -qq autossh 2>/dev/null && ok "autossh installed" || info "autossh not available (will use ssh)"
    else
        ok "autossh present"
    fi
fi

# Every answer from the registry (device token, gateway host key) and the tunnel's
# key refresh are parsed with python3. On a Mac without the Command Line Tools,
# /usr/bin/python3 is a stub that only offers to install them; every parse then
# comes back empty and the strict tunnel can never verify the gateway. Checked
# before the token is consumed, so a re-run uses the same token.
if ! python3 -c 'import json' >/dev/null 2>&1; then
    echo ""
    fail "python3 does not run on this machine; the bootstrap needs it to read the registry's answers."
    if [[ "$OS" == "macos" ]]; then
        info "  xcode-select --install"
    else
        info "  sudo apt-get install -y python3"
    fi
    info "Your enrollment token is still valid — re-run this command after installing."
    exit 1
fi

# Detect local sshd port. Default 22 is wrong on some images: a WSL distro
# may run sshd on :2222 to avoid clashing with the Windows host's :22.
# Source order (first wins):
#   1. `sshd -T` output (canonical effective config incl. defaults; needs sudo)
#   2. `Port` line in /etc/ssh/sshd_config (no sudo, but ignores Include/defaults)
#   3. Whatever sshd is currently listening on (ss -tlnp)
#   4. 22 (fallback)
# The detected port is sent to /devices/activate as `ssh_port` so the registry
# stores it as outbound_ssh_port, and used directly in the systemd unit's
# `-R ${TUNNEL}:localhost:${LOCAL_SSH_PORT}` target.
LOCAL_SSH_PORT=""
if command -v sshd >/dev/null 2>&1; then
    LOCAL_SSH_PORT=$(sudo -n sshd -T 2>/dev/null | awk '$1=="port"{print $2; exit}' || true)
fi
if [[ -z "$LOCAL_SSH_PORT" && -r /etc/ssh/sshd_config ]]; then
    LOCAL_SSH_PORT=$(awk '/^[[:space:]]*Port[[:space:]]/ {print $2; exit}' /etc/ssh/sshd_config 2>/dev/null || true)
fi
if [[ -z "$LOCAL_SSH_PORT" ]] && command -v ss >/dev/null 2>&1; then
    LOCAL_SSH_PORT=$(ss -tlnp 2>/dev/null | awk '/sshd/ {split($4,a,":"); print a[length(a)]; exit}' || true)
fi
[[ -z "$LOCAL_SSH_PORT" ]] && LOCAL_SSH_PORT=22
info "Local sshd port: ${LOCAL_SSH_PORT}"

# The gateway reaches this machine at localhost:LOCAL_SSH_PORT, directly or through
# the reverse tunnel. If nothing answers there — Remote Login off on a Mac, sshd
# bound only to a LAN address — the enrollment would succeed and the device would
# never be reachable. Checked before the token is consumed.
if ! (exec 3<>"/dev/tcp/127.0.0.1/${LOCAL_SSH_PORT}") 2>/dev/null; then
    echo ""
    fail "Nothing answers on localhost:${LOCAL_SSH_PORT}; the gateway reaches this machine there."
    if [[ "$OS" == "macos" ]]; then
        info "  Turn on Remote Login: System Settings → General → Sharing → Remote Login"
        info "  (or: sudo systemsetup -setremotelogin on)"
    else
        info "  Start sshd, listening on localhost: sudo service ssh start"
    fi
    info "Your enrollment token is still valid — re-run this command after starting it."
    exit 1
fi
ok "sshd answers on localhost:${LOCAL_SSH_PORT}"

# The host key this machine's sshd presents on that port, asked of the sshd itself
# rather than guessed from a file: whatever answers on LOCAL_SSH_PORT is exactly
# what the gateway reaches, directly or through the reverse tunnel. Sent to
# /devices/activate so the gateway verifies the FIRST hop instead of learning
# whatever answers then. Empty is fine — the gateway falls back to learning it.
HOST_PUBKEY=""
if command -v ssh-keyscan >/dev/null 2>&1; then
    HOST_PUBKEY=$(ssh-keyscan -t ed25519 -p "$LOCAL_SSH_PORT" -T 5 localhost 2>/dev/null \
        | awk '$2=="ssh-ed25519"{print $2, $3; exit}' || true)
fi
if [[ -n "$HOST_PUBKEY" ]]; then
    info "Host key: $(echo "$HOST_PUBKEY" | awk '{print substr($2,1,20)}')..."
else
    warn "Could not read this machine's SSH host key; the gateway will learn it on first hop"
fi

# ══════════════════════════════════════════════════════════
# 1. Clean existing installation
# ══════════════════════════════════════════════════════════

EXISTING=false
[[ -f "$KEY" ]] && EXISTING=true
grep -q "Host ${SSH_HOST}" "$HOME/.ssh/config" 2>/dev/null && EXISTING=true
[[ -f "${TOKEN_FILE}" ]] && EXISTING=true

if [[ "$EXISTING" == true ]]; then
    echo ""
    warn "Existing ${ENV:-prod} installation detected — cleaning"
    rm -f "$KEY" "${KEY}.pub" 2>/dev/null && info "Removed old key"
    rm -f "${TOKEN_FILE}" 2>/dev/null && info "Removed old token"

    if grep -q "Host ${SSH_HOST}" "$HOME/.ssh/config" 2>/dev/null; then
        if [[ "$OS" == "macos" ]]; then
            sed -i '' "/${CFG_SED}/,/StrictHostKeyChecking/d" "$HOME/.ssh/config" 2>/dev/null
        else
            sed -i "/${CFG_SED}/,/StrictHostKeyChecking/d" "$HOME/.ssh/config" 2>/dev/null
        fi
        info "Removed old SSH config"
    fi

    if [[ "$OS" == "macos" ]]; then
        for plist in "$HOME/Library/LaunchAgents/${TUN_GLOB}"*; do
            [[ -f "$plist" ]] && launchctl unload "$plist" 2>/dev/null && rm "$plist" && info "Removed old tunnel plist"
        done
    elif [[ "$OS" == "linux" || "$OS" == "wsl" ]]; then
        for unit in /etc/systemd/system/${UNIT_GLOB}*.service; do
            [[ -f "$unit" ]] || continue
            UNIT_NAME="$(basename "$unit" .service)"
            if sudo -n systemctl disable --now "$UNIT_NAME" 2>/dev/null \
                && sudo -n rm "$unit" 2>/dev/null; then
                info "Removed old tunnel unit: $UNIT_NAME"
            else
                # Ticket #14: don't pretend we cleaned up if sudo failed
                warn "Could not remove old unit $UNIT_NAME (sudo unavailable)"
                info "  sudo systemctl disable --now $UNIT_NAME"
                info "  sudo rm $unit"
            fi
        done
    fi

    ok "Clean slate"
fi

# ══════════════════════════════════════════════════════════
# 1. SSH Keypair
# ══════════════════════════════════════════════════════════

step "SSH Keypair"

mkdir -p "$(dirname "$KEY")"
ssh-keygen -t ed25519 -f "$KEY" -N "" -C "samcloud:${DEVICE}" -q
chmod 600 "$KEY"
PUBKEY=$(cat "${KEY}.pub")
ok "Generated: ${KEY}"
info "${PUBKEY:0:60}..."

# ══════════════════════════════════════════════════════════
# 2. Activate
# ══════════════════════════════════════════════════════════

step "Activate"

STARTUP="unknown"
case "$OS" in
    macos)     STARTUP="launchd" ;;
    linux|wsl) STARTUP="systemd" ;;
esac

SSH_USER=$(whoami)

RESP=$(curl -sf -X POST "${API}/api/v1/devices/activate" \
    -H "Content-Type: application/json" \
    -d "{
        \"token\": \"${TOKEN}\",
        \"ssh_pubkey\": \"${PUBKEY}\",
        \"startup_method\": \"${STARTUP}\",
        \"ssh_user\": \"${SSH_USER}\",
        \"ssh_port\": ${LOCAL_SSH_PORT},
        \"host_pubkey\": \"${HOST_PUBKEY}\"
    }" 2>&1) || {
    fail "Activation failed"
    fail "$RESP"
    warn "Token may be expired or already used. Re-generate from admin."
    exit 1
}

json_get() { echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('$1') or '')" 2>/dev/null || echo ""; }
DEV_TOKEN=$(json_get device_token)
GW_PUBKEY=$(json_get gateway_pubkey)
SSH_USER_WARNING=$(json_get ssh_user_warning)
# The gateway's HOST keys (what this device checks the gateway BY) and the alias to
# file them under. Distinct from GW_PUBKEY above, which is the gateway's CLIENT key
# (what this device puts in authorized_keys) — opposite directions.
GW_HOST_KEY_ALIAS=$(json_get gateway_host_key_alias)
[[ -z "$GW_HOST_KEY_ALIAS" ]] && GW_HOST_KEY_ALIAS="samcloud-gateway"
GW_HOST_KEYS=$(echo "$RESP" | python3 -c "
import sys,json
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
alias=d.get('gateway_host_key_alias') or 'samcloud-gateway'
for k in (d.get('gateway_host_keys') or []):
    f=k.split()
    if len(f)>=2: print(alias, f[0], f[1])
" 2>/dev/null || echo "")
KEYS_N=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('authorized_keys_synced',{}).get('managed',0))" 2>/dev/null || echo "?")

HOST_KEY_PINNED=$(echo "$RESP" | python3 -c "import sys,json; v=json.load(sys.stdin).get('host_key_pinned'); print('' if v is None else ('yes' if v else 'no'))" 2>/dev/null || echo "")

ok "Activated"
ok "authorized_keys synced (${KEYS_N} managed)"
mkdir -p "$CFG_DIR"
if [[ -z "$GW_HOST_KEYS" ]]; then
    # Not in the activation answer: ask the published endpoint once more.
    GW_HOST_KEYS=$(curl -fsS "${API}/api/v1/gateway/host-key" 2>/dev/null | python3 -c "
import sys,json
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
alias=d.get('alias') or '${GW_HOST_KEY_ALIAS}'
for k in (d.get('host_keys') or []):
    f=k.split()
    if len(f)>=2: print(alias, f[0], f[1])
" 2>/dev/null || echo "")
fi
# The tunnel and the samcloud-gw alias verify the gateway strictly against this
# file: with no key in it, every connect is refused. The tunnel runner retries the
# fetch before each attempt, so this is reported, not fatal.
if [[ -n "$GW_HOST_KEYS" ]]; then
    printf '%s\n' "$GW_HOST_KEYS" > "${CFG_DIR}/known_hosts"
    ok "Gateway host key pinned — the tunnel verifies the gateway it dials"
else
    fail "Could not get the gateway's host key; the tunnel refuses to connect until it has one"
    info "  Check: curl -s ${API}/api/v1/gateway/host-key"
fi
if [[ -z "$DEV_TOKEN" ]]; then
    fail "The activation answer carried no device token"
fi
case "$HOST_KEY_PINNED" in
    yes) ok "Host key pinned — the gateway verifies this machine from its first hop" ;;
    no)  info "A host key is already pinned for this device; this run did not change it" ;;
esac
[[ -n "$SSH_USER_WARNING" ]] && warn "$SSH_USER_WARNING"

# ══════════════════════════════════════════════════════════
# 3. Gateway key
# ══════════════════════════════════════════════════════════

step "Gateway Access"

if [[ -n "$GW_PUBKEY" ]]; then
    AUTH_KEYS="$HOME/.ssh/authorized_keys"

    if [[ "$OS" == "wsl" ]]; then
        mkdir -p "$(dirname "$AUTH_KEYS")"
        touch "$AUTH_KEYS" && chmod 600 "$AUTH_KEYS"
        if ! grep -qF "$GW_PUBKEY" "$AUTH_KEYS" 2>/dev/null; then
            echo "$GW_PUBKEY" >> "$AUTH_KEYS"
            ok "Gateway key → WSL authorized_keys"
        else
            ok "Gateway key already in WSL"
        fi

        WIN_ADMIN_KEYS="/mnt/c/ProgramData/ssh/administrators_authorized_keys"
        if [[ -d "/mnt/c/ProgramData/ssh" ]]; then
            if echo "$GW_PUBKEY" >> "$WIN_ADMIN_KEYS" 2>/dev/null; then
                ok "Gateway key → Windows admin authorized_keys"
            else
                info "Windows admin keys not writable (non-critical for WSL)"
            fi
        fi
    else
        mkdir -p "$(dirname "$AUTH_KEYS")"
        touch "$AUTH_KEYS" && chmod 600 "$AUTH_KEYS"
        if ! grep -qF "$GW_PUBKEY" "$AUTH_KEYS" 2>/dev/null; then
            echo "$GW_PUBKEY" >> "$AUTH_KEYS"
            ok "Gateway key installed"
        else
            ok "Gateway key already installed"
        fi
    fi
else
    warn "No gateway pubkey — gateway won't route to this device"
fi

# ══════════════════════════════════════════════════════════
# 4. SSH Config
# ══════════════════════════════════════════════════════════

step "SSH Config"

SSH_CFG="$HOME/.ssh/config"
mkdir -p "$(dirname "$SSH_CFG")"
touch "$SSH_CFG" && chmod 600 "$SSH_CFG"

cat >> "$SSH_CFG" <<EOF

${CFG_MARK}
Host ${SSH_HOST}
    HostName ${GW_HOST}
    Port ${GW_PORT}
    User ${GW_USER}
    IdentityFile ${KEY}
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
    StrictHostKeyChecking yes
    UserKnownHostsFile ${CFG_DIR}/known_hosts
    HostKeyAlias ${GW_HOST_KEY_ALIAS}
    HashKnownHosts no
EOF
ok "Added '${SSH_HOST}' to ${SSH_CFG}"

# ══════════════════════════════════════════════════════════
# 5. Tunnel
# ══════════════════════════════════════════════════════════

if [[ -n "$TUNNEL" ]]; then
    step "Tunnel"

    # ── The tunnel runner ────────────────────────────────────────────────────
    #
    # One shape on every platform: refresh the gateway's pinned host key, then run
    # ONE ssh, then back off and repeat. The refresh has to sit in front of every
    # connect attempt, which is why this is a loop here rather than a pre-start hook
    # in the unit: autossh restarts ssh itself and, with AUTOSSH_GATETIME=0, never
    # exits — so a unit-level hook would fire once at boot and never again, and a
    # gateway re-key would refuse forever. That is the outage in CHANGELOG.md with
    # more steps. autossh is not used here; ServerAlive* makes ssh exit when the link
    # dies and this loop is what brings it back.
    RUNNER="${CFG_DIR}/tunnel.sh"
    mkdir -p "$CFG_DIR"
    cat > "$RUNNER" <<RUNNER_EOF
#!/bin/sh
# samcloud tunnel runner for ${DEVICE} — written by bootstrap, do not edit.
# Refresh the gateway's host key, run one ssh, back off, repeat.
API="${API}"
KH="${CFG_DIR}/known_hosts"
ALIAS="${GW_HOST_KEY_ALIAS}"
KEY="${KEY}"
BACKOFF=5

while :; do
    # Refresh the pin. Written atomically, and ONLY when the fetch succeeded and
    # parsed — a network blip must never wipe a working pin. No fallback to an
    # unverified connect: if there is no pin, ssh refuses, which is the point.
    BODY=\$(curl -fsS --max-time 10 "\${API}/api/v1/gateway/host-key" 2>/dev/null || true)
    if [ -n "\$BODY" ]; then
        LINES=\$(printf '%s' "\$BODY" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(1)
alias=d.get("alias") or "samcloud-gateway"
keys=[k for k in (d.get("host_keys") or []) if isinstance(k,str) and len(k.split())>=2]
if not keys: sys.exit(1)
for k in keys:
    t,b=k.split()[:2]
    print(f"{alias} {t} {b}")
' 2>/dev/null || true)
        if [ -n "\$LINES" ]; then
            printf '%s\\n' "\$LINES" > "\${KH}.new" && mv "\${KH}.new" "\$KH"
        fi
    fi

    START=\$(date +%s)
    ssh -N \\
        -o ExitOnForwardFailure=yes \\
        -o ServerAliveInterval=30 \\
        -o ServerAliveCountMax=3 \\
        -o StrictHostKeyChecking=yes \\
        -o UserKnownHostsFile="\$KH" \\
        -o HostKeyAlias="\$ALIAS" \\
        -o HashKnownHosts=no \\
        -o IdentitiesOnly=yes \\
        -i "\$KEY" \\
        -R ${TUNNEL}:localhost:${LOCAL_SSH_PORT} \\
        -p ${GW_PORT} \\
        ${GW_USER}@${GW_HOST} || true

    # A session that lasted is a healthy one: reset the backoff. A quick failure
    # escalates, so a refused key or a down gateway is retried without hammering.
    if [ \$(( \$(date +%s) - START )) -ge 30 ]; then
        BACKOFF=5
    else
        BACKOFF=\$(( BACKOFF * 2 ))
        [ "\$BACKOFF" -gt 60 ] && BACKOFF=60
    fi
    sleep "\$BACKOFF"
done
RUNNER_EOF
    chmod 700 "$RUNNER"
    ok "Tunnel runner: ${RUNNER}"

    # sshd + autossh already handled in Prerequisites step

    case "$OS" in
        macos)
            LABEL="${TUN_LABEL}"
            PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
            mkdir -p "$HOME/Library/LaunchAgents"
            cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${RUNNER}</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/${LOGBASE}.log</string>
    <key>StandardErrorPath</key><string>/tmp/${LOGBASE}.err</string>
</dict>
</plist>
PLIST
            ok "LaunchAgent created"
            launchctl unload "$PLIST" 2>/dev/null || true
            launchctl load "$PLIST" 2>/dev/null || true
            sleep 1
            launchctl list 2>/dev/null | grep -q "$LABEL" && ok "Tunnel started" || warn "Check: launchctl list | grep samcloud"
            ;;

        linux|wsl)
            # UNIT defined in the env-namespacing block above.
            # autossh is deliberately not used: it restarts ssh internally and with
            # AUTOSSH_GATETIME=0 never exits, so nothing would re-run the host-key
            # refresh and a gateway re-key would refuse forever. The runner's loop
            # reconnects instead, refreshing the pin before each attempt.

            cat > "/tmp/${UNIT}.service" <<UNIT_EOF
[Unit]
Description=samcloud tunnel for ${DEVICE}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
ExecStart=${RUNNER}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT_EOF
            ok "Unit: /tmp/${UNIT}.service"

            sudo cp "/tmp/${UNIT}.service" /etc/systemd/system/ && \
            sudo systemctl daemon-reload && \
            sudo systemctl enable --now "$UNIT" && \
            ok "Tunnel started (systemd)" || {
                warn "Could not install systemd unit"
                info "  sudo cp /tmp/${UNIT}.service /etc/systemd/system/"
                info "  sudo systemctl enable --now ${UNIT}"
            }
            ;;

        *)
            warn "Unknown OS — configure tunnel manually:"
            info "ssh -N -R ${TUNNEL}:localhost:${LOCAL_SSH_PORT} ${SSH_HOST}"
            ;;
    esac
fi

# ══════════════════════════════════════════════════════════
# 6. Device Token
# ══════════════════════════════════════════════════════════

step "Device Token"

if [[ -n "$DEV_TOKEN" ]]; then
    mkdir -p "$CFG_DIR"
    echo "$DEV_TOKEN" > "${TOKEN_FILE}"
    chmod 600 "${TOKEN_FILE}"
    ok "Stored: ${TOKEN_FILE/#$HOME/~}"
else
    warn "No device token received"
fi

# ══════════════════════════════════════════════════════════
# 7. Verify
# ══════════════════════════════════════════════════════════

step "Verify"

CK=0; OK=0
chk() { CK=$((CK+1)); if eval "$2"; then ok "$1"; OK=$((OK+1)); else warn "$1"; fi; }

chk "SSH key"       "[[ -f '$KEY' ]]"
chk "Device token"  "[[ -f '${TOKEN_FILE}' ]]"
chk "SSH config"    "grep -q 'Host ${SSH_HOST}' '$SSH_CFG' 2>/dev/null"

if [[ -n "$TUNNEL" ]]; then
    case "$OS" in
        macos)     chk "Tunnel" "launchctl list 2>/dev/null | grep -q '${TUN_LABEL}'" ;;
        linux|wsl) chk "Tunnel" "systemctl is-active --quiet '${UNIT}' 2>/dev/null" ;;
    esac
    chk "Gateway host key" "ssh-keygen -F '${GW_HOST_KEY_ALIAS}' -f '${CFG_DIR}/known_hosts' >/dev/null 2>&1"

    # A registered job is not a connected tunnel: one refused on every attempt keeps
    # restarting and stays listed. ExitOnForwardFailure makes ssh exit unless the
    # gateway accepted the forward, so the tunnel's ssh still running across several
    # looks is the connection holding.
    tunnel_holds() {
        local seen=0 i
        for i in $(seq 1 15); do
            if pgrep -f "${TUNNEL}:localhost:${LOCAL_SSH_PORT}" >/dev/null 2>&1; then
                seen=$((seen + 1))
                [[ $seen -ge 3 ]] && return 0
            else
                seen=0
            fi
            sleep 2
        done
        return 1
    }
    info "Waiting for the tunnel to connect..."
    TUNNEL_UP=false
    tunnel_holds && TUNNEL_UP=true
    chk "Tunnel connected" "$TUNNEL_UP"
    if [[ "$TUNNEL_UP" != true ]]; then
        case "$OS" in
            macos)     info "  Why: tail -20 /tmp/${LOGBASE}.err" ;;
            linux|wsl) info "  Why: journalctl -u ${UNIT} -n 20" ;;
        esac
    fi
fi

# ══════════════════════════════════════════════════════════
# Done
# ══════════════════════════════════════════════════════════

echo ""
echo -e "${M}   *   .    *    .    *  .    * ${NC}"
echo -e "${B}╔═══════════════════════════════════════════════╗${NC}"
if [[ "$OK" -eq "$CK" ]]; then
echo -e "${B}║${NC}  ${G}${BD}✓ Bootstrap complete${NC}  (${OK}/${CK})               ${B}║${NC}"
else
echo -e "${B}║${NC}  ${Y}${BD}! Bootstrap partial${NC}   (${OK}/${CK})               ${B}║${NC}"
fi
echo -e "${B}╚═══════════════════════════════════════════════╝${NC}"
echo -e "${M}        .       *    .   *        .${NC}"
echo ""
echo -e "  ${BD}${DEVICE}${NC} enrolled in samcloud"
[[ -n "$TUNNEL" ]] && echo -e "  Tunnel: ${BD}localhost:${TUNNEL}${NC} → this machine"
echo ""

# Clean up temp file if we were re-exec'd
[[ -n "${TMPSCRIPT:-}" ]] && rm -f "$TMPSCRIPT"
