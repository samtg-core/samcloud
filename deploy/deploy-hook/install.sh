#!/bin/bash
# Install or update the deploy receiver on the deploy host. Idempotent.
#
# The service runs an installed copy, not the checkout, so a deploy never rewrites
# the running receiver; after a change to deploy/deploy-hook/ run this once on the
# host:   ~/samcloud/deploy/deploy-hook/install.sh
#
# Copies receiver.py and askpass.sh to ~/.local/samcloud/deploy-hook/, renders the
# unit for the user running this (its home, and DEPLOY_HOOK_REPO_DIR from
# deploy-hook.env, else ~/samcloud), installs it when it changed, restarts, and
# prints /health (which names the receiver version and whether it announces on GitHub).
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/samcloud/deploy-hook"
ENVF="$HOME/.config/samcloud/deploy-hook.env"
UNIT=/etc/systemd/system/samcloud-deploy-hook.service

envval() { sed -n "s/^$1=//p" "$ENVF" 2>/dev/null | tail -1 | tr -d '[:space:]'; }
[ -f "$ENVF" ] || { echo "install.sh: $ENVF is missing; docs/Configuration.md lists its settings" >&2; exit 2; }
[ -n "$(envval DEPLOY_HOOK_REPO)" ] || { echo "install.sh: set DEPLOY_HOOK_REPO (owner/name) in $ENVF" >&2; exit 2; }
repo_dir=$(envval DEPLOY_HOOK_REPO_DIR); repo_dir=${repo_dir:-$HOME/samcloud}

mkdir -p "$DEST"
install -m 0644 "$SRC/receiver.py" "$DEST/receiver.py"
install -m 0755 "$SRC/askpass.sh" "$DEST/askpass.sh"
RENDERED=$(mktemp); trap 'rm -f "$RENDERED"' EXIT
sed -e "s#__USER__#$(id -un)#; s#__REPO_DIR__#$repo_dir#; s#__HOME__#$HOME#g" \
    "$SRC/samcloud-deploy-hook.service.in" > "$RENDERED"
if ! cmp -s "$RENDERED" "$UNIT"; then
    sudo install -m 0644 "$RENDERED" "$UNIT"
    sudo systemctl daemon-reload
fi
sudo systemctl restart samcloud-deploy-hook
sleep 1
echo "samcloud-deploy-hook: $(systemctl is-active samcloud-deploy-hook)"
PORT=$(envval DEPLOY_HOOK_PORT)
curl -s "localhost:${PORT:-9911}/health"; echo
