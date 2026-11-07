#!/bin/sh
## Seed entrypoint — sets up a complete staging environment:
##   Admin + scoped users, groups, devices, services, ownership.
##   Validates visibility per scope at the end.
set -e

REGISTRY="http://registry:7777"
BOOTSTRAP_DIR="/bootstrap"

echo "[seed] Waiting for registry..."
until curl -sf "$REGISTRY/health" > /dev/null 2>&1; do sleep 1; done
echo "[seed] Registry healthy"

ok() { echo "[seed]   ✓ $1"; }
fail() { echo "[seed]   ✗ $1: $2"; }

# ── Init admin ──────────────────────────────────────────
echo "[seed] Initializing..."
INIT_RESP=$(curl -s -X POST "$REGISTRY/api/v1/init" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}')
TOKEN=$(echo "$INIT_RESP" | grep -o '"token":"[^"]*"' | cut -d'"' -f4)
if [ -z "$TOKEN" ]; then
  TOKEN=$(curl -s -X POST "$REGISTRY/api/v1/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"admin123"}' | \
    grep -o '"token":"[^"]*"' | cut -d'"' -f4)
fi
[ -z "$TOKEN" ] && echo "[seed] FAIL: no admin token" && exit 1
ok "Admin (admin/admin123)"
AUTH="Authorization: Bearer $TOKEN"

# ── Groups ──────────────────────────────────────────────
echo "[seed] Groups..."
curl -s -X POST "$REGISTRY/api/v1/groups" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"id":"home","name":"Home","description":"Home network — media, storage, automation"}' > /dev/null
curl -s -X POST "$REGISTRY/api/v1/groups" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"id":"work","name":"Work","description":"GPU compute — inference, rendering, training"}' > /dev/null
ok "home, work"

# ── Scoped users ────────────────────────────────────────
echo "[seed] Users..."

# Home operator — manages home devices
curl -s -X POST "$REGISTRY/api/v1/users" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"username":"home-ops","password":"home123","role":"user","scopes":["group:home"]}' > /dev/null
ok "home-ops (user, group:home)"

# Work operator — manages work/GPU devices
curl -s -X POST "$REGISTRY/api/v1/users" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"username":"work-ops","password":"work123","role":"agent","scopes":["group:work"]}' > /dev/null
ok "work-ops (agent, group:work)"

# Read-only viewer — can browse but not modify
curl -s -X POST "$REGISTRY/api/v1/users" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"username":"viewer","password":"view123","role":"user","scopes":["group:home"]}' > /dev/null
ok "viewer (user, group:home)"

# ── Gateway check ───────────────────────────────────────
echo "[seed] Gateway..."
GW_EXISTS=$(curl -s "$REGISTRY/api/v1/gateway/status" -H "$AUTH" | \
  grep -o '"gateway_key_exists":[a-z]*' | cut -d: -f2)
[ "$GW_EXISTS" = "true" ] && ok "Gateway key exists" || fail "Gateway key" "not found"

# ── Enroll sim devices ──────────────────────────────────
echo "[seed] Enrolling sim devices..."
mkdir -p "$BOOTSTRAP_DIR"

for DEV_SPEC in "dev-alpha:server:home" "dev-bravo:server:home" "dev-charlie:gpu:work" "dev-delta:gpu:work"; do
  DEV_ID=$(echo "$DEV_SPEC" | cut -d: -f1)
  DEV_TYPE=$(echo "$DEV_SPEC" | cut -d: -f2)
  DEV_GROUP=$(echo "$DEV_SPEC" | cut -d: -f3)

  ENROLL_RESP=$(curl -s -X POST "$REGISTRY/api/v1/enroll" -H "$AUTH" \
    -H "Content-Type: application/json" \
    -d "{\"device_id\":\"$DEV_ID\",\"name\":\"$DEV_ID\",\"type\":\"$DEV_TYPE\",\"access_target\":\"direct-ssh\",\"target_ip\":\"$DEV_ID\",\"target_ssh_user\":\"samcloud\",\"target_ssh_port\":22,\"group_ids\":[\"$DEV_GROUP\"]}")

  ENROLL_TOKEN=$(echo "$ENROLL_RESP" | grep -o '"token":"[^"]*"' | cut -d'"' -f4)
  BOOTSTRAP_URL=$(echo "$ENROLL_RESP" | grep -o '"bootstrap_url":"[^"]*"' | cut -d'"' -f4)

  if [ -z "$ENROLL_TOKEN" ]; then
    ok "$DEV_ID (exists)"
    continue
  fi

  INTERNAL_URL=$(echo "$BOOTSTRAP_URL" | sed "s|https://[^/]*/|$REGISTRY/|")
  BS_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$INTERNAL_URL")
  echo "$INTERNAL_URL" > "$BOOTSTRAP_DIR/$DEV_ID.url"
  [ "$BS_STATUS" = "200" ] && ok "$DEV_ID → $DEV_GROUP (bootstrap valid)" || fail "$DEV_ID" "HTTP $BS_STATUS"
done

# ── Wait for devices to bootstrap ───────────────────────
echo "[seed] Waiting for devices to bootstrap..."
for i in $(seq 1 30); do
  ONLINE=$(curl -s "$REGISTRY/api/v1/devices" -H "$AUTH" | \
    grep -o '"status":"online"' | wc -l | tr -d ' ')
  [ "$ONLINE" -ge 4 ] 2>/dev/null && ok "$ONLINE devices online" && break
  [ "$i" = "30" ] && fail "Timeout" "$ONLINE/4 online"
  sleep 3
done

# ── Register services (owned by scoped users) ───────────
echo "[seed] Registering services..."

# Get scoped user tokens
HOME_TOKEN=$(curl -s -X POST "$REGISTRY/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"home-ops","password":"home123"}' | \
  grep -o '"token":"[^"]*"' | cut -d'"' -f4)

WORK_TOKEN=$(curl -s -X POST "$REGISTRY/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"work-ops","password":"work123"}' | \
  grep -o '"token":"[^"]*"' | cut -d'"' -f4)

# Home services (owned by home-ops)
HOME_AUTH="Authorization: Bearer $HOME_TOKEN"

curl -s -X POST "$REGISTRY/api/v1/services" -H "$HOME_AUTH" -H "Content-Type: application/json" \
  -d '{"name":"media-server","device_id":"dev-alpha","port":8081,"description":"Streaming media library — movies, TV, music","health_endpoint":"/health","subdomain":"media"}' > /dev/null
ok "dev-alpha/media-server (home-ops)"

curl -s -X POST "$REGISTRY/api/v1/services" -H "$HOME_AUTH" -H "Content-Type: application/json" \
  -d '{"name":"photo-library","device_id":"dev-alpha","port":2283,"description":"Photo and video management with ML-powered search","health_endpoint":"/health","subdomain":"photos"}' > /dev/null
ok "dev-alpha/photo-library (home-ops)"

curl -s -X POST "$REGISTRY/api/v1/services" -H "$HOME_AUTH" -H "Content-Type: application/json" \
  -d '{"name":"home-api","device_id":"dev-bravo","port":8123,"description":"Home automation API — lights, sensors, routines","health_endpoint":"/api/","subdomain":"hass"}' > /dev/null
ok "dev-bravo/home-api (home-ops)"

curl -s -X POST "$REGISTRY/api/v1/services" -H "$HOME_AUTH" -H "Content-Type: application/json" \
  -d '{"name":"file-sync","device_id":"dev-bravo","port":8384,"description":"Encrypted file synchronisation across devices","health_endpoint":"/rest/system/status"}' > /dev/null
ok "dev-bravo/file-sync (home-ops)"

# Work services (owned by work-ops)
WORK_AUTH="Authorization: Bearer $WORK_TOKEN"

curl -s -X POST "$REGISTRY/api/v1/services" -H "$WORK_AUTH" -H "Content-Type: application/json" \
  -d '{"name":"inference-api","device_id":"dev-charlie","port":8420,"description":"GPU inference — splatfacto, nerfacto, depth estimation","health_endpoint":"/health","subdomain":"inf","capabilities":["inference","export"]}' > /dev/null
ok "dev-charlie/inference-api (work-ops)"

curl -s -X POST "$REGISTRY/api/v1/services" -H "$WORK_AUTH" -H "Content-Type: application/json" \
  -d '{"name":"model-gateway","device_id":"dev-charlie","port":8450,"description":"LLM model serving — chat, generate, tool calling","health_endpoint":"/health","subdomain":"llm","capabilities":["chat","generate","model-lifecycle"]}' > /dev/null
ok "dev-charlie/model-gateway (work-ops)"

curl -s -X POST "$REGISTRY/api/v1/services" -H "$WORK_AUTH" -H "Content-Type: application/json" \
  -d '{"name":"render-engine","device_id":"dev-delta","port":8440,"description":"GPU rendering — splat preview, video export","health_endpoint":"/health","subdomain":"rndr","capabilities":["ply-render","preview-video"]}' > /dev/null
ok "dev-delta/render-engine (work-ops)"

curl -s -X POST "$REGISTRY/api/v1/services" -H "$WORK_AUTH" -H "Content-Type: application/json" \
  -d '{"name":"eval-service","device_id":"dev-delta","port":9000,"description":"Model evaluation and benchmarking","health_endpoint":"/health","subdomain":"eval","capabilities":["evaluate","leaderboard"]}' > /dev/null
ok "dev-delta/eval-service (work-ops)"

# ── Validate endpoints ──────────────────────────────────
echo "[seed] Validating endpoints..."
for EP in "/api/v1/" "/api/v1/status" "/api/v1/devices" "/api/v1/services" "/api/v1/groups" "/api/v1/routes" "/api/v1/gateway/status"; do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$REGISTRY$EP" -H "$AUTH")
  [ "$STATUS" = "200" ] && ok "$EP" || fail "$EP" "HTTP $STATUS"
done

# ── Validate guides ─────────────────────────────────────
echo "[seed] Validating guides..."
for SVC in "dev-alpha/media-server" "dev-charlie/inference-api"; do
  GUIDE_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$REGISTRY/api/v1/services/$SVC/guide" -H "$AUTH")
  [ "$GUIDE_STATUS" = "200" ] && ok "$SVC guide" || fail "$SVC guide" "HTTP $GUIDE_STATUS"
done

for USER_TOK_NAME in "home-ops:$HOME_TOKEN" "work-ops:$WORK_TOKEN"; do
  UNAME=$(echo "$USER_TOK_NAME" | cut -d: -f1)
  UTOK=$(echo "$USER_TOK_NAME" | cut -d: -f2-)
  GUIDE_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$REGISTRY/api/v1/users/me/guide" -H "Authorization: Bearer $UTOK")
  [ "$GUIDE_STATUS" = "200" ] && ok "$UNAME user guide" || fail "$UNAME user guide" "HTTP $GUIDE_STATUS"
done

# ── Visibility report ───────────────────────────────────
echo ""
echo "[seed] === Visibility Report ==="

for USER_SPEC in "admin:$TOKEN" "home-ops:$HOME_TOKEN" "work-ops:$WORK_TOKEN"; do
  UNAME=$(echo "$USER_SPEC" | cut -d: -f1)
  UTOK=$(echo "$USER_SPEC" | cut -d: -f2-)
  UAUTH="Authorization: Bearer $UTOK"

  DEVS=$(curl -s "$REGISTRY/api/v1/devices" -H "$UAUTH" | grep -o '"id"' | wc -l | tr -d ' ')
  SVCS=$(curl -s "$REGISTRY/api/v1/services" -H "$UAUTH" | grep -o '"id"' | wc -l | tr -d ' ')
  ROUTES=$(curl -s "$REGISTRY/api/v1/routes" -H "$UAUTH" | grep -o '"subdomain"' | wc -l | tr -d ' ')
  echo "[seed]   $UNAME: $DEVS devices, $SVCS services, $ROUTES routes"
done

# ── Summary ─────────────────────────────────────────────
echo ""
echo "[seed] === Summary ==="
curl -s "$REGISTRY/api/v1/status" -H "$AUTH" | \
  sed 's/.*"counts":{//' | sed 's/}.*//' | tr ',' '\n' | \
  sed 's/"//g' | sed 's/:/: /' | sed 's/^/[seed]   /'
echo ""
echo "[seed] Done"
echo "[seed]   admin/admin123 (full access)"
echo "[seed]   home-ops/home123 (group:home)"
echo "[seed]   work-ops/work123 (group:work)"
echo "[seed]   viewer/view123 (group:home, read-only)"
