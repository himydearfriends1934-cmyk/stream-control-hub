#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/himydearfriends1934-cmyk/stream-control-hub.git"
BRANCH="main"
APP_DIR="/opt/stream-control-hub"
SERVICE_NAME="stream-control-hub"
SERVICE_USER="streamhub"
PORT="8788"
BIND_HOST="0.0.0.0"
CONFIRM_DELETE_OLD=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) REPO_URL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --service-user) SERVICE_USER="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --bind) BIND_HOST="$2"; shift 2 ;;
    --confirm-delete-old) CONFIRM_DELETE_OLD=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_DIR="/etc/stream-control-hub"
ENV_FILE="${ENV_DIR}/hub.env"
DATA_DIR="/var/lib/stream-control-hub"
CONFIG_DATA_DIR="${DATA_DIR}/config"
MEDIA_DIR="${DATA_DIR}/media"
WORK_DIR="${DATA_DIR}/work"
LOG_DIR="/var/log/stream-control-hub"
RELEASE_ROOT="/opt/stream-control-hub-releases"
BACKUP_DIR="/opt/stream-control-hub-backups"
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
  record_service "stream-control-hub.service"
  record_service "stream-control-node-agent.service"
  record_service "stream-dashboard.service"
  record_service "istanbul-stream-dashboard.service"

  shopt -s nullglob
  for unit in /etc/systemd/system/stream-control*.service /etc/systemd/system/*stream-dashboard*.service; do
    record_service "$(basename "$unit")"
  done
  for path in \
    "$SERVICE_FILE" "$ENV_DIR" "$APP_DIR" "$DATA_DIR" "$LOG_DIR" "$RELEASE_ROOT" "$BACKUP_DIR" \
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

  echo "Old project content deleted. Starting fresh Hub installation."
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

mkdir -p "$ENV_DIR" "$DATA_DIR" "$CONFIG_DATA_DIR" "$MEDIA_DIR" "$WORK_DIR" "$LOG_DIR" "$RELEASE_ROOT" "$BACKUP_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR" "$LOG_DIR" "$RELEASE_ROOT" "$BACKUP_DIR"
chmod 750 "$ENV_DIR"

git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$NEW_RELEASE"

python3 -m venv "${NEW_RELEASE}/.venv"
"${NEW_RELEASE}/.venv/bin/pip" install --upgrade pip
"${NEW_RELEASE}/.venv/bin/pip" install -r "${NEW_RELEASE}/requirements.txt"
chown -R "$SERVICE_USER:$SERVICE_USER" "$NEW_RELEASE"

cat > "$ENV_FILE" <<EOF
STREAM_HUB_HOST=${BIND_HOST}
STREAM_HUB_PORT=${PORT}
STREAM_HUB_DATA_DIR=${DATA_DIR}
STREAM_HUB_NODES_FILE=${CONFIG_DATA_DIR}/nodes.json
PYTHONUNBUFFERED=1
EOF
chmod 640 "$ENV_FILE"
chown root:"$SERVICE_USER" "$ENV_FILE" || true
chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DATA_DIR"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Stream Control Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/gunicorn --worker-class gthread --workers 1 --threads 4 --timeout 3600 --graceful-timeout 30 --bind \${STREAM_HUB_HOST}:\${STREAM_HUB_PORT} stream_control_hub.app:APP
Restart=always
RestartSec=3
KillMode=process

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
curl -fsS --max-time 8 "http://${HEALTH_HOST}:${PORT}/" >/dev/null

echo "Hub deployed as ${SERVICE_NAME}.service"
echo "URL: http://${BIND_HOST}:${PORT}/"
