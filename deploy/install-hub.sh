#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/himydearfriends1934-cmyk/stream-control-hub.git"
BRANCH="main"
APP_DIR="/opt/stream-control-hub"
SERVICE_NAME="stream-control-hub"
SERVICE_USER="streamhub"
PORT="8788"
BIND_HOST="0.0.0.0"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) REPO_URL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --service-user) SERVICE_USER="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --bind) BIND_HOST="$2"; shift 2 ;;
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

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root or with sudo." >&2
  exit 1
fi

apt-get update
apt-get install -y git python3 python3-venv python3-pip ffmpeg curl

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$ENV_DIR" "$DATA_DIR" "$CONFIG_DATA_DIR" "$MEDIA_DIR" "$WORK_DIR" "$LOG_DIR" "$RELEASE_ROOT" "$BACKUP_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR" "$LOG_DIR" "$RELEASE_ROOT" "$BACKUP_DIR"
chmod 750 "$ENV_DIR"

if [ -f "${APP_DIR}/config/nodes.json" ]; then
  mkdir -p "${BACKUP_DIR}/${TS}"
  cp "${APP_DIR}/config/nodes.json" "${BACKUP_DIR}/${TS}/nodes.json"
  cp "${APP_DIR}/config/nodes.json" "${CONFIG_DATA_DIR}/nodes.json"
  chmod 600 "${BACKUP_DIR}/${TS}/nodes.json" || true
fi

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
