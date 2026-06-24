#!/usr/bin/env sh
set -eu

INSTALL_DIR="${INSTALL_DIR:-/opt/stream-control-hub-agent}"
REPO_URL="${REPO_URL:-https://github.com/himydearfriends1934-cmyk/stream-control-hub.git}"
BRANCH="${BRANCH:-main}"
STREAM_AGENT_HOST="${STREAM_AGENT_HOST:-0.0.0.0}"
STREAM_AGENT_PORT="${STREAM_AGENT_PORT:-8787}"
STREAM_AGENT_NAME="${STREAM_AGENT_NAME:-$(hostname)}"
STREAM_AGENT_CONTROL_HUB="${STREAM_AGENT_CONTROL_HUB:-}"
TAILSCALE_AUTH_KEY="${TAILSCALE_AUTH_KEY:-}"
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-$STREAM_AGENT_NAME}"
ACTION="${ACTION:-${STREAM_AGENT_ACTION:-install}}"
UNINSTALL="${UNINSTALL:-0}"
REMOVE_DATA="${REMOVE_DATA:-${STREAM_AGENT_REMOVE_DATA:-0}}"
CHOICE="${CHOICE:-${STREAM_AGENT_CHOICE:-}}"

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: curl ... | sudo sh" >&2
    exit 1
  fi
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "$1 is required but missing." >&2
    exit 1
  }
}

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y git python3 python3-venv python3-pip curl ffmpeg
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y git python3 python3-pip curl ffmpeg
  elif command -v yum >/dev/null 2>&1; then
    yum install -y git python3 python3-pip curl ffmpeg
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache git python3 py3-pip curl ffmpeg
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

tailscale_ip() {
  if command -v tailscale >/dev/null 2>&1; then
    tailscale ip -4 2>/dev/null | head -n 1 || true
  fi
}

public_ip() {
  curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || \
    curl -fsS --max-time 8 https://ifconfig.me 2>/dev/null || true
}

show_menu() {
  echo "Stream Control Headless Agent"
  echo "1) Install or update"
  echo "2) Uninstall, keep media and local env"
  echo "3) Uninstall and remove media/local env"
  printf "Choose [1-3] (default 1): "
}

resolve_action() {
  if [ "$UNINSTALL" = "1" ]; then
    ACTION="uninstall"
    return
  fi
  if [ "$ACTION" != "install" ] || [ -n "$CHOICE" ]; then
    :
  elif [ -r /dev/tty ] && [ -w /dev/tty ]; then
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

uninstall_agent() {
  need_root
  systemctl stop stream-control-headless-agent.service >/dev/null 2>&1 || true
  systemctl disable stream-control-headless-agent.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/stream-control-headless-agent.service
  systemctl daemon-reload >/dev/null 2>&1 || true
  pkill -f "$INSTALL_DIR/.venv/bin/python -m stream_control_hub.headless_agent" >/dev/null 2>&1 || true
  if [ ! -e "$INSTALL_DIR" ]; then
    echo "Stream Control Headless Agent is not installed at: $INSTALL_DIR"
    return 0
  fi
  if [ "$REMOVE_DATA" = "1" ] || [ "$REMOVE_DATA" = "true" ] || [ "$REMOVE_DATA" = "yes" ]; then
    rm -rf "$INSTALL_DIR"
    echo "Stream Control Headless Agent uninstalled. Data removed: $INSTALL_DIR"
    return 0
  fi
  rm -rf \
    "$INSTALL_DIR/.venv" \
    "$INSTALL_DIR/.git" \
    "$INSTALL_DIR/stream_control_hub" \
    "$INSTALL_DIR/scripts" \
    "$INSTALL_DIR/config" \
    "$INSTALL_DIR/requirements.txt" \
    "$INSTALL_DIR/README.md"
  echo "Stream Control Headless Agent uninstalled. Media and local env preserved in: $INSTALL_DIR"
  echo "Use REMOVE_DATA=1 to remove agent_data and .agent.env too."
}

resolve_action

if [ "$ACTION" = "uninstall" ]; then
  uninstall_agent
  exit 0
fi

need_root
install_packages
need_cmd git
need_cmd python3

if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
elif [ -e "$INSTALL_DIR" ]; then
  echo "INSTALL_DIR exists but is not a git checkout: $INSTALL_DIR" >&2
  exit 1
else
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
"$INSTALL_DIR/.venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"

ENV_FILE="$INSTALL_DIR/.agent.env"
TOKEN=""
if [ -f "$ENV_FILE" ]; then
  TOKEN="$(sed -n 's/^STREAM_AGENT_CONTROL_TOKEN=//p' "$ENV_FILE" | head -n 1)"
fi
[ -n "$TOKEN" ] || TOKEN="$(new_token)"

cat > "$ENV_FILE" <<EOF
STREAM_AGENT_CONTROL_TOKEN=$TOKEN
STREAM_AGENT_HOST=$STREAM_AGENT_HOST
STREAM_AGENT_PORT=$STREAM_AGENT_PORT
STREAM_AGENT_NAME=$STREAM_AGENT_NAME
STREAM_AGENT_CONTROL_HUB=$STREAM_AGENT_CONTROL_HUB
STREAM_AGENT_DATA_DIR=$INSTALL_DIR/agent_data
EOF
chmod 600 "$ENV_FILE"

if [ -n "$TAILSCALE_AUTH_KEY" ]; then
  if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
  tailscale up --auth-key "$TAILSCALE_AUTH_KEY" --hostname "$TAILSCALE_HOSTNAME" --accept-dns=false
fi

cat > /etc/systemd/system/stream-control-headless-agent.service <<EOF
[Unit]
Description=Stream Control Hub Headless Agent
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.agent.env
ExecStart=$INSTALL_DIR/.venv/bin/python -m stream_control_hub.headless_agent
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now stream-control-headless-agent.service

NODE_IP="$(tailscale_ip)"
[ -n "$NODE_IP" ] || NODE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PUBLIC_IP="$(public_ip)"

echo "Stream Control Headless Agent installed."
echo "Add this node to the Hub nodes file:"
if [ -n "$PUBLIC_IP" ] && [ "$PUBLIC_IP" != "$NODE_IP" ]; then
cat <<EOF
{
  "id": "$STREAM_AGENT_NAME",
  "name": "$STREAM_AGENT_NAME",
  "base_url": "http://$NODE_IP:$STREAM_AGENT_PORT",
  "upload_base_url": "http://$PUBLIC_IP:$STREAM_AGENT_PORT",
  "role": "stream-node",
  "enabled": true,
  "token": "$TOKEN"
}
EOF
else
cat <<EOF
{
  "id": "$STREAM_AGENT_NAME",
  "name": "$STREAM_AGENT_NAME",
  "base_url": "http://$NODE_IP:$STREAM_AGENT_PORT",
  "role": "stream-node",
  "enabled": true,
  "token": "$TOKEN"
}
EOF
fi
