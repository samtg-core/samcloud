#!/bin/bash
## seed.sh — Seed a samcloud environment through the enrollment API
##
## Usage:
##   ./deploy/seed.sh                          # seed against default registry (:7778)
##   ./deploy/seed.sh --registry http://localhost:7776  # custom registry
##   ./deploy/seed.sh --fresh                   # wipe DB first (requires admin token)
##   ./deploy/seed.sh --with-sims              # start sim-network and enroll sim devices
##
## Idempotent: safe to run multiple times (uses INSERT OR IGNORE / checks existing).

set -euo pipefail

SAMCLOUD="$(cd "$(dirname "$0")/.." && pwd)"
REGISTRY="${REGISTRY:-http://localhost:7778}"
ADMIN_USER="${SEED_ADMIN_USER:-admin}"
ADMIN_PASS="${SEED_ADMIN_PASS:-admin123}"
FRESH=false
WITH_SIMS=false

for arg in "$@"; do
    case "$arg" in
        --fresh) FRESH=true ;;
        --with-sims) WITH_SIMS=true ;;
        --registry) shift; REGISTRY="$2" ;;
        --registry=*) REGISTRY="${arg#*=}" ;;
    esac
done

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# --- Helpers ---
ADMIN_TOKEN=""

api() {
    local method="$1" path="$2" body="${3:-}"
    local headers=(-H 'Content-Type: application/json')
    [ -n "$ADMIN_TOKEN" ] && headers+=(-H "Authorization: Bearer $ADMIN_TOKEN")
    if [ -n "$body" ]; then
        curl -s -X "$method" "$REGISTRY$path" "${headers[@]}" -d "$body"
    else
        curl -s -X "$method" "$REGISTRY$path" "${headers[@]}"
    fi
}

check() {
    local desc="$1" result="$2"
    if echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if 'detail' not in d else 1)" 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} $desc"
    else
        local detail=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('detail',''))" 2>/dev/null)
        if echo "$detail" | grep -qi "already\|exists\|duplicate"; then
            echo -e "  ${GREEN}✓${NC} $desc (already exists)"
        else
            echo -e "  ${RED}✗${NC} $desc"
            echo "    $detail"
        fi
    fi
}

# --- Verify registry ---
if ! curl -sf "$REGISTRY/health" > /dev/null 2>&1; then
    echo -e "${RED}Registry not running at $REGISTRY${NC}"
    exit 1
fi

echo ""
echo -e "${CYAN}=========================================${NC}"
echo -e "  ${CYAN}samcloud seed${NC}"
echo -e "  Registry: $REGISTRY"
[ "$FRESH" = true ] && echo -e "  Mode: ${YELLOW}FRESH (wipe + reseed)${NC}"
[ "$WITH_SIMS" = true ] && echo -e "  Sims: ${YELLOW}enabled${NC}"
echo -e "${CYAN}=========================================${NC}"
echo ""

# ── Step 1: Admin user ──────────────────────────────────
echo -e "${YELLOW}Step 1: Admin${NC}"

result=$(api POST /api/v1/init "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}")
ADMIN_TOKEN=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)

if [ -z "$ADMIN_TOKEN" ]; then
    # Already initialized — try login
    result=$(api POST /api/v1/auth/login "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}")
    ADMIN_TOKEN=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
fi

if [ -z "$ADMIN_TOKEN" ]; then
    echo -e "  ${RED}✗ Could not get admin token${NC}"
    echo "  Set SEED_ADMIN_USER and SEED_ADMIN_PASS env vars to match your admin credentials"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} Admin token acquired"

# ── Step 2: Additional users ────────────────────────────
echo -e "${YELLOW}Step 2: Users${NC}"
result=$(api POST /api/v1/users '{"username":"agent","password":"changeme","role":"admin"}')
check "User: agent (admin)" "$result"
result=$(api POST /api/v1/users '{"username":"viewer","password":"hello","role":"user"}')
check "User: viewer (user)" "$result"

# ── Step 3: Groups ──────────────────────────────────────
echo -e "${YELLOW}Step 3: Groups${NC}"
result=$(api POST /api/v1/groups '{"id":"home","name":"Home","description":"Home network services","route_path":"/home"}')
check "Group: home" "$result"
result=$(api POST /api/v1/groups '{"id":"work","name":"Work","description":"GPU compute services","route_path":"/work"}')
check "Group: work" "$result"

# ── Step 4: Sim devices (optional) ──────────────────────
if [ "$WITH_SIMS" = true ]; then
    echo -e "${YELLOW}Step 4: Sim network${NC}"

    # Start sim containers
    echo -n "  sim containers... "
    SIM_DIR="$SAMCLOUD/staging/sim-network"
    if [ -f "$SIM_DIR/docker-compose.yml" ]; then
        (cd "$SIM_DIR" && docker compose up -d --build 2>/dev/null)
        echo -e "${GREEN}✓${NC}"
    else
        echo -e "${RED}✗ sim-network not found${NC}"
    fi

    # Get gateway pubkey for installation
    GATEWAY_PUBKEY=$(api GET /api/v1/gateway/pubkey | python3 -c "import sys,json; print(json.load(sys.stdin).get('public_key',''))" 2>/dev/null)

    # Enroll sim devices
    SIM_DEVICES="dev-alpha:server:2201 dev-bravo:server:2202 dev-charlie:gpu:2203 dev-delta:gpu:2204"
    for entry in $SIM_DEVICES; do
        IFS=: read -r DEV_ID DEV_TYPE SSH_PORT <<< "$entry"

        result=$(api POST /api/v1/enroll "{
            \"device_id\":\"$DEV_ID\",
            \"name\":\"$DEV_ID\",
            \"type\":\"$DEV_TYPE\",
            \"access_target\":\"direct-ssh\",
            \"target_ip\":\"localhost\",
            \"target_ssh_user\":\"samcloud\",
            \"target_ssh_port\":$SSH_PORT
        }")
        TOKEN=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)

        if [ -z "$TOKEN" ]; then
            check "$DEV_ID enrolled" "$result"
            continue
        fi

        result=$(api POST /api/v1/devices/activate "{
            \"token\":\"$TOKEN\",
            \"ssh_pubkey\":\"ssh-ed25519 AAAA sim-key-$DEV_ID\",
            \"startup_method\":\"docker\"
        }")
        check "$DEV_ID activated" "$result"

        # Install gateway key on sim container
        if [ -n "$GATEWAY_PUBKEY" ] && command -v sshpass &>/dev/null; then
            sshpass -p samcloud ssh -o StrictHostKeyChecking=no -o ConnectTimeout=3 \
                -p "$SSH_PORT" samcloud@localhost \
                "mkdir -p ~/.ssh && echo '$GATEWAY_PUBKEY' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys" \
                2>/dev/null && check "$DEV_ID gateway key" '{"status":"ok"}' \
                || check "$DEV_ID gateway key" '{"detail":"sshpass needed"}'
        fi
    done

    # Register services on sim devices
    echo -e "${YELLOW}Step 5: Sim services${NC}"
    result=$(api POST /api/v1/services '{"name":"web-app","device_id":"dev-alpha","port":8080,"description":"Web application","health_endpoint":"/health","subdomain":"app-stg"}')
    check "web-app → dev-alpha" "$result"
    result=$(api POST /api/v1/services '{"name":"api-server","device_id":"dev-bravo","port":8000,"description":"API server","health_endpoint":"/health","subdomain":"api-stg"}')
    check "api-server → dev-bravo" "$result"
    result=$(api POST /api/v1/services '{"name":"inference","device_id":"dev-charlie","port":8420,"description":"ML inference","health_endpoint":"/health","subdomain":"inf-stg"}')
    check "inference → dev-charlie" "$result"
    result=$(api POST /api/v1/services '{"name":"model-api","device_id":"dev-delta","port":8450,"description":"Model serving","health_endpoint":"/health","subdomain":"mdl-stg"}')
    check "model-api → dev-delta" "$result"

    # Services → groups
    result=$(api POST /api/v1/groups/home/services '{"service_id":"dev-alpha/web-app"}')
    check "web-app → home" "$result"
    result=$(api POST /api/v1/groups/home/services '{"service_id":"dev-bravo/api-server"}')
    check "api-server → home" "$result"
    result=$(api POST /api/v1/groups/work/services '{"service_id":"dev-charlie/inference"}')
    check "inference → work" "$result"
    result=$(api POST /api/v1/groups/work/services '{"service_id":"dev-delta/model-api"}')
    check "model-api → work" "$result"
else
    echo -e "${YELLOW}Step 4: Sim devices (skipped — use --with-sims)${NC}"
fi

# ── Step 5/6: Test enrollment flows ─────────────────────
echo -e "${YELLOW}Step $( [ "$WITH_SIMS" = true ] && echo 6 || echo 5): Test devices${NC}"
result=$(api POST /api/v1/enroll '{"device_id":"test-route","name":"Test Route Device","type":"phone","access_route":"ssh-tunnel"}')
check "test-route (access route, auto-active)" "$result"
result=$(api POST /api/v1/enroll '{"device_id":"test-target","name":"Test Target Device","type":"server","access_target":"reverse-tunnel","target_ssh_user":"test"}')
check "test-target (access target, pending)" "$result"
TOKEN=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
if [ -n "$TOKEN" ]; then
    result=$(api POST /api/v1/devices/activate "{\"token\":\"$TOKEN\",\"ssh_pubkey\":\"ssh-ed25519 AAAA test-target-key\",\"startup_method\":\"systemd\"}")
    check "test-target activated" "$result"
fi

# ── Access key + sync ───────────────────────────────────
echo -e "${YELLOW}Step $( [ "$WITH_SIMS" = true ] && echo 7 || echo 6): Access key + sync${NC}"
result=$(api POST /api/v1/access-keys "{\"username\":\"$ADMIN_USER\",\"device_name\":\"seed-test\"}")
check "Access key: $ADMIN_USER@seed-test" "$result"
result=$(api POST /api/v1/gateway/sync)
check "authorized_keys synced" "$result"

# ── Verify ──────────────────────────────────────────────
echo ""
echo -e "${YELLOW}Verify${NC}"
result=$(api GET /api/v1/status)
echo "$result" | python3 -c "
import sys,json
d = json.load(sys.stdin)
c = d['counts']
print(f'  Devices:  {c[\"devices\"]}')
print(f'  Services: {c[\"services\"]}')
print(f'  Routes:   {c[\"routes\"]}')
print(f'  Tickets:  {c[\"open_tickets\"]}')
"

echo ""
echo -e "${CYAN}=========================================${NC}"
echo -e "  ${GREEN}Seed complete${NC}"
echo -e "  Registry: $REGISTRY"
echo -e "  Admin:    $ADMIN_USER / $ADMIN_PASS"
echo -e "${CYAN}=========================================${NC}"
