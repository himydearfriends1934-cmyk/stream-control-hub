#!/usr/bin/env sh
set -eu

if [ -z "${INSTALL_DIR:-}" ]; then
  EXISTING_INSTALL_DIR=""
  if command -v systemctl >/dev/null 2>&1; then
    EXISTING_INSTALL_DIR="$(systemctl show -p WorkingDirectory --value stream-control-hub.service 2>/dev/null || true)"
    if [ -z "$EXISTING_INSTALL_DIR" ] && [ "$(id -u)" -ne 0 ]; then
      EXISTING_INSTALL_DIR="$(systemctl --user show -p WorkingDirectory --value stream-control-hub.service 2>/dev/null || true)"
    fi
  fi
  if [ -z "$EXISTING_INSTALL_DIR" ] && [ -f /etc/systemd/system/stream-control-hub.service ]; then
    EXISTING_INSTALL_DIR="$(sed -n 's/^WorkingDirectory=//p' /etc/systemd/system/stream-control-hub.service | head -n 1)"
  fi
  if [ -z "$EXISTING_INSTALL_DIR" ] && [ -f "$HOME/.config/systemd/user/stream-control-hub.service" ]; then
    EXISTING_INSTALL_DIR="$(sed -n 's/^WorkingDirectory=//p' "$HOME/.config/systemd/user/stream-control-hub.service" | head -n 1)"
  fi
  if [ -n "$EXISTING_INSTALL_DIR" ] && [ -d "$EXISTING_INSTALL_DIR" ]; then
    INSTALL_DIR="$EXISTING_INSTALL_DIR"
  elif [ -d "/opt/stream-control-hub/.git" ]; then
    INSTALL_DIR="/opt/stream-control-hub"
  elif [ -d "$HOME/stream-control-hub/.git" ]; then
    INSTALL_DIR="$HOME/stream-control-hub"
  elif [ "$(id -u)" -eq 0 ]; then
    INSTALL_DIR="/opt/stream-control-hub"
  else
    INSTALL_DIR="$HOME/stream-control-hub"
  fi
fi
REPO_URL="${REPO_URL:-https://github.com/himydearfriends1934-cmyk/stream-control-hub.git}"
BRANCH="${BRANCH:-main}"
STREAM_HUB_HOST="${STREAM_HUB_HOST:-}"
STREAM_HUB_PORT="${STREAM_HUB_PORT:-}"
STREAM_HUB_NODES_FILE="${STREAM_HUB_NODES_FILE:-}"
STREAM_HUB_TRUSTED_REMOTE_WRITES="${STREAM_HUB_TRUSTED_REMOTE_WRITES:-}"
STREAM_HUB_SERVICE_MODE="${STREAM_HUB_SERVICE_MODE:-}"
YOUTUBE_CLIENT_ID="${YOUTUBE_CLIENT_ID:-}"
YOUTUBE_CLIENT_SECRET="${YOUTUBE_CLIENT_SECRET:-}"
YOUTUBE_CREDENTIAL_FILE="${YOUTUBE_CREDENTIAL_FILE:-}"
TAILSCALE_AUTH_KEY="${TAILSCALE_AUTH_KEY:-}"
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-stream-control-hub}"
ACTION="${ACTION:-${STREAM_HUB_ACTION:-install}}"
UNINSTALL="${UNINSTALL:-0}"
REMOVE_DATA="${REMOVE_DATA:-${STREAM_HUB_REMOVE_DATA:-0}}"
CHOICE="${CHOICE:-${STREAM_HUB_CHOICE:-}}"
SUPPRESS_TOKEN_OUTPUT="${STREAM_HUB_SUPPRESS_TOKEN_OUTPUT:-0}"
ROLE_SWITCH_CONFIRMED="${ROLE_SWITCH_CONFIRMED:-${STREAM_ROLE_SWITCH_CONFIRMED:-0}}"

resolve_service_mode() {
  if [ -n "$STREAM_HUB_SERVICE_MODE" ]; then
    case "$STREAM_HUB_SERVICE_MODE" in
      system|user) return 0 ;;
      *) echo "STREAM_HUB_SERVICE_MODE must be system or user." >&2; exit 1 ;;
    esac
  fi
  if [ -f /etc/systemd/system/stream-control-hub.service ] \
    && grep -Fq "WorkingDirectory=$INSTALL_DIR" /etc/systemd/system/stream-control-hub.service; then
    STREAM_HUB_SERVICE_MODE="system"
  elif [ -f "$HOME/.config/systemd/user/stream-control-hub.service" ] \
    && grep -Fq "WorkingDirectory=$INSTALL_DIR" "$HOME/.config/systemd/user/stream-control-hub.service"; then
    STREAM_HUB_SERVICE_MODE="user"
  elif [ "$(id -u)" -eq 0 ] && [ "$INSTALL_DIR" = "/opt/stream-control-hub" ]; then
    STREAM_HUB_SERVICE_MODE="system"
  else
    STREAM_HUB_SERVICE_MODE="user"
  fi
}

resolve_service_mode

if [ -z "${STREAM_NODE_ROLE_FILE:-}" ]; then
  if [ "$STREAM_HUB_SERVICE_MODE" = "system" ]; then
    STREAM_NODE_ROLE_FILE="/etc/stream-control/role"
  else
    STREAM_NODE_ROLE_FILE="$HOME/.config/stream-control/role"
  fi
fi
UPGRADE_LOCK_FILE="${STREAM_NODE_UPGRADE_LOCK_FILE:-$INSTALL_DIR/data/.upgrade.lock}"

acquire_upgrade_lock() {
  mkdir -p "$(dirname "$UPGRADE_LOCK_FILE")"
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$UPGRADE_LOCK_FILE"
    flock -n 9 || {
      echo "Another Stream Control upgrade or install is already running." >&2
      exit 75
    }
  else
    lock_dir="${UPGRADE_LOCK_FILE}.d"
    if ! mkdir "$lock_dir" 2>/dev/null; then
      echo "Another Stream Control upgrade or install is already running." >&2
      exit 75
    fi
    trap 'rmdir "$lock_dir" >/dev/null 2>&1 || true' EXIT
  fi
}

assert_single_role() {
  if [ -f "$STREAM_NODE_ROLE_FILE" ]; then
    current_role="$(head -n 1 "$STREAM_NODE_ROLE_FILE" | tr -d '\r' | tr '[:upper:]' '[:lower:]')"
    if [ -n "$current_role" ] && [ "$current_role" != "hub" ] && [ "$ROLE_SWITCH_CONFIRMED" != "1" ]; then
      echo "This machine is declared as $current_role. Deactivate that role before installing HUB." >&2
      exit 6
    fi
  fi
  if command -v systemctl >/dev/null 2>&1 \
    && systemctl is-active --quiet stream-control-headless-agent.service \
    && [ "$ROLE_SWITCH_CONFIRMED" != "1" ]; then
    echo "Agent service is active. Deactivate Agent before installing HUB." >&2
    exit 6
  fi
}

write_role_marker() {
  mkdir -p "$(dirname "$STREAM_NODE_ROLE_FILE")"
  temporary="${STREAM_NODE_ROLE_FILE}.tmp.$$"
  printf 'hub\n' > "$temporary"
  chmod 600 "$temporary" 2>/dev/null || true
  mv -f "$temporary" "$STREAM_NODE_ROLE_FILE"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "$1 is required. Install it and run this installer again." >&2
    exit 1
  }
}

hub_systemctl() {
  if [ "$STREAM_HUB_SERVICE_MODE" = "system" ]; then
    systemctl "$@"
  else
    systemctl --user "$@"
  fi
}

health_check_hub() {
  probe_host="$(sed -n 's/^STREAM_HUB_HOST=//p' "$INSTALL_DIR/.env" | head -n 1)"
  probe_port="$(sed -n 's/^STREAM_HUB_PORT=//p' "$INSTALL_DIR/.env" | head -n 1)"
  [ -n "$probe_host" ] || probe_host="127.0.0.1"
  [ -n "$probe_port" ] || probe_port="8788"
  case "$probe_host" in
    0.0.0.0) probe_host="127.0.0.1" ;;
    ::) probe_host="[::1]" ;;
  esac
  expected_version="$(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
  for _ in $(seq 1 20); do
    status="$(curl -fsS --max-time 3 "http://$probe_host:$probe_port/api/role-status" 2>/dev/null || true)"
    version="$(curl -fsS --max-time 3 "http://$probe_host:$probe_port/api/app-version" 2>/dev/null || true)"
    if hub_systemctl is-active --quiet stream-control-hub.service \
      && ! systemctl is-active --quiet stream-control-headless-agent.service \
      && printf '%s' "$status" | grep -q '"hub"' \
      && printf '%s' "$status" | grep -q '"enabled":true' \
      && printf '%s' "$version" | grep -q "\"version\":\"$expected_version\""; then
      return 0
    fi
    sleep 1
  done
  return 1
}

transactional_refresh_hub() {
  [ -d "$INSTALL_DIR/.git" ] || return 0
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  if [ -n "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=all)" ]; then
    echo "Refusing upgrade: the HUB checkout has local changes." >&2
    return 6
  fi
  old_commit="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
  old_branch="$(git -C "$INSTALL_DIR" branch --show-current)"
  candidate="$INSTALL_DIR/.upgrade-candidate.$$"
  backup="$INSTALL_DIR/data/upgrade-backups/$(date +%Y%m%d%H%M%S)-$old_commit"
  mkdir -p "$INSTALL_DIR/data/upgrade-backups" "$backup"
  git -C "$INSTALL_DIR" worktree add --detach "$candidate" "origin/$BRANCH"
  cleanup_candidate() {
    git -C "$INSTALL_DIR" worktree remove --force "$candidate" >/dev/null 2>&1 || rm -rf "$candidate"
  }
  if ! python3 -m venv "$candidate/.venv" \
    || ! "$candidate/.venv/bin/python" -m pip install -q --upgrade pip \
    || ! "$candidate/.venv/bin/python" -m pip install -q -r "$candidate/requirements.txt" \
    || ! "$candidate/.venv/bin/python" -m compileall -q "$candidate/stream_control_hub"; then
    cleanup_candidate
    rm -rf "$backup"
    echo "Candidate HUB failed pre-activation validation; current version was kept." >&2
    return 6
  fi

  code_items="stream_control_hub scripts config requirements.txt README.md run-hub.sh"
  for item in $code_items; do
    [ -e "$INSTALL_DIR/$item" ] && mv "$INSTALL_DIR/$item" "$backup/$item"
  done
  [ -e "$INSTALL_DIR/.venv" ] && mv "$INSTALL_DIR/.venv" "$backup/.venv"
  for item in $code_items; do
    [ -e "$candidate/$item" ] && cp -a "$candidate/$item" "$INSTALL_DIR/$item"
  done
  mv "$candidate/.venv" "$INSTALL_DIR/.venv"
  git -C "$INSTALL_DIR" checkout -B "$BRANCH" "origin/$BRANCH"
  hub_systemctl restart stream-control-hub.service
  if health_check_hub; then
    cleanup_candidate
    echo "HUB upgrade activated; rollback snapshot retained at $backup"
    return 0
  fi

  echo "New HUB failed health checks; rolling back." >&2
  hub_systemctl stop stream-control-hub.service >/dev/null 2>&1 || true
  for item in $code_items; do
    rm -rf "$INSTALL_DIR/$item"
    [ -e "$backup/$item" ] && mv "$backup/$item" "$INSTALL_DIR/$item"
  done
  rm -rf "$INSTALL_DIR/.venv"
  [ -e "$backup/.venv" ] && mv "$backup/.venv" "$INSTALL_DIR/.venv"
  if [ -n "$old_branch" ]; then
    git -C "$INSTALL_DIR" checkout -B "$old_branch" "$old_commit"
  else
    git -C "$INSTALL_DIR" checkout --detach "$old_commit"
  fi
  cleanup_candidate
  hub_systemctl restart stream-control-hub.service >/dev/null 2>&1 || true
  rm -rf "$backup"
  return 6
}

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y git python3 python3-venv python3-pip curl
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y git python3 python3-pip curl
  elif command -v yum >/dev/null 2>&1; then
    yum install -y git python3 python3-pip curl
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache git python3 py3-pip curl
  fi
}

new_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 32 | tr '+/' '-_' | tr -d '='
  else
    python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
  fi
}

show_menu() {
  echo "Stream Control Hub"
  echo "1) Install or update"
  echo "2) Uninstall, keep saved data"
  echo "3) Uninstall and remove saved data"
  printf "Choose [1-3] (default 1): "
}

resolve_action() {
  if [ "$UNINSTALL" = "1" ]; then
    ACTION="uninstall"
    return
  fi
  if [ "$ACTION" != "install" ] || [ -n "$CHOICE" ]; then
    :
  elif [ -t 0 ] && [ -r /dev/tty ] && [ -w /dev/tty ]; then
    show_menu > /dev/tty
    read -r CHOICE < /dev/tty || CHOICE="1"
  else
    CHOICE="1"
  fi

  case "${CHOICE:-1}" in
    1|"") ACTION="install" ;;
    2) ACTION="uninstall"; REMOVE_DATA="0" ;;
    3) ACTION="uninstall"; REMOVE_DATA="1" ;;
    *) echo "Invalid choice: $CHOICE" >&2; exit 1 ;;
  esac
}

uninstall_hub() {
  if command -v systemctl >/dev/null 2>&1; then
    if [ "$STREAM_HUB_SERVICE_MODE" = "system" ]; then
      systemctl disable --now stream-control-hub.service >/dev/null 2>&1 || true
      rm -f /etc/systemd/system/stream-control-hub.service
      systemctl daemon-reload >/dev/null 2>&1 || true
    else
      systemctl --user disable --now stream-control-hub.service >/dev/null 2>&1 || true
      rm -f "$HOME/.config/systemd/user/stream-control-hub.service"
      systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
  fi
  rm -f "$STREAM_NODE_ROLE_FILE"
  pkill -f "$INSTALL_DIR/.venv/bin/python -m stream_control_hub" >/dev/null 2>&1 || true
  pkill -f "$INSTALL_DIR/run-hub.sh" >/dev/null 2>&1 || true
  if [ ! -e "$INSTALL_DIR" ]; then
    echo "Stream Control Hub is not installed at: $INSTALL_DIR"
    return 0
  fi
  if [ "$REMOVE_DATA" = "1" ] || [ "$REMOVE_DATA" = "true" ] || [ "$REMOVE_DATA" = "yes" ]; then
    rm -rf "$INSTALL_DIR"
    echo "Stream Control Hub uninstalled. Data removed: $INSTALL_DIR"
    return 0
  fi
  rm -rf \
    "$INSTALL_DIR/.venv" \
    "$INSTALL_DIR/.git" \
    "$INSTALL_DIR/stream_control_hub" \
    "$INSTALL_DIR/scripts" \
    "$INSTALL_DIR/config" \
    "$INSTALL_DIR/requirements.txt" \
    "$INSTALL_DIR/README.md" \
    "$INSTALL_DIR/run-hub.sh"
  echo "Stream Control Hub uninstalled. Data preserved in: $INSTALL_DIR"
  echo "Use REMOVE_DATA=1 to remove saved data and local config too."
}

resolve_action

if [ "$ACTION" = "uninstall" ]; then
  acquire_upgrade_lock
  uninstall_hub
  exit 0
fi

acquire_upgrade_lock
assert_single_role
install_packages
need_cmd git
need_cmd python3
need_cmd curl

if [ -d "$INSTALL_DIR/.git" ]; then
  transactional_refresh_hub
elif [ -e "$INSTALL_DIR" ]; then
  echo "Adopting existing Hub data directory without deleting local data or env: $INSTALL_DIR"
  git -C "$INSTALL_DIR" init
  if git -C "$INSTALL_DIR" remote get-url origin >/dev/null 2>&1; then
    git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
  else
    git -C "$INSTALL_DIR" remote add origin "$REPO_URL"
  fi
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout -B "$BRANCH" FETCH_HEAD
else
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
"$INSTALL_DIR/.venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"

ENV_FILE="$INSTALL_DIR/.env"
TOKEN=""
EXISTING_HOST=""
EXISTING_PORT=""
EXISTING_NODES_FILE=""
EXISTING_TRUSTED_REMOTE_WRITES=""
EXISTING_YOUTUBE_CLIENT_ID=""
EXISTING_YOUTUBE_CLIENT_SECRET=""
EXISTING_YOUTUBE_CREDENTIAL_FILE=""
existing_env_value() {
  key="$1"
  [ -f "$ENV_FILE" ] || return 0
  ENV_FILE="$ENV_FILE" ENV_KEY="$key" python3 - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["ENV_FILE"])
target = os.environ["ENV_KEY"]
text = path.read_text(errors="replace")
text = (
    text.replace("\\r\\n", "\n")
    .replace("\\n", "\n")
    .replace("\\r", "\n")
    .replace("\r\n", "\n")
    .replace("\r", "\n")
)
pattern = re.compile(r"(?:STREAM_[A-Z0-9_]+|YOUTUBE_[A-Z0-9_]+)=")
for raw_line in text.split("\n"):
    if not raw_line.strip() or raw_line.lstrip().startswith("#") or "=" not in raw_line:
        continue
    matches = list(pattern.finditer(raw_line))
    if len(matches) > 1:
        segments = [
            raw_line[match.start(): matches[index + 1].start() if index + 1 < len(matches) else len(raw_line)]
            for index, match in enumerate(matches)
        ]
    else:
        segments = [raw_line]
    for segment in segments:
        key, sep, value = segment.partition("=")
        if sep and key.strip() == target:
            print(value.strip().strip("\"'"), end="")
            raise SystemExit(0)
PY
}
if [ -f "$ENV_FILE" ]; then
  TOKEN="$(existing_env_value STREAM_HUB_CONTROL_TOKEN)"
  EXISTING_HOST="$(existing_env_value STREAM_HUB_HOST)"
  EXISTING_PORT="$(existing_env_value STREAM_HUB_PORT)"
  EXISTING_NODES_FILE="$(existing_env_value STREAM_HUB_NODES_FILE)"
  EXISTING_TRUSTED_REMOTE_WRITES="$(existing_env_value STREAM_HUB_TRUSTED_REMOTE_WRITES)"
  EXISTING_YOUTUBE_CLIENT_ID="$(existing_env_value YOUTUBE_CLIENT_ID)"
  EXISTING_YOUTUBE_CLIENT_SECRET="$(existing_env_value YOUTUBE_CLIENT_SECRET)"
  EXISTING_YOUTUBE_CREDENTIAL_FILE="$(existing_env_value YOUTUBE_CREDENTIAL_FILE)"
fi
[ -n "$STREAM_HUB_HOST" ] || STREAM_HUB_HOST="${EXISTING_HOST:-127.0.0.1}"
[ -n "$STREAM_HUB_PORT" ] || STREAM_HUB_PORT="${EXISTING_PORT:-8788}"
[ -n "$STREAM_HUB_NODES_FILE" ] || STREAM_HUB_NODES_FILE="${EXISTING_NODES_FILE:-$INSTALL_DIR/data/nodes.local.json}"
[ -n "$STREAM_HUB_TRUSTED_REMOTE_WRITES" ] || STREAM_HUB_TRUSTED_REMOTE_WRITES="${EXISTING_TRUSTED_REMOTE_WRITES:-0}"
[ -n "$YOUTUBE_CLIENT_ID" ] || YOUTUBE_CLIENT_ID="$EXISTING_YOUTUBE_CLIENT_ID"
[ -n "$YOUTUBE_CLIENT_SECRET" ] || YOUTUBE_CLIENT_SECRET="$EXISTING_YOUTUBE_CLIENT_SECRET"
[ -n "$YOUTUBE_CREDENTIAL_FILE" ] || YOUTUBE_CREDENTIAL_FILE="${EXISTING_YOUTUBE_CREDENTIAL_FILE:-$INSTALL_DIR/data/youtube_credentials.json}"
case "$(printf '%s' "$STREAM_HUB_TRUSTED_REMOTE_WRITES" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) STREAM_HUB_TRUSTED_REMOTE_WRITES="1" ;;
  0|false|no) STREAM_HUB_TRUSTED_REMOTE_WRITES="0" ;;
  *) echo "STREAM_HUB_TRUSTED_REMOTE_WRITES must be 0 or 1." >&2; exit 1 ;;
esac
NODES_FILE="$STREAM_HUB_NODES_FILE"
mkdir -p "$(dirname "$NODES_FILE")"
[ -f "$NODES_FILE" ] || printf '[]\n' > "$NODES_FILE"

[ -n "$TOKEN" ] || TOKEN="$(new_token)"

cat > "$ENV_FILE" <<EOF
STREAM_HUB_CONTROL_TOKEN=$TOKEN
STREAM_NODE_ROLE=hub
STREAM_NODE_ROLE_FILE=$STREAM_NODE_ROLE_FILE
STREAM_HUB_NODES_FILE=$NODES_FILE
STREAM_HUB_HOST=$STREAM_HUB_HOST
STREAM_HUB_PORT=$STREAM_HUB_PORT
STREAM_HUB_TRUSTED_REMOTE_WRITES=$STREAM_HUB_TRUSTED_REMOTE_WRITES
YOUTUBE_CLIENT_ID=$YOUTUBE_CLIENT_ID
YOUTUBE_CLIENT_SECRET=$YOUTUBE_CLIENT_SECRET
YOUTUBE_CREDENTIAL_FILE=$YOUTUBE_CREDENTIAL_FILE
EOF
chmod 600 "$ENV_FILE"
write_role_marker

cat > "$INSTALL_DIR/run-hub.sh" <<EOF
#!/usr/bin/env sh
set -eu
cd "$INSTALL_DIR"
exec "$INSTALL_DIR/.venv/bin/python" -m stream_control_hub
EOF
chmod +x "$INSTALL_DIR/run-hub.sh"

if [ -n "$TAILSCALE_AUTH_KEY" ]; then
  TAILSCALE_HOSTNAME="$TAILSCALE_HOSTNAME" \
  TAILSCALE_AUTH_KEY="$TAILSCALE_AUTH_KEY" \
  TAILSCALE_ACCEPT_ROUTES="${TAILSCALE_ACCEPT_ROUTES:-1}" \
  sh "$INSTALL_DIR/scripts/tailscale-install.sh" connect
fi

if command -v systemctl >/dev/null 2>&1; then
  if [ "$STREAM_HUB_SERVICE_MODE" = "system" ]; then
    SERVICE_FILE="/etc/systemd/system/stream-control-hub.service"
    SERVICE_TARGET="multi-user.target"
    SYSTEMCTL="systemctl"
  else
    SERVICE_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SERVICE_DIR"
    SERVICE_FILE="$SERVICE_DIR/stream-control-hub.service"
    SERVICE_TARGET="default.target"
    SYSTEMCTL="systemctl --user"
  fi
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Stream Control Hub
After=network-online.target
Wants=network-online.target
Conflicts=stream-control-headless-agent.service
Before=stream-control-headless-agent.service
StartLimitIntervalSec=60
StartLimitBurst=10

[Service]
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
Environment=STREAM_NODE_ROLE=hub
Environment=STREAM_NODE_ROLE_FILE=$STREAM_NODE_ROLE_FILE
ExecStart=$INSTALL_DIR/.venv/bin/python -m stream_control_hub
Restart=always
RestartSec=3
TimeoutStopSec=20
KillMode=control-group
UMask=0077

[Install]
WantedBy=$SERVICE_TARGET
EOF
  $SYSTEMCTL daemon-reload
  $SYSTEMCTL enable stream-control-hub.service
  $SYSTEMCTL reset-failed stream-control-hub.service || true
  $SYSTEMCTL restart stream-control-hub.service
else
  "$INSTALL_DIR/run-hub.sh" &
fi

PROBE_HOST="$STREAM_HUB_HOST"
case "$PROBE_HOST" in
  0.0.0.0) PROBE_HOST="127.0.0.1" ;;
  ::) PROBE_HOST="[::1]" ;;
esac
HEALTHY="0"
ATTEMPT="0"
while [ "$ATTEMPT" -lt 10 ]; do
  if curl -fsS --max-time 5 "http://$PROBE_HOST:$STREAM_HUB_PORT/" >/dev/null 2>&1; then
    HEALTHY="1"
    break
  fi
  ATTEMPT=$((ATTEMPT + 1))
  sleep 1
done
if [ "$HEALTHY" != "1" ]; then
  echo "Hub health check failed at $PROBE_HOST:$STREAM_HUB_PORT." >&2
  exit 1
fi

echo "Stream Control Hub installed."
if [ "$SUPPRESS_TOKEN_OUTPUT" = "1" ]; then
  echo "Open the Hub at the configured host and port. Control token output suppressed for background installation."
else
  echo "Open: http://127.0.0.1:$STREAM_HUB_PORT/?token=$TOKEN"
fi
echo "Nodes file: $NODES_FILE"
echo "Install path: $INSTALL_DIR ($STREAM_HUB_SERVICE_MODE service)"
echo "Trusted remote writes: $STREAM_HUB_TRUSTED_REMOTE_WRITES"
