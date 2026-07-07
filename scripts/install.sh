#!/usr/bin/env sh
set -eu

REPO_RAW_URL="${REPO_RAW_URL:-https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main}"
REPO_CONTENTS_API="${REPO_CONTENTS_API:-https://api.github.com/repos/himydearfriends1934-cmyk/stream-control-hub/contents}"
BRANCH="${BRANCH:-main}"
HUB_INSTALL_DIR="${HUB_INSTALL_DIR:-/opt/stream-control-hub}"
AGENT_INSTALL_DIR="${AGENT_INSTALL_DIR:-/opt/stream-control-hub-agent}"
CHOICE="${CHOICE:-${STREAM_CONTROL_CHOICE:-}}"
REMOVE_DATA="${REMOVE_DATA:-}"
TMP_DIR=""

cleanup() { [ -z "$TMP_DIR" ] || rm -rf "$TMP_DIR"; }
trap cleanup EXIT HUP INT TERM

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "This unified installer manages system services and must run as root." >&2
    echo "Run: curl -fsSL $REPO_RAW_URL/scripts/install.sh | sudo sh" >&2
    exit 1
  fi
}

fetch() {
  url="$1"; output="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --connect-timeout 15 "$url" -o "$output"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --tries=3 --timeout=15 "$url" -O "$output"
  else
    echo "curl or wget is required." >&2; exit 1
  fi
}

fetch_repo_script() {
  script_name="$1"; output="$2"
  api_url="$REPO_CONTENTS_API/scripts/$script_name?ref=$BRANCH&ts=$(date +%s)"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --connect-timeout 15 \
      -H "Accept: application/vnd.github.raw+json" \
      -H "Cache-Control: no-cache" \
      "$api_url" -o "$output"
  elif command -v wget >/dev/null 2>&1; then
    fetch "$REPO_RAW_URL/scripts/$script_name?ts=$(date +%s)" "$output"
  else
    echo "curl or wget is required." >&2; exit 1
  fi
}

show_menu() {
  cat <<'EOF'

Stream Control Hub - Linux Unified Manager
================================================
1) Install Hub
2) Upgrade / repair Hub
3) Uninstall Hub
4) Install Agent
5) Upgrade / repair Agent
6) Uninstall Agent
7) Install / repair / connect Tailscale
8) Show Hub, Agent and Tailscale status
0) Exit
================================================
EOF
  printf "Choose [0-8]: "
}

read_choice() {
  if [ -n "$CHOICE" ]; then
    return 0
  fi
  if [ -r /dev/tty ] && [ -w /dev/tty ]; then
    show_menu > /dev/tty
    read -r CHOICE < /dev/tty || CHOICE=""
  else
    echo "No interactive terminal. Set CHOICE=1..8." >&2
    echo "Example: curl -fsSL $REPO_RAW_URL/scripts/install.sh | sudo env CHOICE=1 sh" >&2
    exit 1
  fi
}

confirm_remove_data() {
  component="$1"
  if [ -n "$REMOVE_DATA" ]; then
    return 0
  fi
  if [ -r /dev/tty ] && [ -w /dev/tty ]; then
    printf "Remove all %s data too? [y/N]: " "$component" > /dev/tty
    answer=""; read -r answer < /dev/tty || answer=""
    case "$answer" in y|Y|yes|YES) REMOVE_DATA="1" ;; *) REMOVE_DATA="0" ;; esac
  else
    REMOVE_DATA="0"
  fi
}

run_remote_script() {
  script_name="$1"; shift
  script_path="$TMP_DIR/$script_name"
  echo "Downloading latest $script_name from GitHub ($BRANCH)..."
  fetch_repo_script "$script_name" "$script_path"
  chmod 700 "$script_path"
  env "$@" sh "$script_path"
}

configure_tailscale() {
  auth_key="${TAILSCALE_AUTH_KEY:-}"
  if [ -z "$auth_key" ] && [ -r /dev/tty ] && [ -w /dev/tty ]; then
    printf "One-time Tailscale auth key (leave empty to install/repair only): " > /dev/tty
    stty -echo < /dev/tty 2>/dev/null || true
    read -r auth_key < /dev/tty || auth_key=""
    stty echo < /dev/tty 2>/dev/null || true
    printf "\n" > /dev/tty
  fi
  if [ -n "$auth_key" ]; then
    run_remote_script tailscale-install.sh TAILSCALE_AUTH_KEY="$auth_key" TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-stream-control-node}"
  else
    helper_path="$TMP_DIR/tailscale-install.sh"
    fetch_repo_script tailscale-install.sh "$helper_path"
    chmod 700 "$helper_path"
    sh "$helper_path" install
    echo "Tailscale installed/repaired. Run option 7 again with a one-time auth key to connect this device."
  fi
}

hub_installed() { [ -f "$HUB_INSTALL_DIR/.env" ] || [ -d "$HUB_INSTALL_DIR/.git" ]; }
agent_installed() { [ -f "$AGENT_INSTALL_DIR/.agent.env" ] || [ -d "$AGENT_INSTALL_DIR/.git" ]; }

show_status() {
  echo "Hub directory: $HUB_INSTALL_DIR"
  if hub_installed; then echo "Hub files: installed"; else echo "Hub files: not installed"; fi
  if systemctl is-active --quiet stream-control-hub.service 2>/dev/null; then echo "Hub service: running"; else echo "Hub service: stopped or not installed"; fi
  echo
  echo "Agent directory: $AGENT_INSTALL_DIR"
  if agent_installed; then echo "Agent files: installed"; else echo "Agent files: not installed"; fi
  if systemctl is-active --quiet stream-control-headless-agent.service 2>/dev/null; then echo "Agent service: running"; else echo "Agent service: stopped or not installed"; fi
  echo
  if command -v tailscale >/dev/null 2>&1; then
    tailscale status 2>/dev/null || true
    tailscale ip -4 2>/dev/null | sed 's/^/Tailscale IPv4: /' || true
  else
    echo "Tailscale: not installed"
  fi
}

need_root
TMP_DIR="$(mktemp -d)"
read_choice

case "$CHOICE" in
  1) run_remote_script install-hub.sh ACTION=install CHOICE=1 INSTALL_DIR="$HUB_INSTALL_DIR" ;;
  2)
    if ! hub_installed; then echo "Hub is not installed at $HUB_INSTALL_DIR; choose 1 first." >&2; exit 1; fi
    run_remote_script install-hub.sh ACTION=install CHOICE=1 INSTALL_DIR="$HUB_INSTALL_DIR"
    ;;
  3)
    confirm_remove_data "Hub"
    run_remote_script install-hub.sh ACTION=uninstall REMOVE_DATA="$REMOVE_DATA" INSTALL_DIR="$HUB_INSTALL_DIR"
    ;;
  4) run_remote_script install-agent.sh ACTION=install CHOICE=1 INSTALL_DIR="$AGENT_INSTALL_DIR" ;;
  5)
    if ! agent_installed; then echo "Agent is not installed at $AGENT_INSTALL_DIR; choose 4 first." >&2; exit 1; fi
    run_remote_script install-agent.sh ACTION=install CHOICE=1 INSTALL_DIR="$AGENT_INSTALL_DIR"
    ;;
  6)
    confirm_remove_data "Agent"
    run_remote_script install-agent.sh ACTION=uninstall REMOVE_DATA="$REMOVE_DATA" INSTALL_DIR="$AGENT_INSTALL_DIR"
    ;;
  7) configure_tailscale ;;
  8) show_status ;;
  0) echo "No changes made." ;;
  *) echo "Invalid choice: $CHOICE (expected 0-8)" >&2; exit 1 ;;
esac
