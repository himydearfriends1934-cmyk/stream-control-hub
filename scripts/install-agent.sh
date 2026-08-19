#!/usr/bin/env sh
set -eu

INSTALL_DIR="${INSTALL_DIR:-/opt/stream-control-hub-agent}"
REPO_URL="${REPO_URL:-https://github.com/himydearfriends1934-cmyk/stream-control-hub.git}"
BRANCH="${BRANCH:-main}"
STREAM_AGENT_HOST="${STREAM_AGENT_HOST:-}"
STREAM_AGENT_PORT="${STREAM_AGENT_PORT:-}"
STREAM_AGENT_NAME="${STREAM_AGENT_NAME:-}"
STREAM_AGENT_CONTROL_HUB="${STREAM_AGENT_CONTROL_HUB:-}"
STREAM_AGENT_CONTROL_HUB_CLEAR="${STREAM_AGENT_CONTROL_HUB_CLEAR:-}"
STREAM_AGENT_PUBLIC_ORIGIN="${STREAM_AGENT_PUBLIC_ORIGIN:-}"
STREAM_AUTO_RESTART_ENABLED="${STREAM_AUTO_RESTART_ENABLED:-}"
STREAM_AGENT_TRUSTED_REMOTE_WRITES="${STREAM_AGENT_TRUSTED_REMOTE_WRITES:-}"
STREAM_MEDIA_DIR="${STREAM_MEDIA_DIR:-}"
YOUTUBE_CLIENT_ID="${YOUTUBE_CLIENT_ID:-}"
YOUTUBE_CLIENT_SECRET="${YOUTUBE_CLIENT_SECRET:-}"
TAILSCALE_AUTH_KEY="${TAILSCALE_AUTH_KEY:-}"
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-$STREAM_AGENT_NAME}"
ACTION="${ACTION:-${STREAM_AGENT_ACTION:-install}}"
UNINSTALL="${UNINSTALL:-0}"
REMOVE_DATA="${REMOVE_DATA:-${STREAM_AGENT_REMOVE_DATA:-0}}"
CHOICE="${CHOICE:-${STREAM_AGENT_CHOICE:-}}"
CONFIRM_REMOVE_CONFLICTS="${CONFIRM_REMOVE_CONFLICTS:-0}"
ENV_FILE="$INSTALL_DIR/.agent.env"
ROLE_SWITCH_CONFIRMED="${ROLE_SWITCH_CONFIRMED:-${STREAM_ROLE_SWITCH_CONFIRMED:-0}}"
STREAM_NODE_ROLE_FILE="${STREAM_NODE_ROLE_FILE:-/etc/stream-control/role}"
UPGRADE_LOCK_FILE="${STREAM_NODE_UPGRADE_LOCK_FILE:-$INSTALL_DIR/agent_data/.upgrade.lock}"

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
  current_role=""
  if [ -f "$STREAM_NODE_ROLE_FILE" ]; then
    current_role="$(head -n 1 "$STREAM_NODE_ROLE_FILE" | tr -d '\r' | tr '[:upper:]' '[:lower:]')"
    if [ -n "$current_role" ] && [ "$current_role" != "agent" ] && [ "$ROLE_SWITCH_CONFIRMED" != "1" ]; then
      echo "This machine is declared as $current_role. Deactivate that role before installing Agent." >&2
      exit 6
    fi
  fi
  if command -v systemctl >/dev/null 2>&1 \
    && (systemctl is-active --quiet stream-control-hub.service \
      || systemctl is-enabled --quiet stream-control-hub.service) \
    && ! systemctl is-active --quiet stream-control-headless-agent.service \
    && [ "$current_role" != "agent" ] \
    && [ "$ROLE_SWITCH_CONFIRMED" != "1" ]; then
    echo "HUB service is active. Deactivate HUB before installing Agent." >&2
    exit 6
  fi
}

reconcile_agent_role() {
  if ! command -v systemctl >/dev/null 2>&1; then
    return 0
  fi
  if systemctl is-active --quiet stream-control-hub.service \
    || systemctl is-enabled --quiet stream-control-hub.service; then
    current_role=""
    if [ -f "$STREAM_NODE_ROLE_FILE" ]; then
      current_role="$(head -n 1 "$STREAM_NODE_ROLE_FILE" | tr -d '\r' | tr '[:upper:]' '[:lower:]')"
    fi
    if [ "$ROLE_SWITCH_CONFIRMED" != "1" ] \
      && ! systemctl is-active --quiet stream-control-headless-agent.service \
      && [ "$current_role" != "agent" ]; then
      echo "HUB service is active or enabled. Deactivate HUB before installing Agent." >&2
      return 6
    fi
    echo "Disabling the conflicting HUB service for the Agent role."
    systemctl disable --now stream-control-hub.service
  fi
}

write_agent_service_unit() {
  cat > /etc/systemd/system/stream-control-headless-agent.service <<EOF
[Unit]
Description=Stream Control Hub Headless Agent
After=network-online.target
Wants=network-online.target
Conflicts=stream-control-hub.service
Before=stream-control-hub.service
StartLimitIntervalSec=60
StartLimitBurst=10

[Service]
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.agent.env
Environment=STREAM_NODE_ROLE=agent
Environment=STREAM_NODE_ROLE_FILE=$STREAM_NODE_ROLE_FILE
ExecStart=$INSTALL_DIR/.venv/bin/python -m stream_control_hub.headless_agent
Restart=always
RestartSec=3
TimeoutStopSec=20
KillMode=control-group
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
}

write_role_marker() {
  mkdir -p "$(dirname "$STREAM_NODE_ROLE_FILE")"
  temporary="${STREAM_NODE_ROLE_FILE}.tmp.$$"
  printf 'agent\n' > "$temporary"
  chmod 600 "$temporary" 2>/dev/null || true
  mv -f "$temporary" "$STREAM_NODE_ROLE_FILE"
}

existing_env_value() {
  key="$1"
  [ -f "$ENV_FILE" ] || return 0
  if command -v python3 >/dev/null 2>&1; then
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
        current_key, sep, value = segment.partition("=")
        if sep and current_key.strip() == target:
            print(value.strip().strip("\"'"), end="")
            raise SystemExit(0)
PY
  else
    sed -n "s/^${key}=//p" "$ENV_FILE" | head -n 1
  fi
}

[ -n "$STREAM_AGENT_HOST" ] || STREAM_AGENT_HOST="$(existing_env_value STREAM_AGENT_HOST)"
[ -n "$STREAM_AGENT_HOST" ] || STREAM_AGENT_HOST="0.0.0.0"
[ -n "$STREAM_AGENT_PORT" ] || STREAM_AGENT_PORT="$(existing_env_value STREAM_AGENT_PORT)"
[ -n "$STREAM_AGENT_PORT" ] || STREAM_AGENT_PORT="8787"
[ -n "$STREAM_AGENT_NAME" ] || STREAM_AGENT_NAME="$(existing_env_value STREAM_AGENT_NAME)"
[ -n "$STREAM_AGENT_NAME" ] || STREAM_AGENT_NAME="$(hostname)"
[ -n "$STREAM_AGENT_CONTROL_HUB_CLEAR" ] || STREAM_AGENT_CONTROL_HUB_CLEAR="$(existing_env_value STREAM_AGENT_CONTROL_HUB_CLEAR)"
[ "$STREAM_AGENT_CONTROL_HUB_CLEAR" = "1" ] || STREAM_AGENT_CONTROL_HUB_CLEAR="0"
if [ "$STREAM_AGENT_CONTROL_HUB_CLEAR" != "1" ]; then
  [ -n "$STREAM_AGENT_CONTROL_HUB" ] || STREAM_AGENT_CONTROL_HUB="$(existing_env_value STREAM_AGENT_CONTROL_HUB)"
fi
[ -n "$STREAM_AUTO_RESTART_ENABLED" ] || STREAM_AUTO_RESTART_ENABLED="$(existing_env_value STREAM_AUTO_RESTART_ENABLED)"
[ -n "$STREAM_AUTO_RESTART_ENABLED" ] || STREAM_AUTO_RESTART_ENABLED="1"
[ -n "$STREAM_AGENT_TRUSTED_REMOTE_WRITES" ] || STREAM_AGENT_TRUSTED_REMOTE_WRITES="$(existing_env_value STREAM_AGENT_TRUSTED_REMOTE_WRITES)"
[ -n "$STREAM_AGENT_TRUSTED_REMOTE_WRITES" ] || STREAM_AGENT_TRUSTED_REMOTE_WRITES="0"
[ -n "$STREAM_MEDIA_DIR" ] || STREAM_MEDIA_DIR="$INSTALL_DIR/media"
[ -n "$TAILSCALE_HOSTNAME" ] || TAILSCALE_HOSTNAME="$STREAM_AGENT_NAME"
case "$(printf '%s' "$STREAM_AUTO_RESTART_ENABLED" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) STREAM_AUTO_RESTART_ENABLED="1" ;;
  0|false|no) STREAM_AUTO_RESTART_ENABLED="0" ;;
  *) echo "STREAM_AUTO_RESTART_ENABLED must be 0 or 1." >&2; exit 1 ;;
esac
case "$(printf '%s' "$STREAM_AGENT_TRUSTED_REMOTE_WRITES" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) STREAM_AGENT_TRUSTED_REMOTE_WRITES="1" ;;
  0|false|no) STREAM_AGENT_TRUSTED_REMOTE_WRITES="0" ;;
  *) echo "STREAM_AGENT_TRUSTED_REMOTE_WRITES must be 0 or 1." >&2; exit 1 ;;
esac

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

migrate_shared_media() {
  STREAM_MEDIA_TARGET="$STREAM_MEDIA_DIR" STREAM_INSTALL_DIR="$INSTALL_DIR" python3 - <<'PY'
import filecmp
import os
from pathlib import Path

target = Path(os.environ["STREAM_MEDIA_TARGET"]).expanduser()
install_dir = Path(os.environ["STREAM_INSTALL_DIR"]).expanduser()
target.mkdir(parents=True, exist_ok=True)
for source in (install_dir / "agent_data" / "media", install_dir / "data" / "media"):
    source = source.resolve()
    if source == target.resolve() or not source.is_dir():
        continue
    for item in list(source.iterdir()):
        if not item.is_file():
            continue
        destination = target / item.name
        if destination.exists():
            if filecmp.cmp(item, destination, shallow=False):
                item.unlink()
                continue
            counter = 1
            while True:
                candidate = target / f"{item.stem}-legacy-{counter}{item.suffix}"
                if not candidate.exists():
                    destination = candidate
                    break
                counter += 1
        item.replace(destination)
    try:
        source.rmdir()
    except OSError:
        pass
PY
}

health_check_agent() {
  case "$STREAM_AGENT_HOST" in
    0.0.0.0) probe_host="127.0.0.1" ;;
    ::) probe_host="[::1]" ;;
    *) probe_host="$STREAM_AGENT_HOST" ;;
  esac
  token="$(existing_env_value STREAM_AGENT_CONTROL_TOKEN)"
  probe_url="http://$probe_host:$STREAM_AGENT_PORT/api/status"
  for _ in $(seq 1 20); do
    if systemctl is-active --quiet stream-control-headless-agent.service \
      && curl -fsS --max-time 3 -H "X-Control-Token: $token" "$probe_url" >/dev/null 2>&1 \
      && ! systemctl is-active --quiet stream-control-hub.service; then
      return 0
    fi
    sleep 1
  done
  return 1
}

transactional_refresh_agent() {
  [ -d "$INSTALL_DIR/.git" ] || return 0
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  if [ -n "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=all)" ]; then
    echo "Refusing upgrade: the Agent checkout has local changes." >&2
    return 6
  fi
  old_commit="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
  old_branch="$(git -C "$INSTALL_DIR" branch --show-current)"
  candidate="$INSTALL_DIR/.upgrade-candidate.$$"
  backup="$INSTALL_DIR/agent_data/upgrade-backups/$(date +%Y%m%d%H%M%S)-$old_commit"
  mkdir -p "$INSTALL_DIR/agent_data/upgrade-backups" "$backup"
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
    echo "Candidate Agent failed pre-activation validation; current version was kept." >&2
    return 6
  fi

  code_items="stream_control_hub scripts config requirements.txt README.md"
  for item in $code_items; do
    [ -e "$INSTALL_DIR/$item" ] && mv "$INSTALL_DIR/$item" "$backup/$item"
  done
  [ -e "$INSTALL_DIR/.venv" ] && mv "$INSTALL_DIR/.venv" "$backup/.venv"
  for item in $code_items; do
    [ -e "$candidate/$item" ] && cp -a "$candidate/$item" "$INSTALL_DIR/$item"
  done
  mv "$candidate/.venv" "$INSTALL_DIR/.venv"
  # The candidate files are already in the main worktree. Reset the index and
  # commit pointer to that exact candidate instead of asking checkout to
  # overwrite the files it just received from the candidate worktree.
  git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
  systemctl restart stream-control-headless-agent.service
  if health_check_agent; then
    cleanup_candidate
    echo "Agent upgrade activated; rollback snapshot retained at $backup"
    return 0
  fi

  echo "New Agent failed authenticated health check; rolling back." >&2
  systemctl stop stream-control-headless-agent.service >/dev/null 2>&1 || true
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
  systemctl restart stream-control-headless-agent.service >/dev/null 2>&1 || true
  rm -rf "$backup"
  return 6
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

configure_agent_firewall() {
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow "${STREAM_AGENT_PORT}/tcp" comment 'Stream Control Agent public upload' >/dev/null
  fi
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="${STREAM_AGENT_PORT}/tcp" >/dev/null
    firewall-cmd --reload >/dev/null
  fi
  if command -v iptables >/dev/null 2>&1; then
    iptables -C INPUT -p tcp --dport "$STREAM_AGENT_PORT" -j ACCEPT >/dev/null 2>&1 || \
      iptables -I INPUT 1 -p tcp --dport "$STREAM_AGENT_PORT" -j ACCEPT
  fi
  if command -v ip6tables >/dev/null 2>&1; then
    ip6tables -C INPUT -p tcp --dport "$STREAM_AGENT_PORT" -j ACCEPT >/dev/null 2>&1 || \
      ip6tables -I INPUT 1 -p tcp --dport "$STREAM_AGENT_PORT" -j ACCEPT
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
    case "$service" in
      stream-control-agent-upgrade-*.service) continue ;;
      stream-control-agent-activate-*.service) continue ;;
    esac
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

uninstall_agent() {
  need_root
  systemctl stop stream-control-headless-agent.service >/dev/null 2>&1 || true
  systemctl disable stream-control-headless-agent.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/stream-control-headless-agent.service
  systemctl daemon-reload >/dev/null 2>&1 || true
  rm -f "$STREAM_NODE_ROLE_FILE"
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
  acquire_upgrade_lock
  uninstall_agent
  exit 0
fi

need_root
acquire_upgrade_lock
assert_single_role
install_packages
configure_agent_firewall
need_cmd git
need_cmd python3
need_cmd systemctl
migrate_shared_media
git config --global --add safe.directory "$INSTALL_DIR" >/dev/null 2>&1 || true
reconcile_agent_role
write_agent_service_unit
remove_legacy_conflicts
# The transactional refresh starts the Agent for its health check. During an
# explicit Hub-to-Agent switch, the role marker must already match that unit.
if [ "$ROLE_SWITCH_CONFIRMED" = "1" ]; then
  write_role_marker
fi

if [ -d "$INSTALL_DIR/.git" ]; then
  transactional_refresh_agent
elif [ -e "$INSTALL_DIR" ]; then
  echo "Adopting existing Agent data directory without deleting local media or env: $INSTALL_DIR"
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
  TOKEN="$(existing_env_value STREAM_AGENT_CONTROL_TOKEN)"
  EXISTING_PUBLIC_ORIGIN="$(existing_env_value STREAM_AGENT_PUBLIC_ORIGIN)"
  EXISTING_YOUTUBE_CLIENT_ID="$(existing_env_value YOUTUBE_CLIENT_ID)"
  EXISTING_YOUTUBE_CLIENT_SECRET="$(existing_env_value YOUTUBE_CLIENT_SECRET)"
fi
[ -n "$TOKEN" ] || TOKEN="$(new_token)"
if [ -z "$STREAM_AGENT_PUBLIC_ORIGIN" ]; then
  STREAM_AGENT_PUBLIC_ORIGIN="$EXISTING_PUBLIC_ORIGIN"
fi
if [ -z "$STREAM_AGENT_PUBLIC_ORIGIN" ] && [ -n "$PUBLIC_IP" ]; then
  STREAM_AGENT_PUBLIC_ORIGIN="http://$PUBLIC_IP:$STREAM_AGENT_PORT"
fi
[ -n "$YOUTUBE_CLIENT_ID" ] || YOUTUBE_CLIENT_ID="$EXISTING_YOUTUBE_CLIENT_ID"
[ -n "$YOUTUBE_CLIENT_SECRET" ] || YOUTUBE_CLIENT_SECRET="$EXISTING_YOUTUBE_CLIENT_SECRET"

cat > "$ENV_FILE" <<EOF
STREAM_AGENT_CONTROL_TOKEN=$TOKEN
STREAM_NODE_ROLE=agent
STREAM_NODE_ROLE_FILE=$STREAM_NODE_ROLE_FILE
STREAM_AGENT_HOST=$STREAM_AGENT_HOST
STREAM_AGENT_PORT=$STREAM_AGENT_PORT
STREAM_AGENT_NAME=$STREAM_AGENT_NAME
STREAM_AGENT_CONTROL_HUB=$STREAM_AGENT_CONTROL_HUB
STREAM_AGENT_CONTROL_HUB_CLEAR=0
STREAM_AGENT_PUBLIC_ORIGIN=$STREAM_AGENT_PUBLIC_ORIGIN
STREAM_AGENT_DATA_DIR=$INSTALL_DIR/agent_data
STREAM_MEDIA_DIR=$STREAM_MEDIA_DIR
STREAM_AUTO_RESTART_ENABLED=$STREAM_AUTO_RESTART_ENABLED
STREAM_AGENT_TRUSTED_REMOTE_WRITES=$STREAM_AGENT_TRUSTED_REMOTE_WRITES
YOUTUBE_CLIENT_ID=$YOUTUBE_CLIENT_ID
YOUTUBE_CLIENT_SECRET=$YOUTUBE_CLIENT_SECRET
YOUTUBE_CREDENTIAL_FILE=$INSTALL_DIR/agent_data/youtube_credentials.json
EOF
chmod 600 "$ENV_FILE"
write_role_marker

if [ -n "$TAILSCALE_AUTH_KEY" ]; then
  TAILSCALE_HOSTNAME="$TAILSCALE_HOSTNAME" \
  TAILSCALE_AUTH_KEY="$TAILSCALE_AUTH_KEY" \
  TAILSCALE_ACCEPT_ROUTES="${TAILSCALE_ACCEPT_ROUTES:-1}" \
  sh "$INSTALL_DIR/scripts/tailscale-install.sh" connect
fi

write_agent_service_unit
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
