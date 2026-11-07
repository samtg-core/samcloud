#!/bin/bash
## ddns-update.sh — Update a domain's A records via the Namecheap API
## Preserves ALL existing records when updating IP.
## Run via cron every 5 minutes:
##   */5 * * * * /path/to/ddns-update.sh >> ~/Library/Logs/samcloud/ddns.log 2>&1
##
## Required env vars (set in crontab, or in ~/.config/samcloud/namecheap.env):
##   SAMCLOUD_DOMAIN    — the zone to update, e.g. example.org
##   NAMECHEAP_API_USER — Namecheap username
##   NAMECHEAP_API_KEY  — Namecheap API key

set -euo pipefail

IP_CHECK_URL="https://ifconfig.me"
IP_CACHE_FILE="${HOME}/.cache/samcloud/ddns-ip"

# Load credentials from env file if present
CREDS_FILE="${HOME}/.config/samcloud/namecheap.env"
if [ -f "$CREDS_FILE" ]; then
    # shellcheck source=/dev/null
    source "$CREDS_FILE"
fi

if [ -z "${NAMECHEAP_API_USER:-}" ] || [ -z "${NAMECHEAP_API_KEY:-}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [error] NAMECHEAP_API_USER and NAMECHEAP_API_KEY must be set"
    exit 1
fi
DOMAIN="${SAMCLOUD_DOMAIN:-}"
case "$DOMAIN" in
    *.*) ;;
    *) echo "$(date '+%Y-%m-%d %H:%M:%S') [error] SAMCLOUD_DOMAIN must name the zone to update, e.g. example.org"
       exit 1 ;;
esac
SLD="${DOMAIN%%.*}"     # Namecheap splits a domain at its first dot: example.co.uk -> example + co.uk
TLD="${DOMAIN#*.}"

# Get current public IP
CURRENT_IP=$(curl -s --max-time 10 "$IP_CHECK_URL" 2>/dev/null || true)

if [ -z "$CURRENT_IP" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [error] Failed to get public IP"
    exit 1
fi

# Check if IP has changed
mkdir -p "$(dirname "$IP_CACHE_FILE")"
if [ -f "$IP_CACHE_FILE" ]; then
    CACHED_IP=$(cat "$IP_CACHE_FILE")
    if [ "$CURRENT_IP" = "$CACHED_IP" ]; then
        # IP unchanged, skip update
        exit 0
    fi
fi

# Get current records first (setHosts replaces ALL records)
API_URL="https://api.namecheap.com/xml.response"
GET_RESPONSE=$(curl -s --max-time 15 "${API_URL}?ApiUser=${NAMECHEAP_API_USER}&ApiKey=${NAMECHEAP_API_KEY}&UserName=${NAMECHEAP_API_USER}&ClientIp=${CURRENT_IP}&Command=namecheap.domains.dns.getHosts&SLD=${SLD}&TLD=${TLD}" 2>/dev/null || true)

if ! echo "$GET_RESPONSE" | grep -q 'Status="OK"'; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [error] Failed to get current records"
    exit 1
fi

# Parse existing A records and rebuild with new IP (macOS-compatible)
# Extract <host> elements and build params
PARAMS="ApiUser=${NAMECHEAP_API_USER}&ApiKey=${NAMECHEAP_API_KEY}&UserName=${NAMECHEAP_API_USER}&ClientIp=${CURRENT_IP}&Command=namecheap.domains.dns.setHosts&SLD=${SLD}&TLD=${TLD}"

IDX=1
# Parse each <host> line: extract Name and Type attributes
echo "$GET_RESPONSE" | tr '>' '\n' | grep '<host ' | while read -r line; do
    host=$(echo "$line" | sed -n 's/.*Name="\([^"]*\)".*/\1/p')
    type=$(echo "$line" | sed -n 's/.*Type="\([^"]*\)".*/\1/p')
    if [ "$type" = "A" ] && [ -n "$host" ]; then
        TTL=1800
        if [ "$host" = "tunnel" ]; then
            TTL=300
        fi
        echo "${host}|${TTL}"
    fi
done | {
    while IFS='|' read -r host ttl; do
        PARAMS="${PARAMS}&HostName${IDX}=${host}&RecordType${IDX}=A&Address${IDX}=${CURRENT_IP}&TTL${IDX}=${ttl}"
        IDX=$((IDX + 1))
    done
    RECORD_COUNT=$((IDX - 1))

    # Export for the curl below
    echo "${PARAMS}|${RECORD_COUNT}"
} | {
    IFS='|' read -r FINAL_PARAMS RECORD_COUNT
    # Update all records
    RESPONSE=$(curl -s --max-time 15 "${API_URL}?${FINAL_PARAMS}" 2>/dev/null || true)

    if echo "$RESPONSE" | grep -q 'Status="OK"'; then
        echo "$CURRENT_IP" > "$IP_CACHE_FILE"
        echo "$(date '+%Y-%m-%d %H:%M:%S') [ok] Updated ${DOMAIN} (${RECORD_COUNT} records) → ${CURRENT_IP}"
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') [error] DNS update failed. Response:"
        echo "$RESPONSE" | head -5
        exit 1
    fi
}

# (update handled in pipe above)
