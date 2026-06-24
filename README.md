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

One-line Hub install/update/uninstall menu on Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/scripts/install-hub.sh | sh
```

Menu options:

- `1` install or update
- `2` uninstall and keep saved data
- `3` uninstall and remove saved data

One-line Headless Agent install/update/uninstall menu on a VPS:

```sh
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/scripts/install-agent.sh | sudo env TAILSCALE_AUTH_KEY=tskey-auth-xxx sh
```

Menu options:

- `1` install or update
- `2` uninstall and keep media/local env
- `3` uninstall and remove media/local env

For non-interactive automation, pass `CHOICE=2` or `CHOICE=3`.

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

The Hub has a Tailscale panel at the bottom of the page. Paste a one-time Tailscale auth key and click connect to run `tailscale up` for the Hub. Headless Agent install accepts the same style of auth key with `TAILSCALE_AUTH_KEY=...`.

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

### Tailscale 连接管理

Hub 页面底部有 `Tailscale 连接` 面板。为了操作方便，可以直接粘贴一次性 Tailscale auth key，Hub 会执行 `tailscale up`，并且不会把 auth key 写入仓库配置或审计日志。

### 推流说明

Headless Agent 支持接收浏览器直传的视频，也支持把已有视频直接共享到其他 Agent，并用 FFmpeg 启动推流。Smart Start 支持在 Hub 页面选择节点、选择服务器视频、填写 YouTube Stream Key，然后一键启动。

直播码只在启动请求里临时转发，不会写入 Hub 的仓库配置。为了方便操作，页面保留直播码输入框；建议只在本机、Tailscale 或可信内网里使用。

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
