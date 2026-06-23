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

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root or with sudo." >&2
  exit 1
fi

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
