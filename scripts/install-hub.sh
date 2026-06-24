#!/usr/bin/env sh
set -eu

INSTALL_DIR="${INSTALL_DIR:-$HOME/stream-control-hub}"
REPO_URL="${REPO_URL:-https://github.com/himydearfriends1934-cmyk/stream-control-hub.git}"
BRANCH="${BRANCH:-main}"
STREAM_HUB_HOST="${STREAM_HUB_HOST:-127.0.0.1}"
STREAM_HUB_PORT="${STREAM_HUB_PORT:-8788}"
TAILSCALE_AUTH_KEY="${TAILSCALE_AUTH_KEY:-}"
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-stream-control-hub}"
ACTION="${ACTION:-${STREAM_HUB_ACTION:-install}}"
UNINSTALL="${UNINSTALL:-0}"
REMOVE_DATA="${REMOVE_DATA:-${STREAM_HUB_REMOVE_DATA:-0}}"
CHOICE="${CHOICE:-${STREAM_HUB_CHOICE:-}}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "$1 is required. Install it and run this installer again." >&2
    exit 1
  }
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
  SERVICE_DIR="$HOME/.config/systemd/user"
  SERVICE_FILE="$SERVICE_DIR/stream-control-hub.service"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now stream-control-hub.service >/dev/null 2>&1 || true
    rm -f "$SERVICE_FILE"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
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

mkdir -p "$INSTALL_DIR/data"
NODES_FILE="$INSTALL_DIR/data/nodes.local.json"
[ -f "$NODES_FILE" ] || printf '[]\n' > "$NODES_FILE"

ENV_FILE="$INSTALL_DIR/.env"
TOKEN=""
if [ -f "$ENV_FILE" ]; then
  TOKEN="$(sed -n 's/^STREAM_HUB_CONTROL_TOKEN=//p' "$ENV_FILE" | head -n 1)"
fi
[ -n "$TOKEN" ] || TOKEN="$(new_token)"

cat > "$ENV_FILE" <<EOF
STREAM_HUB_CONTROL_TOKEN=$TOKEN
STREAM_HUB_NODES_FILE=$NODES_FILE
STREAM_HUB_HOST=$STREAM_HUB_HOST
STREAM_HUB_PORT=$STREAM_HUB_PORT
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
  if command -v tailscale >/dev/null 2>&1; then
    tailscale up --auth-key "$TAILSCALE_AUTH_KEY" --hostname "$TAILSCALE_HOSTNAME" --accept-dns=false
  else
    echo "tailscale is not installed. Install Tailscale, then use the Hub Tailscale panel or rerun with TAILSCALE_AUTH_KEY." >&2
  fi
fi

if command -v systemctl >/dev/null 2>&1; then
  SERVICE_DIR="$HOME/.config/systemd/user"
  mkdir -p "$SERVICE_DIR"
  cat > "$SERVICE_DIR/stream-control-hub.service" <<EOF
[Unit]
Description=Stream Control Hub
After=network-online.target

[Service]
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m stream_control_hub
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload || true
  if ! systemctl --user enable --now stream-control-hub.service; then
    "$INSTALL_DIR/run-hub.sh" &
  fi
else
  "$INSTALL_DIR/run-hub.sh" &
fi

echo "Stream Control Hub installed."
echo "Open: http://127.0.0.1:$STREAM_HUB_PORT/?token=$TOKEN"
echo "Nodes file: $NODES_FILE"
