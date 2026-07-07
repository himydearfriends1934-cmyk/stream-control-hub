#!/usr/bin/env sh
set -eu

if [ -z "${INSTALL_DIR:-}" ]; then
  if [ -d "/opt/stream-control-hub/.git" ]; then
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
TAILSCALE_AUTH_KEY="${TAILSCALE_AUTH_KEY:-}"
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-stream-control-hub}"
ACTION="${ACTION:-${STREAM_HUB_ACTION:-install}}"
UNINSTALL="${UNINSTALL:-0}"
REMOVE_DATA="${REMOVE_DATA:-${STREAM_HUB_REMOVE_DATA:-0}}"
CHOICE="${CHOICE:-${STREAM_HUB_CHOICE:-}}"
SUPPRESS_TOKEN_OUTPUT="${STREAM_HUB_SUPPRESS_TOKEN_OUTPUT:-0}"

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

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "$1 is required. Install it and run this installer again." >&2
    exit 1
  }
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
  uninstall_hub
  exit 0
fi

install_packages
need_cmd git
need_cmd python3
need_cmd curl

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

ENV_FILE="$INSTALL_DIR/.env"
TOKEN=""
EXISTING_HOST=""
EXISTING_PORT=""
EXISTING_NODES_FILE=""
EXISTING_TRUSTED_REMOTE_WRITES=""
if [ -f "$ENV_FILE" ]; then
  TOKEN="$(sed -n 's/^STREAM_HUB_CONTROL_TOKEN=//p' "$ENV_FILE" | head -n 1)"
  EXISTING_HOST="$(sed -n 's/^STREAM_HUB_HOST=//p' "$ENV_FILE" | head -n 1)"
  EXISTING_PORT="$(sed -n 's/^STREAM_HUB_PORT=//p' "$ENV_FILE" | head -n 1)"
  EXISTING_NODES_FILE="$(sed -n 's/^STREAM_HUB_NODES_FILE=//p' "$ENV_FILE" | head -n 1)"
  EXISTING_TRUSTED_REMOTE_WRITES="$(sed -n 's/^STREAM_HUB_TRUSTED_REMOTE_WRITES=//p' "$ENV_FILE" | head -n 1)"
fi
[ -n "$STREAM_HUB_HOST" ] || STREAM_HUB_HOST="${EXISTING_HOST:-127.0.0.1}"
[ -n "$STREAM_HUB_PORT" ] || STREAM_HUB_PORT="${EXISTING_PORT:-8788}"
[ -n "$STREAM_HUB_NODES_FILE" ] || STREAM_HUB_NODES_FILE="${EXISTING_NODES_FILE:-$INSTALL_DIR/data/nodes.local.json}"
[ -n "$STREAM_HUB_TRUSTED_REMOTE_WRITES" ] || STREAM_HUB_TRUSTED_REMOTE_WRITES="${EXISTING_TRUSTED_REMOTE_WRITES:-0}"
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
STREAM_HUB_NODES_FILE=$NODES_FILE
STREAM_HUB_HOST=$STREAM_HUB_HOST
STREAM_HUB_PORT=$STREAM_HUB_PORT
STREAM_HUB_TRUSTED_REMOTE_WRITES=$STREAM_HUB_TRUSTED_REMOTE_WRITES
EOF
chmod 600 "$ENV_FILE"

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
StartLimitIntervalSec=60
StartLimitBurst=10

[Service]
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/.venv/bin/python -m stream_control_hub
Restart=always
RestartSec=3
TimeoutStopSec=20
KillMode=control-group
UMask=0077

[Install]
WantedBy=$SERVICE_TARGET
EOF
  $SYSTEMCTL daemon-reload || true
  if ! $SYSTEMCTL enable --now stream-control-hub.service; then
    "$INSTALL_DIR/run-hub.sh" &
  else
    $SYSTEMCTL restart stream-control-hub.service
  fi
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
