#!/bin/bash
# Simulated samcloud device
#
# Starts sshd + health server, then waits for a bootstrap URL
# written to /bootstrap/<device-id>.url by the seed container.
# Runs the bootstrap script the same way a real device would.

DEVICE_ID="${DEVICE_ID:-sim-device}"
DEVICE_TYPE="${DEVICE_TYPE:-server}"
HEALTH_PORT="${HEALTH_PORT:-8080}"
BOOTSTRAP_DIR="${BOOTSTRAP_DIR:-/bootstrap}"

echo "=== samcloud sim-device: $DEVICE_ID ($DEVICE_TYPE) ==="

# Start health server
python3 /opt/health-server.py --port "$HEALTH_PORT" --device-id "$DEVICE_ID" --device-type "$DEVICE_TYPE" &

# Start sshd (password auth enabled for bootstrap access)
/usr/sbin/sshd -e

# Wait for bootstrap URL from seed
BOOTSTRAP_FILE="$BOOTSTRAP_DIR/$DEVICE_ID.url"
echo "[$DEVICE_ID] Waiting for bootstrap at $BOOTSTRAP_FILE..."

for i in $(seq 1 60); do
    if [ -f "$BOOTSTRAP_FILE" ] && [ -s "$BOOTSTRAP_FILE" ]; then
        BOOTSTRAP_URL=$(cat "$BOOTSTRAP_FILE")
        echo "[$DEVICE_ID] Bootstrap URL received: ${BOOTSTRAP_URL:0:60}..."
        sleep 2

        # Download and run bootstrap. The script has a self-extract guard
        # (if [[ ! -t 0 ]]) that must be bypassed when running from file.
        # Use script(1) to provide a pseudo-terminal, or just skip past the
        # self-extract section by running with a fake tty.
        curl -sf "$BOOTSTRAP_URL" -o /tmp/bootstrap.sh
        chmod +x /tmp/bootstrap.sh
        # Strip the self-extract guard — we already have the file
        sed -i '/^if \[\[.*-t 0/,/^fi$/d' /tmp/bootstrap.sh
        bash /tmp/bootstrap.sh || true

        # Bootstrap runs as root, so gateway key lands in /root/.ssh/.
        # Copy to samcloud user (the SSH user the gateway connects as).
        if [ -f /root/.ssh/authorized_keys ]; then
            cp /root/.ssh/authorized_keys /home/samcloud/.ssh/authorized_keys
            chown samcloud:samcloud /home/samcloud/.ssh/authorized_keys
            chmod 600 /home/samcloud/.ssh/authorized_keys
            echo "[$DEVICE_ID] Bootstrap complete (gateway key installed for samcloud)"
        elif [ -f /home/samcloud/.ssh/authorized_keys ]; then
            echo "[$DEVICE_ID] Bootstrap complete (gateway key already in place)"
        else
            echo "[$DEVICE_ID] Bootstrap ran but no gateway key found"
        fi
        # Disable password auth now that key is installed
        sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
        kill -HUP $(cat /var/run/sshd.pid 2>/dev/null) 2>/dev/null || true
        break
    fi
    sleep 2
done

if [ ! -f "$BOOTSTRAP_FILE" ]; then
    echo "[$DEVICE_ID] No bootstrap after 120s — running without enrollment"
fi

# Keep container running
exec tail -f /dev/null
