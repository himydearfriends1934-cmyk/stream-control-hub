from __future__ import annotations

import shlex
from typing import Any


DEFAULT_INSTALL_DIR = "/opt/stream-control-hub"
DEFAULT_SERVICE_NAME = "stream-control-node-agent"
DEFAULT_SERVICE_USER = "streamdash"
DEFAULT_STREAM_DIR = "/srv/stream-videos"
DEFAULT_AGENT_PORT = 8787
DEFAULT_HUB_PORT = 8788


def shell_quote(value: object) -> str:
    return shlex.quote(str(value))


def systemd_env_quote(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def node_value(node: dict[str, Any], key: str, default: Any = "") -> Any:
    value = node.get(key)
    if value in (None, ""):
        return default
    return value


def deployment_options(
    node: dict[str, Any],
    *,
    source_repo: str,
    source_branch: str,
    default_control_hub_url: str = "",
) -> dict[str, Any]:
    install_dir = str(node_value(node, "install_dir", DEFAULT_INSTALL_DIR)).rstrip("/")
    service_name = str(node_value(node, "service_name", DEFAULT_SERVICE_NAME))
    service_user = str(node_value(node, "service_user", DEFAULT_SERVICE_USER))
    stream_dir = str(node_value(node, "stream_upload_dir", DEFAULT_STREAM_DIR)).rstrip("/")
    data_dir = str(node_value(node, "agent_data_dir", f"/var/lib/{service_name}")).rstrip("/")
    port = int(node_value(node, "agent_port", DEFAULT_AGENT_PORT))
    control_hub_url = str(node_value(node, "control_hub_url", default_control_hub_url)).rstrip("/")
    node_name = str(node_value(node, "name", node_value(node, "id", "stream-node")))
    return {
        "install_dir": install_dir,
        "service_name": service_name,
        "service_user": service_user,
        "stream_dir": stream_dir,
        "data_dir": data_dir,
        "port": port,
        "node_name": node_name,
        "control_hub_url": control_hub_url,
        "repo": str(node_value(node, "source_repo", source_repo)),
        "branch": str(node_value(node, "source_branch", source_branch)),
        "bind_host": str(node_value(node, "agent_bind_host", "127.0.0.1")),
        "public_upload_firewall": str(node_value(node, "public_upload_firewall", "none")),
        "stream_relay_enabled": "1" if bool(node.get("stream_relay_enabled")) else "0",
    }


def agent_environment(options: dict[str, Any]) -> dict[str, str]:
    install_dir = options["install_dir"]
    service_name = options["service_name"]
    data_dir = options["data_dir"]
    return {
        "PYTHONUNBUFFERED": "1",
        "PORT": str(options["port"]),
        "STREAM_CONTROL_ROLE": "agent",
        "STREAM_NODE_AGENT_MODE": "1",
        "STREAM_NODE_AGENT_NAME": str(options["node_name"]),
        "CONTROL_HUB_URL": str(options["control_hub_url"]),
        "STREAM_UPLOAD_DIR": str(options["stream_dir"]),
        "STREAM_LOG_FILE": f"/var/log/{service_name}/stream.log",
        "STREAM_CONFIG_FILE": f"{data_dir}/stream_config.json",
        "STREAM_TUNING_FILE": f"{data_dir}/stream_tuning.json",
        "STREAM_RUNTIME_FILE": f"{data_dir}/stream_runtime_state.json",
        "STREAM_PID_FILE": f"/run/{service_name}/ffmpeg.pid",
        "CHAT_PLAN_FILE": f"{data_dir}/chat_plan.json",
        "YOUTUBE_CLIENT_FILE": f"{data_dir}/youtube_client.json",
        "YOUTUBE_TOKEN_FILE": f"{data_dir}/youtube_token.json",
        "DASHBOARD_SECRET_FILE": f"{data_dir}/dashboard_secret.key",
        "DASHBOARD_PASSWORD_FILE": f"{data_dir}/dashboard_password.txt",
        "PUBLIC_UPLOAD_FIREWALL": str(options["public_upload_firewall"]),
        "PUBLIC_UPLOAD_RESTRICT": "1",
        "PUBLIC_UPLOAD_CLOSE_ON_START": "1",
        "STREAM_AUTORESTART_ENABLED": "1",
        "STREAM_RELAY_ENABLED": str(options["stream_relay_enabled"]),
    }


def systemd_unit(options: dict[str, Any]) -> str:
    env = agent_environment(options)
    env_lines = "\n".join(f"Environment={systemd_env_quote(f'{key}={value}')}" for key, value in env.items())
    install_dir = options["install_dir"]
    service_user = options["service_user"]
    service_name = options["service_name"]
    bind_host = options["bind_host"]
    port = options["port"]
    return f"""[Unit]
Description=Stream Control Hub Node Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={service_user}
Group={service_user}
WorkingDirectory={install_dir}
{env_lines}
RuntimeDirectory={service_name}
ExecStart={install_dir}/.venv/bin/gunicorn --worker-class gthread --workers 1 --threads 4 --timeout 3600 --graceful-timeout 30 --bind {bind_host}:{port} stream_control_hub.node_agent.wsgi:APP
KillMode=process
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""


def bootstrap_script(options: dict[str, Any]) -> str:
    install_dir = options["install_dir"]
    service_user = options["service_user"]
    service_name = options["service_name"]
    stream_dir = options["stream_dir"]
    data_dir = options["data_dir"]
    repo = options["repo"]
    branch = options["branch"]
    unit = systemd_unit(options).rstrip()
    return f"""#!/usr/bin/env bash
set -euo pipefail

APP_DIR={shell_quote(install_dir)}
SERVICE_USER={shell_quote(service_user)}
SERVICE_NAME={shell_quote(service_name)}
STREAM_DIR={shell_quote(stream_dir)}
DATA_DIR={shell_quote(data_dir)}
REPO_URL={shell_quote(repo)}
BRANCH={shell_quote(branch)}

sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip ffmpeg

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  sudo useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

sudo mkdir -p "$APP_DIR" "$DATA_DIR" "$STREAM_DIR" "/var/log/$SERVICE_NAME"
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$DATA_DIR" "$STREAM_DIR" "/var/log/$SERVICE_NAME"

if [ ! -d "$APP_DIR/.git" ]; then
  sudo -u "$SERVICE_USER" git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  sudo -u "$SERVICE_USER" git -C "$APP_DIR" fetch origin "$BRANCH"
  sudo -u "$SERVICE_USER" git -C "$APP_DIR" checkout "$BRANCH"
  sudo -u "$SERVICE_USER" git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
fi

sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

sudo tee "/etc/systemd/system/$SERVICE_NAME.service" >/dev/null <<'UNIT'
{unit}
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME.service"
sudo systemctl restart "$SERVICE_NAME.service"
sudo systemctl status "$SERVICE_NAME.service" --no-pager
"""


def upgrade_commands(options: dict[str, Any]) -> list[str]:
    install_dir = options["install_dir"]
    service_user = options["service_user"]
    service_name = options["service_name"]
    branch = options["branch"]
    return [
        f"sudo -u {shell_quote(service_user)} git -C {shell_quote(install_dir)} fetch origin {shell_quote(branch)}",
        f"sudo -u {shell_quote(service_user)} git -C {shell_quote(install_dir)} pull --ff-only origin {shell_quote(branch)}",
        f"sudo -u {shell_quote(service_user)} {shell_quote(install_dir)}/.venv/bin/pip install -r {shell_quote(install_dir)}/requirements.txt",
        "sudo systemctl daemon-reload",
        f"sudo systemctl restart {shell_quote(service_name)}.service",
        f"sudo systemctl status {shell_quote(service_name)}.service --no-pager",
    ]


def raw_script_url(*, source_repo: str, source_branch: str, script_name: str) -> str:
    repo = source_repo.removesuffix(".git").replace("https://github.com/", "")
    return f"https://raw.githubusercontent.com/{repo}/{source_branch}/deploy/{script_name}"


def hub_one_liner(*, source_repo: str, source_branch: str, port: int = DEFAULT_HUB_PORT, bind_host: str = "0.0.0.0") -> str:
    url = raw_script_url(source_repo=source_repo, source_branch=source_branch, script_name="install-hub.sh")
    return (
        f"curl -fsSL {shell_quote(url)} | sudo bash -s -- "
        f"--branch {shell_quote(source_branch)} --port {int(port)} --bind {shell_quote(bind_host)}"
    )


def agent_one_liner(
    *,
    source_repo: str,
    source_branch: str,
    hub_url: str,
    node_name: str = "stream-node",
    port: int = DEFAULT_AGENT_PORT,
    bind_host: str = "0.0.0.0",
) -> str:
    url = raw_script_url(source_repo=source_repo, source_branch=source_branch, script_name="install-agent.sh")
    return (
        f"curl -fsSL {shell_quote(url)} | sudo bash -s -- "
        f"--branch {shell_quote(source_branch)} "
        f"--hub-url {shell_quote(hub_url)} "
        f"--node-name {shell_quote(node_name)} "
        f"--port {int(port)} --bind {shell_quote(bind_host)}"
    )


def build_deployment_plan(
    node: dict[str, Any],
    *,
    source_repo: str,
    source_branch: str,
    default_control_hub_url: str = "",
    include_script: bool = True,
) -> dict[str, Any]:
    options = deployment_options(
        node,
        source_repo=source_repo,
        source_branch=source_branch,
        default_control_hub_url=default_control_hub_url,
    )
    warnings = [
        "This plan does not execute SSH commands from the Hub.",
        "Review secrets and bind_host before running the script on a VPS.",
        "Use a private network or reverse proxy before exposing the agent.",
    ]
    if not options["control_hub_url"]:
        warnings.append("CONTROL_HUB_URL is empty; set STREAM_HUB_PUBLIC_URL or node.control_hub_url before production use.")

    plan = {
        "node_id": str(node.get("id") or ""),
        "node_name": options["node_name"],
        "ok": True,
        "mode": "manual-reviewed",
        "transport": "not-executed",
        "warnings": warnings,
        "options": options,
        "environment": agent_environment(options),
        "systemd_unit": systemd_unit(options),
        "upgrade_commands": upgrade_commands(options),
        "one_liner": agent_one_liner(
            source_repo=options["repo"],
            source_branch=options["branch"],
            hub_url=options["control_hub_url"],
            node_name=options["node_name"],
            port=options["port"],
            bind_host=options["bind_host"],
        ),
    }
    if include_script:
        plan["bootstrap_script"] = bootstrap_script(options)
    return plan
