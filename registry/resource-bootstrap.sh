#!/bin/bash
## resource-bootstrap.sh — Discover and register compute resources on this device
##
## Detects platform, discovers hardware (GPU, CPU, memory), registers with
## the samcloud registry, and installs a stats daemon that pushes utilisation.
##
## Usage: curl -s <registry>/api/v1/bootstrap/resource/<token> | bash
##
## Template variables (filled by registry):
##   __REGISTRY_URL__    — registry API base URL
##   __DEVICE_ID__       — device ID in the registry
##   __DEVICE_TOKEN__    — auth token for this device
##   __STATS_INTERVAL__  — seconds between stats pushes (default 15)

set -euo pipefail

REGISTRY="__REGISTRY_URL__"
DEVICE_ID="__DEVICE_ID__"
TOKEN="__DEVICE_TOKEN__"
INTERVAL="${__STATS_INTERVAL__:-15}"

# For standalone testing (not piped from registry)
if [ "$REGISTRY" = "__REGISTRY_URL__" ]; then
    REGISTRY="${SAMCLOUD_REGISTRY:-http://localhost:7778}"
    DEVICE_ID="${SAMCLOUD_DEVICE_ID:-local-test}"
    TOKEN="${SAMCLOUD_DEVICE_TOKEN:-}"
    INTERVAL="${SAMCLOUD_STATS_INTERVAL:-15}"
fi

if [ -z "$TOKEN" ]; then
    echo "Error: no auth token. Set SAMCLOUD_DEVICE_TOKEN or use the bootstrap URL."
    exit 1
fi

AUTH="Authorization: Bearer $TOKEN"
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo "  samcloud resource bootstrap"
echo "  device: $DEVICE_ID"
echo "  registry: $REGISTRY"
echo ""

# ── Platform detection ────────────────────────────────
OS=$(uname -s)
echo -n "Platform: "
case "$OS" in
    Darwin) echo "macOS (Apple Silicon)" ;;
    Linux)  echo "Linux" ;;
    *)      echo "Unsupported: $OS"; exit 1 ;;
esac

# ── Hardware discovery ────────────────────────────────
echo ""
echo "Discovering hardware..."

RESOURCES_JSON="[]"

if [ "$OS" = "Darwin" ]; then
    # Apple Silicon GPU
    GPU_INFO=$(system_profiler SPDisplaysDataType -json 2>/dev/null)
    GPU_MODEL=$(echo "$GPU_INFO" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for card in d.get('SPDisplaysDataType', []):
    name = card.get('sppci_model', 'Unknown GPU')
    cores = card.get('sppci_cores', 'unknown')
    print(f'{name}|{cores}')
    break
" 2>/dev/null || echo "Unknown|0")

    GPU_NAME=$(echo "$GPU_INFO" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for card in d.get('SPDisplaysDataType', []):
    print(card.get('sppci_model', 'Unknown GPU'))
    break
" 2>/dev/null || echo "Unknown GPU")

    GPU_CORES=$(echo "$GPU_INFO" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for card in d.get('SPDisplaysDataType', []):
    cores = card.get('sppci_cores', '0')
    print(cores)
    break
" 2>/dev/null || echo "0")

    # Memory
    TOTAL_MEM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo "0")
    TOTAL_MEM_MB=$((TOTAL_MEM_BYTES / 1048576))

    # Neural Engine cores (M-series)
    NE_CORES=$(system_profiler SPHardwareDataType -json 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
hw = d.get('SPHardwareDataType', [{}])[0]
# Apple doesn't expose NE core count directly; use known values
chip = hw.get('chip_type', '')
ne_map = {'M1': 16, 'M1 Pro': 16, 'M1 Max': 16, 'M1 Ultra': 32,
          'M2': 16, 'M2 Pro': 16, 'M2 Max': 16, 'M2 Ultra': 32,
          'M3': 16, 'M3 Pro': 16, 'M3 Max': 16, 'M4': 16, 'M4 Pro': 16, 'M4 Max': 16}
for k, v in ne_map.items():
    if k in chip: print(v); break
else: print(16)
" 2>/dev/null || echo "16")

    CPU_CORES=$(sysctl -n hw.ncpu 2>/dev/null || echo "0")

    echo -e "  ${GREEN}✓${NC} GPU: $GPU_NAME ($GPU_CORES cores)"
    echo -e "  ${GREEN}✓${NC} Memory: ${TOTAL_MEM_MB}MB unified"
    echo -e "  ${GREEN}✓${NC} Neural Engine: ${NE_CORES} cores"
    echo -e "  ${GREEN}✓${NC} CPU: ${CPU_CORES} cores"

    # Register GPU resource
    echo ""
    echo "Registering resources..."

    RESULT=$(curl -s -X POST "$REGISTRY/api/v1/resources" \
        -H "$AUTH" \
        -H "Content-Type: application/json" \
        -d "{
            \"name\": \"gpu-0\",
            \"device_id\": \"$DEVICE_ID\",
            \"type\": \"gpu\",
            \"provider\": \"apple\",
            \"model\": \"$GPU_NAME\",
            \"allocation_mode\": \"shared\",
            \"specs\": {
                \"unified_memory_mb\": $TOTAL_MEM_MB,
                \"gpu_cores\": $GPU_CORES,
                \"neural_engine_cores\": $NE_CORES,
                \"cpu_cores\": $CPU_CORES
            }
        }")
    STATUS=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status', json.load(open('/dev/stdin')).get('detail','error')))" 2>/dev/null <<< "$RESULT" || echo "error")
    if echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if 'resource_id' in d else 1)" 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} Registered $(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['resource_id'])")"
    else
        echo -e "  ${YELLOW}!${NC} $RESULT"
    fi

elif [ "$OS" = "Linux" ]; then
    # Check for NVIDIA GPU
    if command -v nvidia-smi &>/dev/null; then
        GPU_INFO=$(nvidia-smi --query-gpu=name,memory.total,index --format=csv,nounits,noheader 2>/dev/null)
        GPU_IDX=0
        while IFS=',' read -r GPU_NAME VRAM_MB IDX; do
            GPU_NAME=$(echo "$GPU_NAME" | xargs)
            VRAM_MB=$(echo "$VRAM_MB" | xargs)

            CUDA_CORES=$(nvidia-smi --query-gpu=name --format=csv,noheader -i "$GPU_IDX" 2>/dev/null | head -1)

            echo -e "  ${GREEN}✓${NC} GPU $GPU_IDX: $GPU_NAME (${VRAM_MB}MB VRAM)"

            RESULT=$(curl -s -X POST "$REGISTRY/api/v1/resources" \
                -H "$AUTH" \
                -H "Content-Type: application/json" \
                -d "{
                    \"name\": \"gpu-$GPU_IDX\",
                    \"device_id\": \"$DEVICE_ID\",
                    \"type\": \"gpu\",
                    \"provider\": \"nvidia\",
                    \"model\": \"$GPU_NAME\",
                    \"allocation_mode\": \"shared\",
                    \"specs\": {\"vram_mb\": $VRAM_MB}
                }")
            if echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if 'resource_id' in d else 1)" 2>/dev/null; then
                echo -e "  ${GREEN}✓${NC} Registered $(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['resource_id'])")"
            else
                echo -e "  ${YELLOW}!${NC} $(echo "$RESULT" | head -c 100)"
            fi

            GPU_IDX=$((GPU_IDX + 1))
        done <<< "$GPU_INFO"
    else
        echo -e "  ${YELLOW}!${NC} No NVIDIA GPU detected"
    fi

    # System memory
    TOTAL_MEM_MB=$(free -m | awk '/^Mem:/{print $2}')
    CPU_CORES=$(nproc 2>/dev/null || echo "0")
    echo -e "  ${GREEN}✓${NC} Memory: ${TOTAL_MEM_MB}MB"
    echo -e "  ${GREEN}✓${NC} CPU: ${CPU_CORES} cores"
fi

# ── Install stats daemon ──────────────────────────────
echo ""
echo "Installing stats daemon (interval: ${INTERVAL}s)..."

STATS_SCRIPT="$HOME/.local/samcloud/resource-stats.sh"
mkdir -p "$(dirname "$STATS_SCRIPT")"

if [ "$OS" = "Darwin" ]; then
    cat > "$STATS_SCRIPT" << 'STATS'
#!/bin/bash
# samcloud resource stats pusher (Apple Silicon)
REGISTRY="__REGISTRY__"
DEVICE_ID="__DEVID__"
TOKEN="__TOKEN__"

while true; do
    # Memory: use vm_stat
    TOTAL_MB=$(( $(sysctl -n hw.memsize) / 1048576 ))
    USED_MB=$(vm_stat 2>/dev/null | python3 -c "
import sys
pages = {}
for line in sys.stdin:
    parts = line.strip().rstrip('.').split(':')
    if len(parts) == 2:
        key = parts[0].strip()
        val = parts[1].strip().rstrip('.')
        try: pages[key] = int(val)
        except: pass
page_size = $(sysctl -n vm.pagesize 2>/dev/null || echo 16384)
active = pages.get('Pages active', 0)
wired = pages.get('Pages wired down', 0)
compressed = pages.get('Pages occupied by compressor', 0)
used_mb = (active + wired + compressed) * page_size // 1048576
print(used_mb)
" 2>/dev/null || echo "0")

    # GPU utilisation (approximate from memory pressure)
    PRESSURE=$(memory_pressure 2>/dev/null | grep "System-wide memory free percentage" | awk '{print $5}' | tr -d '%')
    COMPUTE_PCT=$(( 100 - ${PRESSURE:-50} ))

    # Temperature (requires powermetrics, which needs sudo — skip if not available)
    TEMP=""
    if [ -f /tmp/samcloud-gpu-temp ]; then
        TEMP=$(cat /tmp/samcloud-gpu-temp 2>/dev/null)
    fi
    TEMP_JSON=""
    if [ -n "$TEMP" ]; then
        TEMP_JSON=",\"temperature_c\":$TEMP"
    fi

    curl -s -X POST "$REGISTRY/api/v1/resources/$DEVICE_ID/gpu-0/stats" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"memory_used_mb\":$USED_MB,\"memory_total_mb\":$TOTAL_MB,\"compute_pct\":$COMPUTE_PCT$TEMP_JSON}" \
        > /dev/null 2>&1

    sleep __INTERVAL__
done
STATS

    # Fill in values
    sed -i '' "s|__REGISTRY__|$REGISTRY|g" "$STATS_SCRIPT"
    sed -i '' "s|__DEVID__|$DEVICE_ID|g" "$STATS_SCRIPT"
    sed -i '' "s|__TOKEN__|$TOKEN|g" "$STATS_SCRIPT"
    sed -i '' "s|__INTERVAL__|$INTERVAL|g" "$STATS_SCRIPT"
    chmod +x "$STATS_SCRIPT"

    # Install as LaunchAgent
    PLIST_PATH="$HOME/Library/LaunchAgents/com.samcloud.resource-stats.plist"
    cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.samcloud.resource-stats</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$STATS_SCRIPT</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/samcloud-resource-stats.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/samcloud-resource-stats.err</string>
</dict>
</plist>
PLIST

    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH"
    echo -e "  ${GREEN}✓${NC} Stats daemon installed (LaunchAgent)"

elif [ "$OS" = "Linux" ]; then
    cat > "$STATS_SCRIPT" << 'STATS'
#!/bin/bash
# samcloud resource stats pusher (Linux/NVIDIA)
REGISTRY="__REGISTRY__"
DEVICE_ID="__DEVID__"
TOKEN="__TOKEN__"

while true; do
    if command -v nvidia-smi &>/dev/null; then
        IDX=0
        nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw \
            --format=csv,nounits,noheader 2>/dev/null | while IFS=',' read -r MEM_USED MEM_TOTAL GPU_PCT TEMP POWER; do
            MEM_USED=$(echo "$MEM_USED" | xargs)
            MEM_TOTAL=$(echo "$MEM_TOTAL" | xargs)
            GPU_PCT=$(echo "$GPU_PCT" | xargs)
            TEMP=$(echo "$TEMP" | xargs)
            POWER=$(echo "$POWER" | xargs)

            # Get process list
            PROCS=$(nvidia-smi --query-compute-apps=pid,process_name,used_memory \
                --format=csv,nounits,noheader -i $IDX 2>/dev/null | \
                python3 -c "
import sys, json
procs = []
for line in sys.stdin:
    parts = [p.strip() for p in line.strip().split(',')]
    if len(parts) >= 3:
        procs.append({'pid': int(parts[0]), 'name': parts[1], 'memory_mb': int(parts[2])})
print(json.dumps(procs))
" 2>/dev/null || echo "[]")

            curl -s -X POST "$REGISTRY/api/v1/resources/$DEVICE_ID/gpu-$IDX/stats" \
                -H "Authorization: Bearer $TOKEN" \
                -H "Content-Type: application/json" \
                -d "{\"memory_used_mb\":$MEM_USED,\"memory_total_mb\":$MEM_TOTAL,\"compute_pct\":$GPU_PCT,\"temperature_c\":$TEMP,\"power_draw_w\":$POWER,\"processes\":$PROCS}" \
                > /dev/null 2>&1

            IDX=$((IDX + 1))
        done
    fi

    sleep __INTERVAL__
done
STATS

    sed -i "s|__REGISTRY__|$REGISTRY|g" "$STATS_SCRIPT"
    sed -i "s|__DEVID__|$DEVICE_ID|g" "$STATS_SCRIPT"
    sed -i "s|__TOKEN__|$TOKEN|g" "$STATS_SCRIPT"
    sed -i "s|__INTERVAL__|$INTERVAL|g" "$STATS_SCRIPT"
    chmod +x "$STATS_SCRIPT"

    # Install as systemd unit
    UNIT_FILE="/tmp/samcloud-resource-stats.service"
    cat > "$UNIT_FILE" << UNIT
[Unit]
Description=samcloud resource stats daemon
After=network.target

[Service]
Type=simple
ExecStart=/bin/bash $STATS_SCRIPT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

    sudo cp "$UNIT_FILE" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable samcloud-resource-stats
    sudo systemctl start samcloud-resource-stats
    echo -e "  ${GREEN}✓${NC} Stats daemon installed (systemd)"
fi

echo ""
echo -e "${GREEN}Resource bootstrap complete.${NC}"
echo "  Stats pushing every ${INTERVAL}s"
echo "  Dashboard: $REGISTRY/api/v1/resources/dashboard"
echo ""
