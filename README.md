# Stream Control Hub

Local control hub for managing multiple VPS stream nodes.

The hub is designed to run on a local server. VPS nodes stay lightweight: they keep streaming, receive media files directly, share media with other agents, report health, and accept controlled updates.

## Goals

- Upload media directly from the browser to a selected Agent, without storing the video on the Hub.
- Share an existing video from one Agent to other Agents.
- Watch all stream nodes from one place.
- Check GitHub updates centrally and deploy them to nodes.
- Keep FFmpeg streaming processes independent from panel upgrades.
- Never store server secrets in the repository.

## Quick Start

One-line Hub install on Windows:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/scripts/install-hub.ps1 -UseBasicParsing | iex"
```

One-line Hub uninstall on Windows, preserving saved data:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:STREAM_HUB_ACTION='uninstall'; iwr https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/scripts/install-hub.ps1 -UseBasicParsing | iex"
```

One-line system Hub install/update/uninstall menu on Linux (recommended for a VPS):

```sh
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/scripts/install-hub.sh | sudo sh
```

This installs a root-run Hub at `/opt/stream-control-hub` with a system service. For an unprivileged per-user install, omit `sudo`; it uses `$HOME/stream-control-hub` and a user service. Updates auto-detect an existing installation in either location, preserve its host, port, nodes-file path, control token, and trusted-write policy, and retain the matching systemd service scope. You can override this explicitly with `INSTALL_DIR=...` and `STREAM_HUB_SERVICE_MODE=system|user`.

For a Hub bound only to a trusted Tailscale address, `STREAM_HUB_TRUSTED_REMOTE_WRITES=1` allows Tailnet clients to use control actions without placing the Hub token in the browser. This broadens control access to every client that can reach the Hub, so leave the default `0` on public or shared networks. The installer persists the selected policy across later updates.

Menu options:

- `1` install or update
- `2` uninstall and keep saved data
- `3` uninstall and remove saved data

One-line Headless Agent install/update/uninstall menu on a VPS:

```sh
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/scripts/install-agent.sh | sudo env STREAM_AGENT_CONTROL_HUB=http://100.64.0.1:8788 TAILSCALE_AUTH_KEY=tskey-auth-xxx sh
```

Menu options:

- `1` install or update
- `2` uninstall and keep media/local env
- `3` uninstall and remove media/local env

For non-interactive automation, pass `CHOICE=2` or `CHOICE=3`.

Before an Agent install or update, the installer stops the managed Agent service and scans for known legacy dashboards, services, project directories, and listeners on port `8787`. It prints the complete cleanup report and requires the exact confirmation `DELETE` before removing recognized legacy projects. For explicitly approved unattended replacement, pass `CONFIRM_REMOVE_CONFLICTS=1`. An unknown process that still occupies the Agent port is never killed automatically; installation stops with its listener details.

After installation, the service must pass an authenticated `/api/status` check before the script reports success. The generated fallback node registration, including its control token, is stored at `/opt/stream-control-hub-agent/node-registration.json` with mode `600` instead of being printed to terminal logs.

Agent updates preserve the existing bind host, port, Agent name, Hub pairing URL, public upload origin, auto-restart policy, trusted-write policy, and YouTube OAuth client settings. Both Linux services use a restrictive `0077` umask, bounded restart timing, process-group cleanup, and an explicit environment file so manual systemd drop-ins are not needed.

When `STREAM_AGENT_CONTROL_HUB` points to the Hub's Tailscale URL, the Agent trusts API traffic only from that exact Tailscale source IP. In the Hub Tailscale wizard, choose `新增 Agent（仅输入 IP）`, enter the Agent's `100.x` address, and connect. Other tailnet peers still need the per-Agent control token.

The installers generate a local control token and keep node secrets outside git. The Hub prints a URL like `http://127.0.0.1:8788/?token=...`; use that URL for remote write actions when `STREAM_HUB_CONTROL_TOKEN` is enabled.

Manual local development:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy config\nodes.example.json config\nodes.json
.\.venv\Scripts\python -m stream_control_hub
```

Open `http://127.0.0.1:8788`.

## Node Model

Each node entry describes how the hub reaches a VPS dashboard or node agent over a trusted network such as Tailscale.

```json
{
  "id": "sample-tailnet-node",
  "name": "Sample Tailnet Node",
  "base_url": "http://100.64.0.10:8787",
  "upload_base_url": "http://198.51.100.10:8787",
  "role": "stream-node"
}
```

Future secure deployment should use a local secret bridge or per-node tokens outside this repo.

For installed Hubs, add real nodes to the generated local file shown by the installer, usually `data/nodes.local.json`. Do not commit real node IPs or tokens into `config/nodes.json`.

Node entries may include a per-agent control token:

```json
{
  "id": "hk-agent",
  "name": "HK Agent",
  "base_url": "http://100.64.0.10:8787",
  "upload_base_url": "http://203.0.113.10:8787",
  "role": "stream-node",
  "enabled": true,
  "token": "generated-agent-token"
}
```

Use `base_url` for trusted control traffic, normally Tailscale. Use `upload_base_url` for the VPS public IP or public DNS name. Browser uploads automatically probe public and Tailscale routes, choose the fastest working route, and use a short-lived upload ticket instead of exposing the long-lived Agent token to the browser.

## Tailscale

The Hub has a Tailscale panel at the bottom of the page. Paste a one-time Tailscale auth key and click connect; the Hub reuses the Tailscale install flow from `vps-pulse-control`: pre-check Linux environment, install or repair Tailscale when missing, enable `tailscaled`, run `tailscale up`, and read final status.

The Linux Hub and Headless Agent one-line installers use the same helper when `TAILSCALE_AUTH_KEY=...` is supplied, so a fresh VPS does not need Tailscale preinstalled.

The Agent automatically discovers its public IPv4 during install and update. It uses `api.ipify.org` first and falls back to `ifconfig.me/ip`, validates that the response is a global IPv4 address, and publishes `http://<public-ip>:8787` to the Hub. Browser uploads probe that public route first and automatically fall back to the Agent Tailscale address when the public port is unavailable. Users do not need to enter `upload_base_url` manually.

Active streams are supervised by the Headless Agent. While a stream is desired, the Agent stores a mode `600` recovery payload in its private data directory, restarts an unexpectedly exited FFmpeg process with bounded exponential backoff, and exposes restart status in monitoring. A manual stop disables recovery and immediately removes the recovery payload. Stream keys are never returned by the API or written to the general runtime state file.

## YouTube Live API

The Headless Agent supports the official YouTube Live Streaming API through OAuth 2.0 device authorization. The Hub only coordinates with YouTube stream and broadcast IDs. The Google refresh token and the RTMP stream name stay on the Agent; neither is returned to the Hub. Automatic FFmpeg recovery resolves the ingestion target again from the saved YouTube stream ID, so no YouTube stream key is needed in the recovery payload.

1. Enable YouTube Data API v3 in Google Cloud.
2. Create an OAuth client with application type `TVs and Limited Input devices`.
3. Install or update the Agent with its client ID. The client secret is optional for clients that do not issue one:

```sh
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/scripts/install-agent.sh | sudo env STREAM_AGENT_CONTROL_HUB='http://100.64.0.1:8788' YOUTUBE_CLIENT_ID='your-client-id' YOUTUBE_CLIENT_SECRET='your-optional-client-secret' sh
```

The installer preserves these values on later updates and stores `.agent.env` with mode `600`. In the Hub, select the Agent, open `YouTube API`, click `连接 YouTube`, enter the displayed device code on Google's authorization page, then create or bind a broadcast. The Agent stores the resulting refresh token at `agent_data/youtube_credentials.json` with mode `600`.

Agent endpoints:

```text
GET  /api/youtube/status
POST /api/youtube/oauth/start
POST /api/youtube/oauth/poll
POST /api/youtube/oauth/revoke
GET  /api/youtube/streams
GET  /api/youtube/broadcasts
POST /api/youtube/prepare
```

Official references: [OAuth 2.0 for TV and limited-input devices](https://developers.google.com/youtube/v3/guides/auth/devices) and [YouTube Live Streaming API](https://developers.google.com/youtube/v3/live/getting-started).

The app does not write the Tailscale auth key to repo config or audit logs. The intended deployment model is local or trusted-network use, preferably behind Tailscale.

## Operational Safety

For day-to-day use, the Hub favors simple local controls: paste a one-time Tailscale auth key into the Hub panel, paste a YouTube stream key into Smart Start, and keep real node tokens in the generated local `data/nodes.local.json` file rather than in git.

When Codex needs to log in to VPS machines or use stored external credentials on your behalf, use your local NewsBoardSecureAgent workflow outside the app. That keeps assistant-driven VPS access separate from the Hub's normal one-screen operator flow.

## Media Transfer

The Hub is a coordinator, not the media warehouse. Browser uploads go straight to the selected Agent through `/api/upload-chunk`, and existing Agent videos can be copied directly from one Agent to another through `/api/share-media`.

- Direct Agent uploads use large chunks and show per-chunk and average speed in the Hub UI.
- Public Agent uploads use Hub-issued short-lived upload tickets; the Agent control token stays on the Hub.
- Agent-to-Agent sharing uses large chunks and retries a small number of times before cleanup.
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

## 中文说明

Stream Control Hub 是一个本地总控台，用来集中管理多台 VPS 推流节点。Hub 负责查看节点状态、连接 Tailscale、下发 Smart Start 推流请求和调度资源共享；视频文件从浏览器直接上传到 Agent，不先落到 Hub。VPS 上的 Headless Agent 保持轻量，负责接收文件、与其他 Agent 共享资源、上报状态和启动 FFmpeg。

### Hub 一行安装 / 卸载菜单

Windows:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/scripts/install-hub.ps1 -UseBasicParsing | iex"
```

Windows 一行卸载，默认保留数据：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:STREAM_HUB_ACTION='uninstall'; iwr https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/scripts/install-hub.ps1 -UseBasicParsing | iex"
```

Linux 一行菜单：

```sh
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/scripts/install-hub.sh | sh
```

菜单选项：

```text
1) 安装或更新
2) 卸载，保留数据
3) 卸载，并删除数据
```

自动化场景可以使用 `CHOICE=2` 或 `CHOICE=3`。

安装完成后，脚本会输出类似下面的地址：

```text
http://127.0.0.1:8788/?token=...
```

这个 token 是 Hub 的控制令牌。远程打开 Hub 时，上传、推送、连接 Tailscale、检查更新等写操作需要带这个 token。

### Headless Agent 一行安装 / 卸载菜单

在 VPS 上执行：

```sh
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/scripts/install-agent.sh | sudo env TAILSCALE_AUTH_KEY=tskey-auth-xxx sh
```

如果 VPS 已经加入 Tailscale，也可以直接：

```sh
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/scripts/install-agent.sh | sudo sh
```

执行一行命令后会出现菜单：

```text
1) 安装或更新
2) 卸载，保留媒体文件和本地环境配置
3) 卸载，并删除媒体文件和本地环境配置
```

自动化场景可以使用 `CHOICE=2` 或 `CHOICE=3`。卸载脚本只处理 `stream-control-headless-agent.service` 和安装目录，不会停止或删除 `sing-box`。

Agent 安装完成后会输出一段节点 JSON。把这段 JSON 加到 Hub 安装脚本提示的本地节点文件里，通常是：

```text
data/nodes.local.json
```

不要把真实节点 IP、域名、token 提交到 `config/nodes.json`。

### 节点配置示例

```json
{
  "id": "hk-agent",
  "name": "HK Agent",
  "base_url": "http://100.64.0.10:8787",
  "role": "stream-node",
  "enabled": true,
  "token": "generated-agent-token"
}
```

`base_url` 建议使用 Tailscale IP 或内网地址。`token` 是 Agent 安装时生成的控制令牌，Hub 会自动带上它访问 Agent。


公网文件上传说明：

- `base_url` 建议继续填 Tailscale 地址，用于 Hub 管理 Agent、申请上传票据、读取状态。
- `upload_base_url` 填 VPS 公网 IP 或公网域名，用于浏览器直传大视频和 Agent 之间共享。
- Hub 会自动测速公网和 Tailscale，优先走公网；公网不可达时自动回退 Tailscale。
- 浏览器拿到的是短期 `X-Upload-Ticket`，不是 Agent 长期控制 token，票据只绑定这一次上传。
- 浏览器直传默认使用 8MB 分块，页面会显示进度、速度、预计剩余时间；上传中可以点击“取消上传”，Agent 会清理临时分片。
- Hub 的资源区会按上传时间倒序显示全部 Agent 上的视频，可以在 Hub 页面查看详情、选用、编辑名称、删除，也可以把选中的视频从源 Agent 共享到勾选的其他 Agent。

### Tailscale 连接管理

Hub 页面底部有 `Tailscale 连接` 面板。为了操作方便，可以直接粘贴一次性 Tailscale auth key。Hub 会复用 `vps-pulse-control` 里的 Tailscale 安装流程：先检查 Linux 环境、包管理器、提权能力、TUN 和 tailscale.com 连通性；如果没安装就自动安装/修复 Tailscale；然后执行 `tailscale up` 并读取最终状态。

Linux Hub 和 Headless Agent 一键安装脚本在传入 `TAILSCALE_AUTH_KEY=...` 时也会走同一个 helper，所以新 VPS 不需要提前手动装 Tailscale。auth key 不会写入仓库配置或审计日志。

### 推流说明

Headless Agent 支持接收浏览器直传的视频，也支持把已有视频直接共享到其他 Agent，并用 FFmpeg 启动推流。Smart Start 支持手动填写 YouTube Stream Key，也支持从 YouTube API 向导选择已授权的直播流后一键启动。

手动直播码不会写入 Hub 的仓库配置；推流期间只保存在 Agent 的 `0600` 恢复文件中，停止推流立即删除。YouTube API 模式下，refresh token 和 RTMP stream name 只留在 Agent，Hub 只传递 stream ID。建议只在本机、Tailscale 或可信内网里使用。

### 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy config\nodes.example.json config\nodes.json
.\.venv\Scripts\python -m stream_control_hub
```

打开：

```text
http://127.0.0.1:8788
```

### 使用和安全建议

- 这个项目按“本机 / 内网 / Tailscale”场景设计，优先保证一站式、少步骤操作。
- 不要提交 `.env`、`.agent.env`、`data/`、`agent_data/`。
- 不要把真实 VPS IP、Tailscale IP、控制 token 写进仓库里的示例配置。
- 如果把 Hub 绑定到 `0.0.0.0`，一定要使用 token，并优先放在 Tailscale 或可信内网里。
- Codex 帮你登录 VPS 或读取远程服务器信息时，才使用本地 NewsBoardSecureAgent；这不影响 Hub 自身的日常操作流程。
