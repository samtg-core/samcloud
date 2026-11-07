#!/usr/bin/env bash
# aws-setup.sh -- provision AWS side of aws-broker.
# Creates: Roles Anywhere trust anchor + profile, two IAM roles.
# Writes ARNs/paths into ~/.config/samcloud/aws-broker/aws.json for main.py.

set -euo pipefail

PROFILE="${AWS_PROFILE_NAME:-default}"
REGION="${AWS_REGION:-us-east-1}"
DIR="${AWS_BROKER_CONFIG:-$HOME/.config/samcloud/aws-broker}"
HOST="${HOST:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"

BROKER_ROLE_NAME="${HOST}-aws-broker"
AGENT_ROLE_NAME="samcloud-agent"
TA_NAME="samcloud-${HOST}"
PROFILE_NAME="samcloud-broker-profile"

cd "$DIR"
[[ -f root-ca.crt && -f "$HOST.crt" && -f "$HOST.key" ]] || {
  echo "missing CA/leaf material -- run bootstrap-ca.sh first"; exit 1;
}

ACCOUNT=$(aws --profile "$PROFILE" sts get-caller-identity --query Account --output text)
echo "==> account: $ACCOUNT  region: $REGION  profile: $PROFILE"

# ---- Trust anchor ----------------------------------------------------------
TA_ARN=$(aws --profile "$PROFILE" --region "$REGION" rolesanywhere list-trust-anchors \
  --query "trustAnchors[?name=='$TA_NAME'].trustAnchorArn | [0]" --output text)
if [[ "$TA_ARN" == "None" || -z "$TA_ARN" ]]; then
  echo "==> creating trust anchor $TA_NAME"
  TA_PAYLOAD=$(python3 -c 'import json,sys; print(json.dumps({"sourceType":"CERTIFICATE_BUNDLE","sourceData":{"x509CertificateData":open("root-ca.crt").read()}}))')
  TA_ARN=$(aws --profile "$PROFILE" --region "$REGION" rolesanywhere create-trust-anchor \
    --name "$TA_NAME" --enabled \
    --source "$TA_PAYLOAD" \
    --query trustAnchor.trustAnchorArn --output text)
else
  echo "==> trust anchor exists: $TA_ARN"
fi

# ---- Broker role (what the cert assumes) -----------------------------------
# Trust policy: allow Roles Anywhere service to assume when presenting a cert
# issued by our trust anchor.
BROKER_TRUST=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "rolesanywhere.amazonaws.com"},
    "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"],
    "Condition": {"ArnEquals": {"aws:SourceArn": "$TA_ARN"}}
  }]
}
EOF
)

if ! aws --profile "$PROFILE" iam get-role --role-name "$BROKER_ROLE_NAME" >/dev/null 2>&1; then
  echo "==> creating role $BROKER_ROLE_NAME"
  aws --profile "$PROFILE" iam create-role \
    --role-name "$BROKER_ROLE_NAME" \
    --assume-role-policy-document "$BROKER_TRUST" \
    --max-session-duration 3600 \
    --description "aws-broker on ${HOST} -- assumed via Roles Anywhere" >/dev/null
else
  echo "==> role $BROKER_ROLE_NAME exists"
fi
BROKER_ROLE_ARN=$(aws --profile "$PROFILE" iam get-role --role-name "$BROKER_ROLE_NAME" \
  --query Role.Arn --output text)

# ---- Agent role (what gets minted to callers) ------------------------------
# Trust policy: only the broker role can assume this.
AGENT_TRUST=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "$BROKER_ROLE_ARN"},
    "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"]
  }]
}
EOF
)

if ! aws --profile "$PROFILE" iam get-role --role-name "$AGENT_ROLE_NAME" >/dev/null 2>&1; then
  echo "==> creating role $AGENT_ROLE_NAME"
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if aws --profile "$PROFILE" iam create-role \
      --role-name "$AGENT_ROLE_NAME" \
      --assume-role-policy-document "$AGENT_TRUST" \
      --max-session-duration 43200 \
      --description "samcloud agent role -- ceiling is AdministratorAccess; trust gate is samcloud auth, demarcation via session_policy" >/dev/null 2>&1; then
      break
    fi
    echo "   (attempt $attempt: broker role not yet propagated, retrying in 3s)"
    sleep 3
    [[ $attempt == 10 ]] && { echo "FAILED after 10 attempts"; exit 1; }
  done
  aws --profile "$PROFILE" iam attach-role-policy \
    --role-name "$AGENT_ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/AdministratorAccess >/dev/null
else
  echo "==> role $AGENT_ROLE_NAME exists"
  # keep trust policy current in case BROKER_ROLE_ARN changed
  aws --profile "$PROFILE" iam update-assume-role-policy \
    --role-name "$AGENT_ROLE_NAME" \
    --policy-document "$AGENT_TRUST" >/dev/null
fi
AGENT_ROLE_ARN=$(aws --profile "$PROFILE" iam get-role --role-name "$AGENT_ROLE_NAME" \
  --query Role.Arn --output text)

# ---- Profile (which roles the trust anchor can mint) -----------------------
PROFILE_ARN=$(aws --profile "$PROFILE" --region "$REGION" rolesanywhere list-profiles \
  --query "profiles[?name=='$PROFILE_NAME'].profileArn | [0]" --output text)
if [[ "$PROFILE_ARN" == "None" || -z "$PROFILE_ARN" ]]; then
  echo "==> creating Roles Anywhere profile $PROFILE_NAME"
  PROFILE_ARN=$(aws --profile "$PROFILE" --region "$REGION" rolesanywhere create-profile \
    --name "$PROFILE_NAME" --enabled \
    --role-arns "$BROKER_ROLE_ARN" \
    --duration-seconds 3600 \
    --query profile.profileArn --output text)
else
  echo "==> Roles Anywhere profile exists: $PROFILE_ARN"
  aws --profile "$PROFILE" --region "$REGION" rolesanywhere update-profile \
    --profile-id "${PROFILE_ARN##*/}" \
    --role-arns "$BROKER_ROLE_ARN" >/dev/null
fi

# ---- Write config ----------------------------------------------------------
cat > "$DIR/aws.json" <<EOF
{
  "account": "$ACCOUNT",
  "region": "$REGION",
  "trust_anchor_arn": "$TA_ARN",
  "profile_arn": "$PROFILE_ARN",
  "broker_role_arn": "$BROKER_ROLE_ARN",
  "agent_roles": {
    "samcloud-agent": "$AGENT_ROLE_ARN"
  },
  "certificate_path": "$DIR/$HOST.crt",
  "private_key_path": "$DIR/$HOST.key",
  "broker_session_duration": 3600
}
EOF
chmod 600 "$DIR/aws.json"

echo ""
echo "==> wrote $DIR/aws.json"
cat "$DIR/aws.json"
