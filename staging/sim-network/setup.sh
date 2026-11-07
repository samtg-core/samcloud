#!/bin/bash
## setup.sh — Seed the staging environment through the enrollment API
## Every action goes through the same flow a real admin/device would use.
##
## Prerequisites:
##   - Staging registry running on :7778 (staging-deploy.sh handles this)
##   - Sim network containers running (docker compose up -d)
##
## Usage: ./staging/sim-network/setup.sh

set -euo pipefail

REGISTRY="http://localhost:7778"
ADMIN_USER="admin"
ADMIN_PASS="admin123"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

# Verify registry is running
if ! curl -sf "$REGISTRY/health" > /dev/null 2>&1; then
    echo -e "${RED}Registry not running at $REGISTRY${NC}"
    echo "Run ./scripts/staging-deploy.sh first"
    exit 1
fi

ADMIN_TOKEN=""

api() {
    local method="$1" path="$2" body="${3:-}"
    local auth_header=""
    if [ -n "$ADMIN_TOKEN" ]; then
        auth_header="-H"
        auth_arg="Authorization: Bearer $ADMIN_TOKEN"
    fi
    if [ -n "$body" ]; then
        if [ -n "$ADMIN_TOKEN" ]; then
            curl -s -X "$method" "$REGISTRY$path" -H 'Content-Type: application/json' -H "Authorization: Bearer $ADMIN_TOKEN" -d "$body"
        else
            curl -s -X "$method" "$REGISTRY$path" -H 'Content-Type: application/json' -d "$body"
        fi
    else
        if [ -n "$ADMIN_TOKEN" ]; then
            curl -s -X "$method" "$REGISTRY$path" -H "Authorization: Bearer $ADMIN_TOKEN"
        else
            curl -s -X "$method" "$REGISTRY$path"
        fi
    fi
}

check() {
    local desc="$1" result="$2"
    if echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('status') not in ['error'] and 'detail' not in d else 1)" 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} $desc"
    else
        echo -e "  ${RED}✗${NC} $desc"
        echo "    $result"
    fi
}

echo ""
echo "========================================="
echo "  Staging Environment Setup"
echo "  Registry: $REGISTRY"
echo "========================================="
echo ""

# ── Step 1: Admin + users ────────────────────────────────
echo -e "${YELLOW}Step 1: Users${NC}"
result=$(api POST /api/v1/init "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}")
check "Admin user" "$result"

# Extract admin token — all subsequent API calls use this
ADMIN_TOKEN=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
if [ -z "$ADMIN_TOKEN" ]; then
    # Already initialized — login instead
    result=$(api POST /api/v1/auth/login "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}")
    ADMIN_TOKEN=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
fi
if [ -z "$ADMIN_TOKEN" ]; then
    echo -e "  ${RED}✗ Could not get admin token${NC}"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} Admin token acquired"

result=$(api POST /api/v1/users '{"username":"claude","password":"changeme","role":"admin"}')
check "User: claude (admin)" "$result"
result=$(api POST /api/v1/users '{"username":"robin","password":"hello","role":"user"}')
check "User: robin (user)" "$result"

# ── Step 2: Generate gateway keypair ─────────────────────
echo -e "${YELLOW}Step 2: Gateway keypair${NC}"
result=$(api POST /api/v1/gateway/keygen)
GATEWAY_PUBKEY=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('public_key',''))" 2>/dev/null)
check "Gateway keypair ($(echo "$GATEWAY_PUBKEY" | cut -c1-30)...)" "$result"

# ── Step 3: Enroll sim devices (access targets) ─────────
echo -e "${YELLOW}Step 3: Enroll sim devices${NC}"

# Each sim device goes through: enroll → activate → install gateway key
SIM_DEVICES="sim-media:media-server:2201 sim-homehub:compute:2202 sim-gpu-1:gpu-workstation:2203 sim-gpu-2:gpu-workstation:2204"

for entry in $SIM_DEVICES; do
    IFS=: read -r DEV_ID DEV_TYPE SSH_PORT <<< "$entry"

    # Enroll as access target (direct-ssh, localhost)
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

    # Activate (simulating bootstrap — generates device identity)
    result=$(api POST /api/v1/devices/activate "{
        \"token\":\"$TOKEN\",
        \"ssh_pubkey\":\"ssh-ed25519 AAAA sim-key-$DEV_ID\",
        \"startup_method\":\"docker\"
    }")
    check "$DEV_ID activated" "$result"

    # Install gateway pubkey on the sim container (so gateway can SSH in)
    if [ -n "$GATEWAY_PUBKEY" ]; then
        # SSH into container with password auth and install the key
        sshpass -p samcloud ssh -o StrictHostKeyChecking=no -o ConnectTimeout=3 \
            -p "$SSH_PORT" samcloud@localhost \
            "mkdir -p ~/.ssh && echo '$GATEWAY_PUBKEY' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys" \
            2>/dev/null && check "$DEV_ID gateway key installed" '{"status":"ok"}' \
            || check "$DEV_ID gateway key installed (sshpass may be needed)" '{"detail":"install sshpass: brew install hudochenkov/sshpass/sshpass"}'
    fi
done

# ── Step 4: Test enrollment flows ────────────────────────
echo -e "${YELLOW}Step 4: Test enrollment flows${NC}"

# Access route device (phone) — immediate activation
result=$(api POST /api/v1/enroll '{"device_id":"test-phone","name":"Test Phone","type":"phone","access_route":"ssh-tunnel"}')
check "test-phone (route, auto-active)" "$result"

# Access target device — pending until bootstrap
result=$(api POST /api/v1/enroll '{"device_id":"test-server","name":"Test Server","type":"server","access_target":"reverse-tunnel","target_ssh_user":"test"}')
check "test-server (target, pending)" "$result"
TOKEN=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
if [ -n "$TOKEN" ]; then
    result=$(api POST /api/v1/devices/activate "{\"token\":\"$TOKEN\",\"ssh_pubkey\":\"ssh-ed25519 AAAA test-server-key\",\"startup_method\":\"systemd\"}")
    check "test-server activated" "$result"
fi

# ── Step 5: Register services ────────────────────────────
echo -e "${YELLOW}Step 5: Services${NC}"
result=$(api POST /api/v1/services '{"name":"mediaserver","device_id":"sim-media","port":8081,"health_endpoint":"/health","subdomain":"media-stg"}')
check "mediaserver → sim-media" "$result"
result=$(api POST /api/v1/services '{"name":"homeassistant","device_id":"sim-homehub","port":8123,"health_endpoint":"/health"}')
check "homeassistant → sim-homehub" "$result"
result=$(api POST /api/v1/services '{"name":"nerfstudio","device_id":"sim-gpu-1","port":8420,"health_endpoint":"/health","subdomain":"spl-stg"}')
check "nerfstudio → sim-gpu-1" "$result"
result=$(api POST /api/v1/services '{"name":"model-gateway","device_id":"sim-gpu-2","port":8450,"health_endpoint":"/health","subdomain":"mdl-stg"}')
check "model-gateway → sim-gpu-2" "$result"

# ── Step 6: Groups ───────────────────────────────────────
echo -e "${YELLOW}Step 6: Groups${NC}"
result=$(api POST /api/v1/groups '{"id":"home","name":"Home","description":"Home services","route_path":"/home"}')
check "Group: home" "$result"
result=$(api POST /api/v1/groups '{"id":"work","name":"Work","description":"Work GPU services","route_path":"/work"}')
check "Group: work" "$result"

# Services → groups
result=$(api POST /api/v1/groups/home/services '{"service_id":"sim-media/mediaserver"}')
check "mediaserver → home" "$result"
result=$(api POST /api/v1/groups/home/services '{"service_id":"sim-homehub/homeassistant"}')
check "homeassistant → home" "$result"
result=$(api POST /api/v1/groups/work/services '{"service_id":"sim-gpu-1/nerfstudio"}')
check "nerfstudio → work" "$result"
result=$(api POST /api/v1/groups/work/services '{"service_id":"sim-gpu-2/model-gateway"}')
check "model-gateway → work" "$result"

# Users → groups
result=$(api POST /api/v1/groups/home/members '{"user_id":1,"role":"admin"}')
check "admin → home" "$result"
result=$(api POST /api/v1/groups/home/members '{"user_id":3,"role":"viewer"}')
check "robin → home" "$result"
result=$(api POST /api/v1/groups/work/members '{"user_id":1,"role":"admin"}')
check "admin → work" "$result"
result=$(api POST /api/v1/groups/work/members '{"user_id":2,"role":"admin"}')
check "claude → work" "$result"

# ── Step 7: Access key + sync ────────────────────────────
echo -e "${YELLOW}Step 7: Access key + authorized_keys sync${NC}"
result=$(api POST /api/v1/access-keys '{"username":"admin","device_name":"setup-test"}')
check "Access key: admin@setup-test" "$result"
result=$(api POST /api/v1/gateway/sync)
check "authorized_keys synced" "$result"

# ── Verify ───────────────────────────────────────────────
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
echo "========================================="
echo -e "  ${GREEN}Staging ready${NC}"
echo "  Console:  https://stg.example.org"
echo "  Registry: http://localhost:7778"
echo "  sshd:     localhost:2200"
echo "  Admin:    $ADMIN_USER / $ADMIN_PASS"
echo "========================================="
