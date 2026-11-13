#!/usr/bin/env bash
# mac-env.sh — a Mac sub-user as an agent environment, handled like a container:
# create it, start it, stop it, remove it.
#
#   scripts/mac-env.sh create <name> (--ip IP | --via DEVICE) [--device ID]
#                                   [--group G[:role]]... [--no-rail] [--skip-preflight]
#   scripts/mac-env.sh start  <name>
#   scripts/mac-env.sh stop   <name>
#   scripts/mac-env.sh remove <name> [--keep-home]
#   scripts/mac-env.sh status <name>
#   scripts/mac-env.sh list
#
# Run it from an admin account, not under sudo. It asks sudo for the steps that
# need root, and makes the plane calls with your own admin token
# ($SAMCLOUD_TOKEN_HDR, default ~/.samcloud/token.hdr). mac-env.md, beside this
# file, says how to use it.
#
# An environment is four things, made by `create` and unmade by `remove`:
#   - a standard macOS account <name>: no admin, no sudo, password never written
#     down, reachable by the gateway's key only;
#   - a device row <device> (default <host>-<name>) the gateway enters it by;
#   - an agent identity <name>, scoped to that device and any --group, with its
#     token in the account's ~/.samcloud;
#   - the rail, installed PARKED (RAIL_DRY=1) behind a LaunchDaemon. `start` and
#     `stop` load and unload that daemon. Unparking is a separate decision.
#
# `create` writes a root-owned record to /var/db/samcloud-envs/<name>. `start`,
# `stop` and `remove` act only on a name with that record, so this script can
# never stop or delete an account it did not make.
set -euo pipefail

prog=${0##*/}
die()  { printf '%s: %s\n' "$prog" "$1" >&2; exit "${2:-1}"; }
say()  { printf '%s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*" >&2; }

STATE_DIR=/var/db/samcloud-envs
HDR="${SAMCLOUD_TOKEN_HDR:-$HOME/.samcloud/token.hdr}"
PY=/usr/bin/python3

usage() { sed -n '5,11p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

# ── the plane ─────────────────────────────────────────────────────────────────
plane_api() {
  # The admin's API base: $SAMCLOUD_API, else the API= line of this account's
  # wake.env (every seat that runs a rail has one).
  local a="${SAMCLOUD_API:-}"
  [ -n "$a" ] || a=$(sed -n 's/^API=//p' "$HOME/.config/samcloud/wake.env" 2>/dev/null | tr -d '"' | tail -1)
  [ -n "$a" ] || die "set SAMCLOUD_API to the plane's API base, e.g. https://cloud.example.org/api/v1" 2
  printf '%s' "${a%/}"
}

# api METHOD PATH [JSON]: the body on stdout; on HTTP >= 400 the body goes to
# stderr and the call fails. The token stays in the header file.
api() {
  local method=$1 path=$2 data=${3:-} out code
  out=$(mktemp)
  if [ -n "$data" ]; then
    code=$(printf '%s' "$data" | curl -sS -o "$out" -w '%{http_code}' -X "$method" \
             -H @"$HDR" -H 'Content-Type: application/json' --data-binary @- "$API$path") || code=000
  else
    code=$(curl -sS -o "$out" -w '%{http_code}' -X "$method" -H @"$HDR" "$API$path") || code=000
  fi
  if [ "$code" -ge 400 ] || [ "$code" = 000 ]; then
    printf '%s %s answered %s: %s\n' "$method" "$path" "$code" "$(head -c 400 "$out")" >&2
    rm -f "$out"; return 1
  fi
  cat "$out"; rm -f "$out"
}

# on_plane PATH: 0 if the row is there, 1 if the plane says 404, 2 for any other answer.
on_plane() {
  local code
  code=$(curl -sS -o /dev/null -w '%{http_code}' -H @"$HDR" "$API$1") || code=000
  case "$code" in 2??) return 0 ;; 404) return 1 ;; *) warn "GET $1 answered $code"; return 2 ;; esac
}

# jget KEY: one field of the JSON object on stdin, without a trailing newline.
jget() { "$PY" -c 'import json,sys; v=json.load(sys.stdin).get(sys.argv[1]); sys.stdout.write("" if v is None else str(v))' "$1"; }

# ── the record ────────────────────────────────────────────────────────────────
rec()  { printf '%s/%s' "$STATE_DIR" "$NAME"; }
rget() { sed -n "s/^$1=//p" "$(rec)" 2>/dev/null | tail -1; }
radd() { printf '%s=%s\n' "$1" "$2" | sudo tee -a "$(rec)" >/dev/null; }

require_record() {
  local f; f=$(rec)
  [ -f "$f" ] || die "no environment named '$NAME' (no $f). $prog only acts on what it created."
  [ "$(stat -f %u "$f")" = 0 ] || die "$f is not owned by root; refusing to trust it"
  UIDN=$(rget uid); DEVICE=$(rget device); USER_ID=$(rget user_id)
  printf '%s' "$UIDN" | grep -Eq '^[0-9]+$' && [ "$UIDN" -ge 501 ] && [ "$UIDN" != "$(id -u)" ] \
    || die "$f records uid '$UIDN', which is not an environment's; refusing"
  HOMEDIR="/Users/$NAME"
  LABEL="com.samcloud.listen.$NAME"; PLIST="/Library/LaunchDaemons/$LABEL.plist"
  if id -u "$NAME" >/dev/null 2>&1; then
    [ "$(id -u "$NAME")" = "$UIDN" ] || die "account $NAME has uid $(id -u "$NAME"), the record says $UIDN; refusing"
    if dsmemberutil checkmembership -U "$NAME" -G admin 2>/dev/null | grep -q 'is a member'; then
      die "$NAME is an admin account; $prog never touches one"
    fi
  fi
}

check_name() {
  case "$NAME" in
    ""|-*) usage ;;
  esac
  printf '%s' "$NAME" | grep -Eq '^[a-z][a-z0-9-]{1,30}$' \
    || die "name must be 2–31 lowercase letters, digits and dashes, starting with a letter" 2
  [ "$NAME" != "$(id -un)" ] || die "that is your own account" 2
}

loaded()  { launchctl print "system/$LABEL" >/dev/null 2>&1; }
running() { pgrep -u "$NAME" -f 'bin/samcloud-listen' 2>/dev/null | head -1; }

# Run a command as the environment's account, from /, with its own HOME (this
# host's sudoers keeps the caller's HOME unless told otherwise).
as_env() {
  (cd / && sudo -H -u "$NAME" env -i HOME="$HOMEDIR" USER="$NAME" LOGNAME="$NAME" \
     PATH="$HOMEDIR/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
     "$@")
}

# ── create ────────────────────────────────────────────────────────────────────
cmd_create() {
  local ip="" via="" groups=() want_rail=1 skip_pf=0
  DEVICE=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --ip)       ip="${2:-}"; shift 2 ;;
      --via)      via="${2:-}"; shift 2 ;;
      --device)   DEVICE="${2:-}"; shift 2 ;;
      --group)    groups+=("${2:-}"); shift 2 ;;
      --no-rail)  want_rail=0; shift ;;
      --skip-preflight) skip_pf=1; shift ;;
      *) die "create: unknown option '$1'" 2 ;;
    esac
  done
  [ -n "$ip$via" ] && { [ -z "$ip" ] || [ -z "$via" ]; } \
    || die "create needs exactly one of --ip IP (the gateway reaches this Mac directly) or --via DEVICE (borrow that device's tunnel)" 2
  [ -z "$ip" ] || printf '%s' "$ip" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' || die "--ip: not an IPv4 address" 2
  local host="${MAC_ENV_HOST:-$(hostname -s | tr 'A-Z' 'a-z')}"
  DEVICE="${DEVICE:-$host-$NAME}"
  printf '%s' "$DEVICE${via:+ $via}" | grep -Eq '^[a-z0-9][a-z0-9 -]*$' || die "device ids are lowercase letters, digits and dashes" 2
  [ "$DEVICE" != "$NAME" ] || die "the device id must differ from the name: the plane refuses a device id that is also a username" 2
  local g
  for g in ${groups[@]+"${groups[@]}"}; do
    printf '%s' "$g" | grep -Eq '^[a-z0-9_-]+(:(viewer|editor|owner))?$' || die "--group takes G or G:viewer|editor|owner" 2
  done

  HOMEDIR="/Users/$NAME"; LABEL="com.samcloud.listen.$NAME"; PLIST="/Library/LaunchDaemons/$LABEL.plist"
  [ ! -e "$(rec)" ] || die "'$NAME' already exists; '$prog status $NAME' shows it, '$prog start $NAME' starts it"
  ! dscl . -read "/Users/$NAME" >/dev/null 2>&1 || die "a macOS account '$NAME' already exists and $prog did not make it"
  [ ! -e "$HOMEDIR" ] || die "$HOMEDIR already exists"

  # The plane first, before anything changes: our token works, and both names are free.
  api GET /auth/verify >/dev/null || die "the plane refused $HDR"
  local rc=0; on_plane "/devices/$DEVICE" || rc=$?
  case "$rc" in 0) die "device '$DEVICE' is already on the plane" ;; 2) die "could not ask the plane about device '$DEVICE'" ;; esac
  local users; users=$(api GET /users) || die "could not list the plane's identities"
  if printf '%s' "$users" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); d=d.get("users",d) if isinstance(d,dict) else d; sys.exit(0 if any(u.get("username")==sys.argv[1] for u in d) else 1)' "$NAME"; then
    die "identity '$NAME' is already on the plane"
  fi
  local gw_pub
  gw_pub=$(curl -fsS "$API/gateway/pubkey" | jget public_key) || die "could not read the gateway's key from $API/gateway/pubkey"
  printf '%s' "$gw_pub" | grep -q '^ssh-' || die "$API/gateway/pubkey did not answer a public key"

  say "== creating $NAME (device $DEVICE) — sudo asks once"
  sudo -v

  # 1. the account. sysadminctl wants a password; it is random and dropped.
  local pw; pw=$(openssl rand -base64 24)
  sudo sysadminctl -addUser "$NAME" -fullName "$NAME (samcloud environment)" \
       -password "$pw" -home "$HOMEDIR" -shell /bin/zsh 2>&1 | grep -v '^$' || true
  unset pw
  id -u "$NAME" >/dev/null 2>&1 || die "sysadminctl did not create $NAME"
  UIDN=$(id -u "$NAME")
  # The record goes down the moment the account exists, so `remove` can undo a
  # create that stops anywhere after this line.
  sudo install -d -m 755 -o root -g wheel "$STATE_DIR"
  printf 'name=%s\nuid=%s\ncreated=%s\ncreated_by=%s\nhost=%s\napi=%s\n' \
    "$NAME" "$UIDN" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(id -un)" "$("$PY" -c 'import socket; print(socket.gethostname())')" "$API" \
    | sudo tee "$(rec)" >/dev/null
  sudo chmod 644 "$(rec)"
  sudo createhomedir -c -u "$NAME" >/dev/null 2>&1 || true
  sudo dseditgroup -o edit -a "$NAME" -t user com.apple.access_ssh
  sudo dseditgroup -o edit -a "$NAME" -t user _developer
  say "[ok] account $NAME (uid $UIDN), in com.apple.access_ssh and _developer; record $(rec)"

  # 2. the gateway's key — the one the running gateway publishes, not a file on
  #    this disk (a stale copy hangs every hop; gateway/main.go).
  sudo install -d -m 700 -o "$NAME" -g staff "$HOMEDIR/.ssh"
  printf '%s\n' "$gw_pub" | sudo tee "$HOMEDIR/.ssh/authorized_keys" >/dev/null
  sudo chown "$NAME":staff "$HOMEDIR/.ssh/authorized_keys"
  sudo chmod 600 "$HOMEDIR/.ssh/authorized_keys"
  printf 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"\n' | sudo tee "$HOMEDIR/.zshenv" >/dev/null
  sudo chown "$NAME":staff "$HOMEDIR/.zshenv"
  say "[ok] the gateway's key is $NAME's only authorized key"

  # 3. the device row and the identity.
  local dev_json
  if [ -n "$ip" ]; then
    dev_json=$(printf '{"id":"%s","name":"%s","type":"device","outbound_method":"direct-ssh","outbound_ip":"%s","outbound_ssh_user":"%s","outbound_ssh_port":22,"startup_method":"launchd"}' "$DEVICE" "$DEVICE" "$ip" "$NAME")
  else
    dev_json=$(printf '{"id":"%s","name":"%s","type":"device","transport_owner":"%s","outbound_ssh_user":"%s","startup_method":"launchd"}' "$DEVICE" "$DEVICE" "$via" "$NAME")
  fi
  api POST /devices "$dev_json" >/dev/null || die "the device row was refused; '$prog remove $NAME' undoes the account"
  radd device "$DEVICE"
  if [ -n "$ip" ]; then say "[ok] device $DEVICE (direct-ssh $ip, user $NAME)"; else say "[ok] device $DEVICE (through the tunnel of $via, user $NAME)"; fi

  local scopes="\"device:$DEVICE\"" roles="" gid grole
  for g in ${groups[@]+"${groups[@]}"}; do
    gid=${g%%:*}; grole=${g#*:}; [ "$grole" != "$g" ] || grole=""
    api POST "/groups/$gid/devices" "{\"device_id\":\"$DEVICE\"}" >/dev/null || warn "device $DEVICE did not join group $gid"
    scopes="$scopes,\"group:$gid\""
    [ -z "$grole" ] || roles="$roles${roles:+,}\"$gid\":\"$grole\""
  done
  local inv user_id invite gr=""
  [ -z "$roles" ] || gr=",\"group_roles\":{$roles}"
  inv=$(api POST /users/invite "{\"username\":\"$NAME\",\"role\":\"agent\",\"scopes\":[$scopes]$gr,\"label\":\"mac-env on $DEVICE\",\"ttl\":600}") \
    || die "the identity was refused; '$prog remove $NAME' undoes the rest"
  user_id=$(printf '%s' "$inv" | jget user_id); invite=$(printf '%s' "$inv" | jget token); unset inv
  radd user_id "$user_id"

  # Activate here: an environment is made whole by its maker. The guide the
  # invite would have shown is the identity's GET /users/me/guide for good.
  local act_pw act
  act_pw=$(openssl rand -base64 24)
  act=$(printf '{"token":"%s","password":"%s"}' "$invite" "$act_pw" \
        | curl -sS -X POST -H 'Content-Type: application/json' --data-binary @- "$API/users/activate")
  unset invite act_pw
  printf '%s' "$act" | jget token | as_env /bin/sh -c 'umask 077; mkdir -p "$HOME/.samcloud" &&
      cat > "$HOME/.samcloud/token" && [ -s "$HOME/.samcloud/token" ] &&
      printf "Authorization: Bearer %s\n" "$(cat "$HOME/.samcloud/token")" > "$HOME/.samcloud/token.hdr"' \
    || die "activation did not give a token; '$prog remove $NAME' undoes the rest"
  unset act
  local who
  who=$(as_env curl -sS -H @"$HOMEDIR/.samcloud/token.hdr" "$API/auth/verify" \
        | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("username") or d.get("owner_id"))')
  case "$who" in "$NAME"|"user:$user_id") ;; *) die "the token in $HOMEDIR/.samcloud answers as '$who', not $NAME" ;; esac
  say "[ok] identity $NAME (user $user_id), scopes [$scopes]; its token is in $HOMEDIR/.samcloud (0600)"

  # 4. the rail, parked, behind a LaunchDaemon that is installed but not loaded.
  if [ "$want_rail" -eq 1 ]; then
    local pass=(RAIL_DRY=1 NO_SUPERVISE=1 API="$API") v
    [ "$skip_pf" -eq 0 ] || pass+=(SKIP_PREFLIGHT=1)
    for v in RAIL_RUNTIME RAIL_RUNTIME_CMD RAIL_RUNTIME_RESUME RAIL_RUNTIME_CONV_CMD RAIL_RUNTIME_CONV_RE \
             RAIL_RUNTIME_LEASE MODEL MAX_SESSION_MIN PERMISSION_ARGS WAKE_CMD PREFLIGHT_CMD; do
      [ -z "${!v:-}" ] || pass+=("$v=${!v}")
    done
    as_env "${pass[@]}" /bin/bash -c 'f=$(mktemp) && curl -fsS -H @"$HOME/.samcloud/token.hdr" "$API/helpers/wake-install" -o "$f" &&
        bash "$f"; rc=$?; rm -f "$f"; exit $rc' \
      | sed 's/^/    /' || die "wake-install failed as $NAME (above); fix it and re-run it as $NAME, or '$prog remove $NAME'"
    [ -x "$HOMEDIR/.local/bin/samcloud-listen" ] || sudo test -f "$HOMEDIR/.local/bin/samcloud-listen" \
      || die "wake-install left no samcloud-listen in $HOMEDIR/.local/bin"
    # Written here rather than copied from the account: a root-loaded plist is
    # the admin's text, not the environment's. Same keys as wake-install's.
    sudo tee "$PLIST" >/dev/null <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$HOMEDIR/.local/bin/samcloud-listen</string>
    </array>
    <key>UserName</key><string>$NAME</string>
    <key>WorkingDirectory</key><string>$HOMEDIR</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$HOMEDIR/logs/samcloud-listen.out</string>
    <key>StandardErrorPath</key><string>$HOMEDIR/logs/samcloud-listen.out</string>
</dict>
</plist>
EOF
    sudo chown root:wheel "$PLIST"; sudo chmod 644 "$PLIST"
    plutil -lint "$PLIST" >/dev/null || die "$PLIST does not lint"
    sudo launchctl disable "system/$LABEL"     # stays down, across reboots too, until `start`
    radd rail parked
    say "[ok] rail installed parked (RAIL_DRY=1); $PLIST is in place and not loaded"
  else
    radd rail none
    say "[ok] no rail (--no-rail): this identity takes part by polling"
  fi

  say ""
  say "[done] $NAME is created and stopped."
  say "       start:  $prog start $NAME"
  say "       reach:  ssh samcloud \"$DEVICE -- whoami\"   (prints $NAME)"
}

# ── start / stop ──────────────────────────────────────────────────────────────
cmd_start() {
  require_record
  [ "$(rget rail)" != none ] || die "$NAME was created with --no-rail; there is nothing to start"
  [ -f "$PLIST" ] || die "$PLIST is missing; '$prog remove $NAME' and create it again"
  sudo launchctl enable "system/$LABEL"
  if loaded; then
    sudo launchctl kickstart "system/$LABEL"
  else
    sudo launchctl bootstrap system "$PLIST"
  fi
  local pid="" i
  for i in 1 2 3 4 5; do pid=$(running); [ -n "$pid" ] && break; sleep 1; done
  if [ -n "$pid" ]; then
    say "[ok] $NAME is running: samcloud-listen pid $pid (log: $HOMEDIR/logs/samcloud-listen.out)"
  else
    warn "$LABEL is loaded but no samcloud-listen is running yet; '$prog status $NAME'"
  fi
}

stop_env() {
  if loaded; then sudo launchctl bootout "system/$LABEL" 2>/dev/null || true; fi
  [ ! -f "$PLIST" ] || sudo launchctl disable "system/$LABEL"
  if pgrep -u "$UIDN" >/dev/null 2>&1; then
    sudo pkill -TERM -u "$UIDN" || true
    local i; for i in 1 2 3 4 5; do pgrep -u "$UIDN" >/dev/null 2>&1 || break; sleep 1; done
    if pgrep -u "$UIDN" >/dev/null 2>&1; then sudo pkill -KILL -u "$UIDN" || true; sleep 1; fi
  fi
  if pgrep -u "$UIDN" >/dev/null 2>&1; then
    warn "still running as $NAME: $(pgrep -u "$UIDN" | tr '\n' ' ')"; return 1
  fi
}

cmd_stop() {
  require_record
  stop_env && say "[ok] $NAME is stopped: $LABEL unloaded and disabled, no processes as $NAME"
}

# ── remove ────────────────────────────────────────────────────────────────────
cmd_remove() {
  local keep_home=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --keep-home) keep_home=1; shift ;;
      *) die "remove: unknown option '$1'" 2 ;;
    esac
  done
  require_record
  local failed=0
  say "== removing $NAME — sudo asks once"
  sudo -v
  if id -u "$NAME" >/dev/null 2>&1; then
    stop_env || failed=1
  fi

  # The plane: the rail's registration (while the identity still exists), the
  # identity, the device. Each one is checked by name before it goes.
  if [ -n "$USER_ID" ]; then
    local uname rc=0
    on_plane "/users/$USER_ID" || rc=$?
    if [ "$rc" -eq 0 ]; then
      uname=$(api GET "/users/$USER_ID" | jget username)
      if [ "$uname" = "$NAME" ]; then
        api POST /wake/register "{\"ended\":true,\"identity\":\"$NAME\",\"host\":\"$(rget host)\"}" >/dev/null 2>&1 \
          && say "[ok] retired $NAME's rail registration at $(rget host)"
        api DELETE "/users/$USER_ID" >/dev/null && say "[ok] identity $NAME (user $USER_ID) deleted" || failed=1
      else
        warn "user $USER_ID is '$uname', not $NAME; left alone"; failed=1
      fi
    elif [ "$rc" -eq 1 ]; then
      say "[ok] identity $NAME (user $USER_ID) is already gone"
    else
      failed=1
    fi
  fi
  if [ -n "$DEVICE" ]; then
    local rc=0
    on_plane "/devices/$DEVICE" || rc=$?
    case "$rc" in
      0) api DELETE "/devices/$DEVICE" >/dev/null && say "[ok] device $DEVICE deleted" || failed=1 ;;
      1) say "[ok] device $DEVICE is already gone" ;;
      *) failed=1 ;;
    esac
  fi

  # The host: the daemon, then the account and its home.
  if [ -f "$PLIST" ]; then
    sudo launchctl enable "system/$LABEL"      # drops the disabled override with the job
    sudo rm -f "$PLIST" && say "[ok] removed $PLIST"
  fi
  if id -u "$NAME" >/dev/null 2>&1; then
    if [ "$keep_home" -eq 1 ]; then
      sudo sysadminctl -deleteUser "$NAME" -keepHome 2>&1 | grep -v '^$' || true
    else
      sudo sysadminctl -deleteUser "$NAME" 2>&1 | grep -v '^$' || true
    fi
    if id -u "$NAME" >/dev/null 2>&1; then
      warn "account $NAME is still there"; failed=1
    else
      say "[ok] account $NAME deleted$([ "$keep_home" -eq 1 ] && echo ", home kept" || echo " with its home")"
    fi
  fi
  if [ "$keep_home" -eq 0 ] && [ -e "$HOMEDIR" ]; then
    if [ "$(sudo stat -f %u "$HOMEDIR")" = "$UIDN" ]; then
      sudo rm -rf "$HOMEDIR" && say "[ok] removed $HOMEDIR"
    else
      warn "$HOMEDIR is not owned by uid $UIDN; left alone"; failed=1
    fi
  fi

  if [ "$failed" -eq 0 ]; then
    sudo rm -f "$(rec)"
    say "[done] $NAME is removed."
  else
    die "some steps did not finish (above); the record stays, so '$prog remove $NAME' can finish them"
  fi
}

# ── status / list ─────────────────────────────────────────────────────────────
state_of() {
  if ! id -u "$NAME" >/dev/null 2>&1; then echo "no-account"
  elif [ "$(rget rail)" = none ]; then echo "no-rail"
  elif [ -n "$(running)" ]; then echo "running"
  else echo "stopped"
  fi
}

cmd_status() {
  require_record
  say "name      $NAME ($(state_of))"
  say "account   uid $UIDN, home $HOMEDIR$(id -u "$NAME" >/dev/null 2>&1 || echo ' — MISSING')"
  say "created   $(rget created) by $(rget created_by)"
  if [ "$(rget rail)" != none ]; then
    local pid; pid=$(running)
    say "rail      $LABEL $(loaded && echo loaded || echo 'not loaded')${pid:+, samcloud-listen pid $pid}"
  fi
  local n; n=$(pgrep -u "$UIDN" 2>/dev/null | wc -l | tr -d ' ')
  say "processes $n as $NAME"
  if [ -n "$DEVICE" ]; then
    local d; d=$(api GET "/devices/$DEVICE" 2>/dev/null | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status"), d.get("outbound_method"), d.get("outbound_ip") or d.get("transport_owner") or "")' 2>/dev/null) || d="not on the plane"
    say "device    $DEVICE: $d"
  fi
  if [ -n "$USER_ID" ]; then
    local u; u=$(api GET "/users/$USER_ID" 2>/dev/null | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("username"), d.get("role"), d.get("scopes"))' 2>/dev/null) || u="not on the plane"
    say "identity  user $USER_ID: $u"
  fi
}

cmd_list() {
  local f found=0
  printf '%-24s %-32s %s\n' NAME DEVICE STATE
  for f in "$STATE_DIR"/*; do
    [ -f "$f" ] || continue
    NAME=${f##*/}; found=1
    [ "$(stat -f %u "$f")" = 0 ] || { printf '%-24s %-32s %s\n' "$NAME" "?" "untrusted record"; continue; }
    LABEL="com.samcloud.listen.$NAME"
    printf '%-24s %-32s %s\n' "$NAME" "$(rget device)" "$(state_of)"
  done
  [ "$found" -eq 1 ] || say "(no environments on this Mac)"
}

# ── main ──────────────────────────────────────────────────────────────────────
[ "$(uname -s)" = Darwin ] || die "this is for macOS" 2
[ "$(id -u)" -ne 0 ] || die "run me as an admin account, not under sudo: I call the plane with your token and ask sudo myself" 2
[ $# -ge 1 ] || usage
sub=$1; shift
case "$sub" in
  list) cmd_list; exit ;;
  create|start|stop|remove|status) ;;
  -h|--help|help) usage ;;
  *) die "unknown command '$sub'" 2 ;;
esac
NAME="${1:-}"; [ $# -eq 0 ] || shift
check_name
case "$sub" in
  create|remove|status) API=$(plane_api) ;;
esac
"cmd_$sub" "$@"
