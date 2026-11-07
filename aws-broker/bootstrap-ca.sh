#!/usr/bin/env bash
# bootstrap-ca.sh — self-managed root CA + a leaf for this host, for IAM Roles Anywhere.
# Writes everything to ~/.config/samcloud/aws-broker/ at mode 600.
# Idempotent: re-run to see existing material; use --force to regenerate.

set -euo pipefail

DIR="${AWS_BROKER_CONFIG:-$HOME/.config/samcloud/aws-broker}"
HOST="${HOST:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
CA_DAYS=3650
LEAF_DAYS=365

mkdir -p "$DIR"
cd "$DIR"

if [[ "${1:-}" == "--force" ]]; then
  rm -f root-ca.key root-ca.crt "$HOST.key" "$HOST.csr" "$HOST.crt"
fi

# Root CA — needs critical basicConstraints CA:TRUE for Roles Anywhere
if [[ ! -f root-ca.key ]]; then
  echo "==> generating root CA"
  openssl genrsa -out root-ca.key 4096
  openssl req -x509 -new -nodes -key root-ca.key -sha256 -days "$CA_DAYS" \
    -subj "/CN=samcloud-aws-broker-root" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,digitalSignature,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" \
    -out root-ca.crt
  chmod 600 root-ca.key
else
  echo "==> root CA exists (root-ca.key)"
fi

# Leaf for this host — clientAuth EKU, not a CA
if [[ ! -f "$HOST.key" ]]; then
  echo "==> generating leaf cert for $HOST"
  openssl genrsa -out "$HOST.key" 2048
  openssl req -new -key "$HOST.key" -subj "/CN=$HOST" -out "$HOST.csr"
  cat > "$HOST.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=clientAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
  openssl x509 -req -in "$HOST.csr" -CA root-ca.crt -CAkey root-ca.key -CAcreateserial \
    -out "$HOST.crt" -days "$LEAF_DAYS" -sha256 -extfile "$HOST.ext"
  chmod 600 "$HOST.key"
  rm -f "$HOST.csr" "$HOST.ext"
else
  echo "==> leaf cert exists ($HOST.crt)"
fi

echo ""
echo "Material in: $DIR"
ls -la "$DIR" | grep -E "root-ca|$HOST"

echo ""
echo "==> root CA (paste this into Roles Anywhere trust anchor):"
cat root-ca.crt
