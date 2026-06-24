#!/usr/bin/env sh
set -eu

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

json_string() {
  if command_exists python3; then
    JSON_VALUE="${1:-}" python3 -c 'import json, os; print(json.dumps(os.environ.get("JSON_VALUE", "")), end="")'
  else
    printf '"%s"' "$(printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g')"
  fi
}

bool_json() {
  if [ "${1:-false}" = "true" ]; then
    printf 'true'
  else
    printf 'false'
  fi
}

has_privilege() {
  if [ "$(id -u 2>/dev/null || printf 1)" = "0" ]; then
    return 0
  fi
  command_exists sudo && sudo -n true >/dev/null 2>&1
}

run_with_privilege() {
  if [ "$(id -u 2>/dev/null || printf 1)" = "0" ]; then
    "$@"
  elif command_exists sudo && sudo -n true >/dev/null 2>&1; then
    sudo -n "$@"
  else
    printf 'root or passwordless sudo is required for this Tailscale action.\n' >&2
    return 1
  fi
}

safe_hostname() {
  value="${1:-stream-control-hub}"
  cleaned="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9-' '-')"
  cleaned="$(printf '%s' "$cleaned" | sed 's/^-*//; s/-*$//; s/--*/-/g')"
  if [ -z "$cleaned" ]; then
    cleaned="stream-control-hub"
  fi
  printf '%.63s' "$cleaned"
}

package_manager() {
  if command_exists apt-get; then
    printf 'apt'
  elif command_exists dnf; then
    printf 'dnf'
  elif command_exists yum; then
    printf 'yum'
  elif command_exists apk; then
    printf 'apk'
  else
    printf 'none'
  fi
}

tailscale_precheck() {
  package_manager_value="$(package_manager)"
  curl_ok="false"
  wget_ok="false"
  needs_fetch_tool="false"
  can_reach_tailscale="false"
  privilege_ok="false"
  systemd_ok="false"
  kernel_ok="false"
  tun_ok="false"
  arch_id="$(uname -m 2>/dev/null || printf unknown)"
  os_id="linux"
  os_name="Linux"

  if [ -r /etc/os-release ]; then
    os_id="$(. /etc/os-release && printf '%s' "${ID:-linux}")"
    os_name="$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-Linux}")"
  fi

  if command_exists curl; then
    curl_ok="true"
  fi
  if command_exists wget; then
    wget_ok="true"
  fi
  if [ "$curl_ok" != "true" ] && [ "$wget_ok" != "true" ]; then
    needs_fetch_tool="true"
  fi
  if has_privilege; then
    privilege_ok="true"
  fi
  if command_exists systemctl; then
    systemd_ok="true"
  fi
  if [ -r /proc/version ]; then
    kernel_ok="true"
  fi
  if [ -c /dev/net/tun ] || lsmod 2>/dev/null | grep -q '^tun'; then
    tun_ok="true"
  fi
  if command_exists curl && curl -fsSI --connect-timeout 5 https://tailscale.com/install.sh >/dev/null 2>&1; then
    can_reach_tailscale="true"
  elif command_exists wget && wget -q --spider --timeout=5 https://tailscale.com/install.sh >/dev/null 2>&1; then
    can_reach_tailscale="true"
  elif [ "$needs_fetch_tool" = "true" ]; then
    can_reach_tailscale="unknown"
  fi

  ok="false"
  if [ "$package_manager_value" != "none" ] && [ "$privilege_ok" = "true" ] && [ "$kernel_ok" = "true" ] && [ "$can_reach_tailscale" != "false" ]; then
    ok="true"
  fi

  printf '{'
  printf '"ok":'; bool_json "$ok"; printf ','
  printf '"osId":'; json_string "$os_id"; printf ','
  printf '"osName":'; json_string "$os_name"; printf ','
  printf '"arch":'; json_string "$arch_id"; printf ','
  printf '"packageManager":'; json_string "$package_manager_value"; printf ','
  printf '"hasCurl":'; bool_json "$curl_ok"; printf ','
  printf '"hasWget":'; bool_json "$wget_ok"; printf ','
  printf '"needsFetchTool":'; bool_json "$needs_fetch_tool"; printf ','
  if [ "$can_reach_tailscale" = "true" ]; then
    printf '"canReachTailscale":true,'
  elif [ "$can_reach_tailscale" = "unknown" ]; then
    printf '"canReachTailscale":null,'
  else
    printf '"canReachTailscale":false,'
  fi
  printf '"hasPrivilege":'; bool_json "$privilege_ok"; printf ','
  printf '"hasSystemd":'; bool_json "$systemd_ok"; printf ','
  printf '"hasTun":'; bool_json "$tun_ok"; printf ','
  printf '"installed":'; if command_exists tailscale; then printf 'true'; else printf 'false'; fi; printf ','
  printf '"recommendedInstall":'; json_string "curl -fsSL https://tailscale.com/install.sh | sh"; printf ','
  printf '"message":'
  if [ "$package_manager_value" = "none" ]; then
    json_string "No supported package manager was detected. Supported flows cover apt, dnf, yum, and apk."
  elif [ "$can_reach_tailscale" = "false" ]; then
    json_string "This machine cannot reach https://tailscale.com/install.sh right now. Check DNS or outbound network first."
  elif [ "$privilege_ok" != "true" ]; then
    json_string "This user cannot elevate privileges non-interactively. Run as root or configure passwordless sudo."
  elif [ "$kernel_ok" != "true" ]; then
    json_string "Kernel/proc information is unavailable. Tailscale installation may not succeed."
  else
    json_string "Environment looks installable. The helper can install or repair Tailscale."
  fi
  printf '}\n'
}

install_fetch_tool_if_needed() {
  if command_exists curl || command_exists wget; then
    return 0
  fi
  printf 'curl/wget was not found. Trying to install curl and ca-certificates first...\n'
  case "$(package_manager)" in
    apt)
      run_with_privilege apt-get update 2>&1 || true
      run_with_privilege apt-get install -y curl ca-certificates 2>&1 || true
      ;;
    dnf)
      run_with_privilege dnf install -y curl ca-certificates 2>&1 || true
      ;;
    yum)
      run_with_privilege yum install -y curl ca-certificates 2>&1 || true
      ;;
    apk)
      run_with_privilege apk add --no-cache curl ca-certificates 2>&1 || true
      ;;
  esac
}

tailscale_install() {
  check="$(tailscale_precheck)"
  if ! printf '%s' "$check" | grep -q '"ok":true'; then
    printf '%s\n' "$check" >&2
    exit 2
  fi

  printf '[1/4] Checking environment...\n'
  printf '%s\n' "$check"
  printf '\n[2/4] Installing or repairing Tailscale...\n'
  install_fetch_tool_if_needed

  if command_exists curl; then
    run_with_privilege sh -c "curl -fsSL https://tailscale.com/install.sh | sh"
  elif command_exists wget; then
    run_with_privilege sh -c "wget -qO- https://tailscale.com/install.sh | sh"
  else
    printf 'Neither curl nor wget is available.\n' >&2
    exit 2
  fi

  printf '\n[3/4] Enabling tailscaled...\n'
  if command_exists systemctl; then
    run_with_privilege systemctl enable --now tailscaled 2>&1 || true
  elif command_exists service; then
    run_with_privilege service tailscaled start 2>&1 || true
  fi

  printf '\n[4/4] Install/Fix complete.\n'
  if command_exists tailscale; then
    tailscale version 2>/dev/null | head -n 1 || true
  fi
}

tailscale_up() {
  auth_key="${TAILSCALE_AUTH_KEY:-}"
  hostname="$(safe_hostname "${TAILSCALE_HOSTNAME:-stream-control-hub}")"
  accept_routes="${TAILSCALE_ACCEPT_ROUTES:-1}"
  enable_ssh="${TAILSCALE_SSH:-0}"
  reset_config="${TAILSCALE_RESET:-0}"

  if [ -z "$auth_key" ]; then
    printf 'Missing TAILSCALE_AUTH_KEY.\n' >&2
    exit 2
  fi
  if ! command_exists tailscale; then
    printf 'tailscale is not installed yet.\n' >&2
    exit 2
  fi

  printf '[1/3] Verifying Tailscale install...\n'
  tailscale version 2>/dev/null | head -n 1 || true

  printf '\n[2/3] Bringing node online...\n'
  set -- up --auth-key "$auth_key" --hostname "$hostname" --accept-dns=false
  if [ "$accept_routes" = "1" ] || [ "$accept_routes" = "true" ] || [ "$accept_routes" = "yes" ]; then
    set -- "$@" --accept-routes
  fi
  if [ "$enable_ssh" = "1" ] || [ "$enable_ssh" = "true" ] || [ "$enable_ssh" = "yes" ]; then
    set -- "$@" --ssh
  fi
  if [ "$reset_config" = "1" ] || [ "$reset_config" = "true" ] || [ "$reset_config" = "yes" ]; then
    set -- "$@" --reset
  fi
  run_with_privilege tailscale "$@"

  printf '\n[3/3] Reading final status...\n'
  tailscale status --json
}

tailscale_connect() {
  if ! command_exists tailscale; then
    tailscale_install
  fi
  tailscale_up
}

tailscale_status() {
  if ! command_exists tailscale; then
    printf '{"ok":false,"installed":false,"message":"tailscale is not installed"}\n'
    exit 1
  fi
  tailscale status --json
}

case "${1:-connect}" in
  precheck) tailscale_precheck ;;
  install|fix|install-fix) tailscale_install ;;
  up|login) tailscale_up ;;
  connect) tailscale_connect ;;
  status) tailscale_status ;;
  *)
    printf 'Usage: %s {precheck|install|up|connect|status}\n' "$0" >&2
    exit 2
    ;;
esac
