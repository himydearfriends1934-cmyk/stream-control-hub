# Stream Control Hub

Local control hub and VPS stream node agent in one repository.

The same codebase now contains:

- a local Hub for managing multiple stream nodes
- a node Agent API for VPS status, uploads, and FFmpeg control
- the optional node Dashboard UI for direct single-node operation

## Installation Entrypoints

Use these as the GitHub landing-page install entrypoints.

| Target | Platform | Entry |
| --- | --- | --- |
| Hub UI/control plane | Windows | [Quick Start: Windows Hub](#quick-start-windows-hub) |
| Hub UI/control plane | Linux VPS | [One-Line Linux Hub](#one-line-linux-hub) |
| Headless Agent node | Windows | [Quick Start: Windows Headless Agent](#quick-start-windows-headless-agent) |
| Headless Agent node | Linux VPS | [One-Line Linux Headless Agent](#one-line-linux-headless-agent) |

If you are testing the current PR branch before merge, replace `main` in the
Linux one-liners with `codex/unify-agent-dashboard`.

## Linux Clean-Install Guard

The Linux installers scan the server before installing. If they find an old
Hub, old Headless Agent, old stream dashboard, matching systemd units, fixed
project directories, data directories, log directories, env files, release
folders, backup folders, or a port conflict, they print a summary first.

When old project content is found, the installer stops and asks for
confirmation. Type `DELETE` in the terminal to stop and disable old services,
remove old systemd unit files, delete old project paths, reload systemd, verify
the install port is clear, and then continue with the new install.

For unattended automation, pass `--confirm-delete-old`. This is intentionally
explicit because it deletes old project data, logs, backups, and uploaded media
that live under the detected project paths.

## Goals

- Upload media once to the local hub, then push it to selected VPS nodes.
- Watch all stream nodes from one place.
- Run headless VPS agents that expose a stable API to the Hub.
- Keep the node Dashboard UI separate from the Agent API.
- Check GitHub updates centrally and deploy them to nodes.
- Keep FFmpeg streaming processes independent from panel upgrades.
- Never store server secrets in the repository.

## Quick Start: Windows Hub

```powershell
git clone https://github.com/himydearfriends1934-cmyk/stream-control-hub.git
cd stream-control-hub
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy config\nodes.example.json config\nodes.json
.\.venv\Scripts\python -m stream_control_hub
```

Open `http://127.0.0.1:8788`.

## Quick Start: Windows Headless Agent

```powershell
git clone https://github.com/himydearfriends1934-cmyk/stream-control-hub.git
cd stream-control-hub
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:STREAM_CONTROL_ROLE="agent"
$env:STREAM_NODE_AGENT_MODE="1"
$env:PORT="8787"
.\.venv\Scripts\python -m stream_control_hub
```

Open `http://127.0.0.1:8787`.

In headless mode, `/` returns the agent contract as JSON. Set
`STREAM_NODE_AGENT_MODE=0` to serve the single-node Dashboard UI.

## One-Line Linux Hub

Use fixed systemd services, fixed install directories, and TailScale/private IPs.
Do not paste secrets into commands. SSH credentials stay behind
NewsBoardSecureAgent.

```bash
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/deploy/install-hub.sh | sudo bash -s -- --bind 0.0.0.0 --port 8788
```

This creates the fixed `stream-control-hub.service` systemd service.
If old project content is detected, the script shows the cleanup report and asks
you to type `DELETE` before it removes the old install.

Unattended clean install:

```bash
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/deploy/install-hub.sh | sudo bash -s -- --bind 0.0.0.0 --port 8788 --confirm-delete-old
```

## One-Line Linux Headless Agent

```bash
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/deploy/install-agent.sh | sudo bash -s -- --hub-url http://<hub-tailscale-ip>:8788 --node-name <agent-name> --bind 0.0.0.0 --port 8787
```

This creates the fixed `stream-control-node-agent.service` systemd service.
If old project content is detected, the script shows the cleanup report and asks
you to type `DELETE` before it removes the old install.

Unattended clean install:

```bash
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/deploy/install-agent.sh | sudo bash -s -- --hub-url http://<hub-tailscale-ip>:8788 --node-name <agent-name> --bind 0.0.0.0 --port 8787 --confirm-delete-old
```

After the Agent is running, open the Hub and enter the Agent TailScale IP in the
`Connect Agent` field. The Hub persists it in `config/nodes.json`, checks
`/api/status`, and then uses the Agent API for updates and media sync.

## Repository Layout

```text
stream_control_hub/app.py                 Hub backend and Hub UI
stream_control_hub/deployment.py          Agent deployment plan builder
stream_control_hub/node_agent/settings.py Environment-backed node settings
stream_control_hub/node_agent/state.py Process-local mutable node state
stream_control_hub/node_agent/runtime.py  Node guards, FFmpeg lifecycle, watchdogs
stream_control_hub/node_agent/agent_api.py Agent status route
stream_control_hub/node_agent/chat_api.py Chat-plan and chat-helper routes
stream_control_hub/node_agent/chat.py Chat-plan state and YouTube chat scheduler
stream_control_hub/node_agent/youtube_api.py YouTube OAuth routes
stream_control_hub/node_agent/youtube.py YouTube OAuth and live chat client helpers
stream_control_hub/node_agent/upload_api.py Upload and public upload-window routes
stream_control_hub/node_agent/uploads.py Upload window, transfer state, and media file helpers
stream_control_hub/node_agent/stream_api.py Streaming control and tuning routes
stream_control_hub/node_agent/dashboard_ui.py Node Dashboard UI routes
stream_control_hub/node_agent/dashboard_templates.py Dashboard HTML templates
stream_control_hub/node_agent/streaming.py Stream config, probing, tuning helpers
deploy/stream-control-node-agent.service  Example systemd unit for a VPS node
deploy/install-hub.sh                     One-line Hub installer
deploy/install-agent.sh                   One-line Headless Agent installer
```

## Node Model

Each node entry describes how the hub reaches a VPS dashboard or node agent over a trusted network such as Tailscale.

```json
{
  "id": "node-a",
  "name": "Primary Stream Node",
  "base_url": "http://100.64.0.10:8787",
  "role": "stream-node"
}
```

Keep the real `config/nodes.json` local. Only `config/nodes.example.json` is tracked.

Future secure deployment should use a local secret bridge or per-node tokens outside this repo.

## Node Agent API

The Hub talks to these node endpoints:

```text
GET  /api/status
GET  /api/public-upload
POST /api/public-upload/open
POST /api/public-upload/heartbeat
POST /api/public-upload/close
POST /api/upload-probe
POST /api/upload-chunk
POST /api/upload-chunk/cancel
POST /api/start-stream
POST /api/stream/recommend
GET  /api/stream/tuning
POST /api/stream/tuning
```

## Deployment Module

The Hub exposes a safe deployment planner that does not execute SSH commands:

```text
GET  /api/deploy/oneliners
POST /api/nodes/deploy/plan
POST /api/nodes/upgrade
POST /api/nodes/connect-agent
```

`/api/nodes/deploy/plan` returns a reviewed bootstrap script, systemd unit, and
upgrade commands for the selected node ids. `/api/nodes/upgrade` returns the
upgrade command list only. Set `STREAM_HUB_PUBLIC_URL` or per-node
`control_hub_url` so generated agents know how to report back to the Hub.

Headless agent mode is controlled by:

```text
STREAM_CONTROL_ROLE=agent
STREAM_NODE_AGENT_MODE=1
STREAM_NODE_AGENT_NAME=<node-name>
CONTROL_HUB_URL=http://<hub-host>:8788
```

## Media Push

The hub uploads media to VPS nodes through the existing dashboard chunk API.

- Public upload windows are preferred when a node supports `/api/public-upload/open`.
- The temporary public token is held in memory only and is never written to logs or config.
- Chunk uploads retry a small number of times before cleanup.
- Failed pushes call `/api/upload-chunk/cancel` through the node `base_url`, then close the public window if one was opened.
- Public routes must pass `/api/upload-probe` and the minimum speed threshold before large chunks are sent.
- If a public route fails during transfer, the hub closes the public window and continues over the node `base_url` with the same upload id and chunk size.
- Each push writes a token-free audit event with policy, route, probe, speed, fallback, and cleanup details.

Policy endpoints:

```text
GET /api/policy
GET /api/push-audit?limit=50
```

Useful environment variables:

```text
STREAM_HUB_UPLOAD_POLICY_NAME=safe-stable-fast-v1
STREAM_HUB_NODE_UPLOAD_CHUNK_BYTES=8388608
STREAM_HUB_NODE_PUBLIC_UPLOAD_CHUNK_BYTES=16777216
STREAM_HUB_NODE_UPLOAD_TIMEOUT_SECONDS=300
STREAM_HUB_NODE_PUBLIC_UPLOAD_TTL_SECONDS=900
STREAM_HUB_NODE_UPLOAD_RETRIES=2
STREAM_HUB_NODE_UPLOAD_PROBE_BYTES=262144
STREAM_HUB_NODE_UPLOAD_PROBE_TIMEOUT_SECONDS=12
STREAM_HUB_MIN_PUBLIC_UPLOAD_BYTES_PER_SECOND=32768
STREAM_HUB_MIN_FREE_AFTER_UPLOAD_BYTES=2147483648
STREAM_HUB_PUSH_AUDIT_LOG_MAX_BYTES=5242880
```
