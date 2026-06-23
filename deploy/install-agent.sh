#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/himydearfriends1934-cmyk/stream-control-hub.git"
BRANCH="main"
APP_DIR="/opt/stream-control-hub"
SERVICE_NAME="stream-control-node-agent"
SERVICE_USER="streamdash"
PORT="8787"
BIND_HOST="0.0.0.0"
HUB_URL=""
NODE_NAME="$(hostname)"
STREAM_DIR="/srv/stream-videos"
CONFIRM_DELETE_OLD=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) REPO_URL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --service-user) SERVICE_USER="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --bind) BIND_HOST="$2"; shift 2 ;;
    --hub-url) HUB_URL="$2"; shift 2 ;;
    --node-name) NODE_NAME="$2"; shift 2 ;;
    --stream-dir) STREAM_DIR="$2"; shift 2 ;;
    --confirm-delete-old) CONFIRM_DELETE_OLD=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$HUB_URL" ]; then
  echo "--hub-url is required for fixed Hub/Agent linkage." >&2
  exit 2
fi

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_DIR="/etc/stream-control-hub"
ENV_FILE="${ENV_DIR}/agent.env"
DATA_DIR="/var/lib/${SERVICE_NAME}"
LOG_DIR="/var/log/${SERVICE_NAME}"
RUN_DIR="/run/${SERVICE_NAME}"
RELEASE_ROOT="/opt/stream-control-hub-agent-releases"
BACKUP_DIR="/opt/stream-control-hub-agent-backups"
TS="$(date +%Y%m%d%H%M%S)"
NEW_RELEASE="${RELEASE_ROOT}/${TS}"
OLD_PATHS=()
OLD_SERVICES=()
OLD_PORTS=()

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root or with sudo." >&2
  exit 1
fi

has_item() {
  local needle="$1"
  local item
  shift || true
  for item in "$@"; do
    if [ "$item" = "$needle" ]; then
      return 0
    fi
  done
  return 1
}

record_path() {
  local path="$1"
  if [ -e "$path" ] || [ -L "$path" ]; then
    if ! has_item "$path" "${OLD_PATHS[@]}"; then
      OLD_PATHS+=("$path")
    fi
  fi
}

record_service() {
  local unit="$1"
  if systemctl cat "$unit" >/dev/null 2>&1 || [ -e "/etc/systemd/system/$unit" ] || [ -e "/lib/systemd/system/$unit" ]; then
    if ! has_item "$unit" "${OLD_SERVICES[@]}"; then
      OLD_SERVICES+=("$unit")
    fi
  fi
}

scan_old_installation() {
  local path unit port_line
  record_service "${SERVICE_NAME}.service"
  record_service "stream-control-node-agent.service"
  record_service "stream-control-hub.service"
  record_service "stream-dashboard.service"
  record_service "istanbul-stream-dashboard.service"

  shopt -s nullglob
  for unit in /etc/systemd/system/stream-control*.service /etc/systemd/system/*stream-dashboard*.service; do
    record_service "$(basename "$unit")"
  done
  for path in \
    "$SERVICE_FILE" "$ENV_DIR" "$APP_DIR" "$DATA_DIR" "$STREAM_DIR" "$LOG_DIR" "$RELEASE_ROOT" "$BACKUP_DIR" \
    /opt/stream-control-hub /opt/stream-control-hub-* /opt/stream-control-node-agent \
    /opt/stream-dashboard /opt/istanbul-stream-dashboard \
    /var/lib/stream-control-hub /var/lib/stream-control-node-agent \
    /var/lib/stream-dashboard /var/lib/istanbul-stream-dashboard \
    /var/log/stream-control-hub /var/log/stream-control-node-agent \
    /var/log/stream-dashboard /var/log/istanbul-stream-dashboard \
    /etc/stream-control-hub /etc/stream-dashboard /etc/istanbul-stream-dashboard; do
    record_path "$path"
  done
  shopt -u nullglob

  if command -v ss >/dev/null 2>&1; then
    while IFS= read -r port_line; do
      [ -n "$port_line" ] && OLD_PORTS+=("$port_line")
    done < <(ss -H -ltnp "sport = :${PORT}" 2>/dev/null || true)
  fi
}

print_items() {
  local title="$1"
  local count
  local item
  shift || true
  count="$#"
  [ "$count" -gt 0 ] || return 0
  echo "${title} (${count}):"
  for item in "$@"; do
    echo "  - ${item}"
  done
}

confirm_old_cleanup() {
  local total reply
  total=$(( ${#OLD_PATHS[@]} + ${#OLD_SERVICES[@]} + ${#OLD_PORTS[@]} ))
  [ "$total" -gt 0 ] || return 0

  echo "Existing or conflicting Stream Control installation content was found."
  echo "Summary: paths=${#OLD_PATHS[@]}, services=${#OLD_SERVICES[@]}, port_conflicts=${#OLD_PORTS[@]}"
  print_items "Services to stop, disable, and remove" "${OLD_SERVICES[@]}"
  print_items "Project paths to permanently delete" "${OLD_PATHS[@]}"
  print_items "Processes currently listening on port ${PORT}" "${OLD_PORTS[@]}"
  echo
  echo "This is a clean install guard. The installer will continue only after the old project is removed."

  if [ "$CONFIRM_DELETE_OLD" = "1" ]; then
    echo "--confirm-delete-old was provided; deleting old project content without prompting."
    return 0
  fi

  if [ ! -r /dev/tty ]; then
    echo "No interactive terminal is available for confirmation." >&2
    echo "Re-run from a terminal and type DELETE, or pass --confirm-delete-old for unattended cleanup." >&2
    exit 3
  fi

  read -r -p "Type DELETE to permanently remove the old project and continue: " reply < /dev/tty
  if [ "$reply" != "DELETE" ]; then
    echo "Install cancelled. No old project content was deleted."
    exit 4
  fi
}

delete_old_installation() {
  local unit path
  [ $(( ${#OLD_PATHS[@]} + ${#OLD_SERVICES[@]} )) -gt 0 ] || return 0

  for unit in "${OLD_SERVICES[@]}"; do
    systemctl stop "$unit" >/dev/null 2>&1 || true
    systemctl disable "$unit" >/dev/null 2>&1 || true
  done

  for unit in "${OLD_SERVICES[@]}"; do
    rm -f "/etc/systemd/system/$unit" "/lib/systemd/system/$unit"
  done
  systemctl daemon-reload || true

  for path in "${OLD_PATHS[@]}"; do
    rm -rf --one-file-system -- "$path"
  done

  echo "Old project content deleted. Starting fresh Headless Agent installation."
}

ensure_port_available() {
  local port_line
  if ! command -v ss >/dev/null 2>&1; then
    return 0
  fi
  port_line="$(ss -H -ltnp "sport = :${PORT}" 2>/dev/null | head -n 1 || true)"
  if [ -n "$port_line" ]; then
    echo "Port ${PORT} is still in use after cleanup:" >&2
    echo "  ${port_line}" >&2
    echo "Stop the conflicting service or choose another --port before installing." >&2
    exit 5
  fi
}

scan_old_installation
confirm_old_cleanup
delete_old_installation
ensure_port_available

apt-get update
apt-get install -y git python3 python3-venv python3-pip ffmpeg curl

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$ENV_DIR" "$DATA_DIR" "$STREAM_DIR" "$LOG_DIR" "$RUN_DIR" "$RELEASE_ROOT" "$BACKUP_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR" "$STREAM_DIR" "$LOG_DIR" "$RUN_DIR" "$RELEASE_ROOT" "$BACKUP_DIR"
chmod 750 "$ENV_DIR"

git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$NEW_RELEASE"
python3 -m venv "${NEW_RELEASE}/.venv"
"${NEW_RELEASE}/.venv/bin/pip" install --upgrade pip
"${NEW_RELEASE}/.venv/bin/pip" install -r "${NEW_RELEASE}/requirements.txt"
chown -R "$SERVICE_USER:$SERVICE_USER" "$NEW_RELEASE"

cat > "$ENV_FILE" <<EOF
PYTHONUNBUFFERED=1
PORT=${PORT}
STREAM_CONTROL_ROLE=agent
STREAM_NODE_AGENT_MODE=1
STREAM_NODE_AGENT_NAME=${NODE_NAME}
CONTROL_HUB_URL=${HUB_URL}
STREAM_UPLOAD_DIR=${STREAM_DIR}
STREAM_LOG_FILE=${LOG_DIR}/stream.log
STREAM_CONFIG_FILE=${DATA_DIR}/stream_config.json
STREAM_TUNING_FILE=${DATA_DIR}/stream_tuning.json
STREAM_RUNTIME_FILE=${DATA_DIR}/stream_runtime_state.json
STREAM_PID_FILE=${RUN_DIR}/ffmpeg.pid
CHAT_PLAN_FILE=${DATA_DIR}/chat_plan.json
YOUTUBE_CLIENT_FILE=${DATA_DIR}/youtube_client.json
YOUTUBE_TOKEN_FILE=${DATA_DIR}/youtube_token.json
DASHBOARD_SECRET_FILE=${DATA_DIR}/dashboard_secret.key
DASHBOARD_PASSWORD_FILE=${DATA_DIR}/dashboard_password.txt
PUBLIC_UPLOAD_FIREWALL=none
PUBLIC_UPLOAD_RESTRICT=1
PUBLIC_UPLOAD_CLOSE_ON_START=1
STREAM_AUTORESTART_ENABLED=1
STREAM_RELAY_ENABLED=0
EOF
chmod 640 "$ENV_FILE"
chown root:"$SERVICE_USER" "$ENV_FILE" || true

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Stream Control Hub Node Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
RuntimeDirectory=${SERVICE_NAME}
ExecStart=${APP_DIR}/.venv/bin/gunicorn --worker-class gthread --workers 1 --threads 4 --timeout 3600 --graceful-timeout 30 --bind ${BIND_HOST}:${PORT} stream_control_hub.node_agent.wsgi:APP
KillMode=process
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl stop "$SERVICE_NAME.service" >/dev/null 2>&1 || true
mkdir -p "${BACKUP_DIR}/${TS}"
if [ -L "$APP_DIR" ]; then
  rm -f "$APP_DIR"
elif [ -d "$APP_DIR" ]; then
  mv "$APP_DIR" "${BACKUP_DIR}/${TS}/previous-app-dir"
fi
ln -s "$NEW_RELEASE" "$APP_DIR"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME.service"
systemctl start "$SERVICE_NAME.service"
sleep 3
systemctl is-active --quiet "$SERVICE_NAME.service"
HEALTH_HOST="$BIND_HOST"
if [ "$HEALTH_HOST" = "0.0.0.0" ] || [ "$HEALTH_HOST" = "::" ]; then
  HEALTH_HOST="127.0.0.1"
fi
curl -fsS --max-time 8 "http://${HEALTH_HOST}:${PORT}/api/status" >/dev/null

echo "Headless Agent deployed as ${SERVICE_NAME}.service"
echo "URL: http://${BIND_HOST}:${PORT}/api/status"
