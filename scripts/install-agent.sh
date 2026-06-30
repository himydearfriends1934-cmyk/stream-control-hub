#!/usr/bin/env sh
set -eu

INSTALL_DIR="${INSTALL_DIR:-/opt/stream-control-hub-agent}"
REPO_URL="${REPO_URL:-https://github.com/himydearfriends1934-cmyk/stream-control-hub.git}"
BRANCH="${BRANCH:-main}"
STREAM_AGENT_HOST="${STREAM_AGENT_HOST:-0.0.0.0}"
STREAM_AGENT_PORT="${STREAM_AGENT_PORT:-8787}"
STREAM_AGENT_NAME="${STREAM_AGENT_NAME:-$(hostname)}"
STREAM_AGENT_CONTROL_HUB="${STREAM_AGENT_CONTROL_HUB:-}"
STREAM_AGENT_PUBLIC_ORIGIN="${STREAM_AGENT_PUBLIC_ORIGIN:-}"
STREAM_AUTO_RESTART_ENABLED="${STREAM_AUTO_RESTART_ENABLED:-1}"
YOUTUBE_CLIENT_ID="${YOUTUBE_CLIENT_ID:-}"
YOUTUBE_CLIENT_SECRET="${YOUTUBE_CLIENT_SECRET:-}"
TAILSCALE_AUTH_KEY="${TAILSCALE_AUTH_KEY:-}"
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-$STREAM_AGENT_NAME}"
ACTION="${ACTION:-${STREAM_AGENT_ACTION:-install}}"
UNINSTALL="${UNINSTALL:-0}"
REMOVE_DATA="${REMOVE_DATA:-${STREAM_AGENT_REMOVE_DATA:-0}}"
CHOICE="${CHOICE:-${STREAM_AGENT_CHOICE:-}}"
CONFIRM_REMOVE_CONFLICTS="${CONFIRM_REMOVE_CONFLICTS:-0}"

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
  curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || \
    curl -4 -fsS --max-time 8 https://ifconfig.me/ip 2>/dev/null || true
}

remove_legacy_conflicts() {
  legacy_services=""
  legacy_paths=""

  systemctl stop stream-control-headless-agent.service >/dev/null 2>&1 || true
  sleep 1

  for service in \
    lightcone-stream-dashboard.service \
    stream-control-node-agent.service \
    stream-dashboard.service \
    istanbul-stream-dashboard.service; do
    if systemctl cat "$service" >/dev/null 2>&1; then
      legacy_services="$legacy_services $service"
    fi
  done

  for service in $(systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}' | grep -E '(stream.*(agent|dashboard)|(agent|dashboard).*stream)' || true); do
    [ "$service" = "stream-control-headless-agent.service" ] && continue
    case " $legacy_services " in
      *" $service "*) ;;
      *) legacy_services="$legacy_services $service" ;;
    esac
  done

  for path in \
    /opt/lightcone-stream-dashboard \
    /opt/stream-control-node-agent \
    /opt/stream-dashboard \
    /opt/istanbul-stream-dashboard \
    /etc/lightcone-stream-dashboard \
    /etc/stream-dashboard \
    /etc/istanbul-stream-dashboard \
    /var/lib/lightcone-stream-dashboard \
    /var/lib/stream-dashboard \
    /var/lib/istanbul-stream-dashboard \
    /var/log/lightcone-stream-dashboard \
    /var/log/stream-dashboard \
    /var/log/istanbul-stream-dashboard; do
    if [ -e "$path" ] || [ -L "$path" ]; then
      legacy_paths="$legacy_paths $path"
    fi
  done

  for service in $legacy_services; do
    path="$(systemctl show "$service" -p WorkingDirectory --value 2>/dev/null || true)"
    case "$path" in
      /opt/*)
        if [ "$path" != "$INSTALL_DIR" ]; then
          case " $legacy_paths " in
            *" $path "*) ;;
            *) legacy_paths="$legacy_paths $path" ;;
          esac
        fi
        ;;
    esac
  done

  port_conflicts=""
  if command -v ss >/dev/null 2>&1; then
    port_conflicts="$(ss -H -ltnp "sport = :$STREAM_AGENT_PORT" 2>/dev/null || true)"
  fi

  if [ -n "$legacy_services$legacy_paths" ]; then
    echo "Legacy or conflicting stream installations were found."
    service_count=$(set -- $legacy_services; echo "$#")
    path_count=$(set -- $legacy_paths; echo "$#")
    port_count=$(printf '%s\n' "$port_conflicts" | grep -c . || true)
    echo "Summary: services=$service_count paths=$path_count port_listeners=$port_count"
    echo "Services to permanently stop and remove:"
    for service in $legacy_services; do echo "  - $service"; done
    echo "Project paths to permanently delete:"
    for path in $legacy_paths; do echo "  - $path"; done
    if [ -n "$port_conflicts" ]; then
      echo "Current listeners on port $STREAM_AGENT_PORT:"
      printf '%s\n' "$port_conflicts"
    fi

    if [ "$CONFIRM_REMOVE_CONFLICTS" != "1" ]; then
      if [ ! -r /dev/tty ]; then
        echo "A terminal confirmation is required. Re-run with CONFIRM_REMOVE_CONFLICTS=1 for unattended cleanup." >&2
        exit 3
      fi
      printf "Type DELETE to remove every listed legacy project and continue: " > /dev/tty
      read -r answer < /dev/tty || answer=""
      if [ "$answer" != "DELETE" ]; then
        echo "Install cancelled. No legacy project content was deleted."
        exit 4
      fi
    fi

    for service in $legacy_services; do
      systemctl disable --now "$service" >/dev/null 2>&1 || true
      rm -f \
        "/etc/systemd/system/$service" \
        "/lib/systemd/system/$service" \
        "/usr/lib/systemd/system/$service"
    done
    systemctl daemon-reload >/dev/null 2>&1 || true
    for path in $legacy_paths; do
      rm -rf -- "$path"
    done
    echo "Legacy stream projects removed."
  fi

  if command -v ss >/dev/null 2>&1; then
    remaining="$(ss -H -ltnp "sport = :$STREAM_AGENT_PORT" 2>/dev/null || true)"
    if [ -n "$remaining" ]; then
      echo "Port $STREAM_AGENT_PORT is still occupied after legacy cleanup:" >&2
      printf '%s\n' "$remaining" >&2
      echo "Stop the unrelated listener or choose another STREAM_AGENT_PORT." >&2
      exit 5
    fi
  fi
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
need_cmd systemctl
remove_legacy_conflicts

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
EXISTING_PUBLIC_ORIGIN=""
EXISTING_YOUTUBE_CLIENT_ID=""
EXISTING_YOUTUBE_CLIENT_SECRET=""
PUBLIC_IP_RAW="$(public_ip | tr -d '\r\n')"
PUBLIC_IP="$(PUBLIC_IP_RAW="$PUBLIC_IP_RAW" python3 - <<'PY'
import ipaddress
import os

try:
    value = ipaddress.ip_address(os.environ.get("PUBLIC_IP_RAW", "").strip())
except ValueError:
    value = None
print(value if value and value.version == 4 and value.is_global else "")
PY
)"
if [ -f "$ENV_FILE" ]; then
  TOKEN="$(sed -n 's/^STREAM_AGENT_CONTROL_TOKEN=//p' "$ENV_FILE" | head -n 1)"
  EXISTING_PUBLIC_ORIGIN="$(sed -n 's/^STREAM_AGENT_PUBLIC_ORIGIN=//p' "$ENV_FILE" | head -n 1)"
  EXISTING_YOUTUBE_CLIENT_ID="$(sed -n 's/^YOUTUBE_CLIENT_ID=//p' "$ENV_FILE" | head -n 1)"
  EXISTING_YOUTUBE_CLIENT_SECRET="$(sed -n 's/^YOUTUBE_CLIENT_SECRET=//p' "$ENV_FILE" | head -n 1)"
fi
[ -n "$TOKEN" ] || TOKEN="$(new_token)"
if [ -z "$STREAM_AGENT_PUBLIC_ORIGIN" ]; then
  if [ -n "$PUBLIC_IP" ]; then
    STREAM_AGENT_PUBLIC_ORIGIN="http://$PUBLIC_IP:$STREAM_AGENT_PORT"
  else
    STREAM_AGENT_PUBLIC_ORIGIN="$EXISTING_PUBLIC_ORIGIN"
  fi
fi
[ -n "$YOUTUBE_CLIENT_ID" ] || YOUTUBE_CLIENT_ID="$EXISTING_YOUTUBE_CLIENT_ID"
[ -n "$YOUTUBE_CLIENT_SECRET" ] || YOUTUBE_CLIENT_SECRET="$EXISTING_YOUTUBE_CLIENT_SECRET"

cat > "$ENV_FILE" <<EOF
STREAM_AGENT_CONTROL_TOKEN=$TOKEN
STREAM_AGENT_HOST=$STREAM_AGENT_HOST
STREAM_AGENT_PORT=$STREAM_AGENT_PORT
STREAM_AGENT_NAME=$STREAM_AGENT_NAME
STREAM_AGENT_CONTROL_HUB=$STREAM_AGENT_CONTROL_HUB
STREAM_AGENT_PUBLIC_ORIGIN=$STREAM_AGENT_PUBLIC_ORIGIN
STREAM_AGENT_DATA_DIR=$INSTALL_DIR/agent_data
STREAM_AUTO_RESTART_ENABLED=$STREAM_AUTO_RESTART_ENABLED
YOUTUBE_CLIENT_ID=$YOUTUBE_CLIENT_ID
YOUTUBE_CLIENT_SECRET=$YOUTUBE_CLIENT_SECRET
YOUTUBE_CREDENTIAL_FILE=$INSTALL_DIR/agent_data/youtube_credentials.json
EOF
chmod 600 "$ENV_FILE"

if [ -n "$TAILSCALE_AUTH_KEY" ]; then
  TAILSCALE_HOSTNAME="$TAILSCALE_HOSTNAME" \
  TAILSCALE_AUTH_KEY="$TAILSCALE_AUTH_KEY" \
  TAILSCALE_ACCEPT_ROUTES="${TAILSCALE_ACCEPT_ROUTES:-1}" \
  sh "$INSTALL_DIR/scripts/tailscale-install.sh" connect
fi

cat > /etc/systemd/system/stream-control-headless-agent.service <<EOF
[Unit]
Description=Stream Control Hub Headless Agent
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=10

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
systemctl enable stream-control-headless-agent.service
systemctl reset-failed stream-control-headless-agent.service >/dev/null 2>&1 || true
systemctl restart stream-control-headless-agent.service

case "$STREAM_AGENT_HOST" in
  0.0.0.0) PROBE_HOST="127.0.0.1" ;;
  ::) PROBE_HOST="[::1]" ;;
  *) PROBE_HOST="$STREAM_AGENT_HOST" ;;
esac
PROBE_URL="http://$PROBE_HOST:$STREAM_AGENT_PORT/api/status"
HEALTHY="0"
for _ in $(seq 1 20); do
  if systemctl is-active --quiet stream-control-headless-agent.service && \
    ENV_FILE="$ENV_FILE" PROBE_URL="$PROBE_URL" "$INSTALL_DIR/.venv/bin/python" - <<'PY' >/dev/null 2>&1
import os
import urllib.request

token = ""
with open(os.environ["ENV_FILE"], encoding="utf-8") as env_file:
    for line in env_file:
        if line.startswith("STREAM_AGENT_CONTROL_TOKEN="):
            token = line.split("=", 1)[1].strip()
            break
request = urllib.request.Request(os.environ["PROBE_URL"], headers={"X-Control-Token": token})
with urllib.request.urlopen(request, timeout=3) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
  then
    HEALTHY="1"
    break
  fi
  sleep 1
done

if [ "$HEALTHY" != "1" ]; then
  echo "Headless Agent failed its authenticated health check." >&2
  systemctl status stream-control-headless-agent.service --no-pager -l >&2 || true
  journalctl -u stream-control-headless-agent.service -n 40 --no-pager >&2 || true
  exit 6
fi

NODE_IP="$(tailscale_ip)"
[ -n "$NODE_IP" ] || NODE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

echo "Stream Control Headless Agent installed."
REGISTRATION_FILE="$INSTALL_DIR/node-registration.json"
if [ -n "$PUBLIC_IP" ] && [ "$PUBLIC_IP" != "$NODE_IP" ]; then
cat > "$REGISTRATION_FILE" <<EOF
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
cat > "$REGISTRATION_FILE" <<EOF
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
chmod 600 "$REGISTRATION_FILE"
echo "Agent health check passed at http://$NODE_IP:$STREAM_AGENT_PORT/api/status"
if [ -n "$STREAM_AGENT_CONTROL_HUB" ]; then
  echo "IP-only Hub pairing enabled for: $STREAM_AGENT_CONTROL_HUB"
else
  echo "Set STREAM_AGENT_CONTROL_HUB to the Hub Tailscale URL to enable secure IP-only pairing."
fi
echo "Fallback node registration was saved with mode 600 at: $REGISTRATION_FILE"
