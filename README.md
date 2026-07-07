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

Recommended unified Linux manager for Hub, Agent, Tailscale, upgrades, uninstalls, and status:

```bash
curl -fsSL -H 'Accept: application/vnd.github.raw+json' 'https://api.github.com/repos/himydearfriends1934-cmyk/stream-control-hub/contents/scripts/install.sh?ref=main' | sudo sh
```

It opens a numbered `0-8` menu. Hub is installed under `/opt/stream-control-hub` and Agent under `/opt/stream-control-hub-agent`. For automation, pass `CHOICE=1` through `CHOICE=8`.

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

Each Agent row in the Hub shows its current Git revision and has its own `升级 / 安装` action. The action schedules an independent systemd upgrade job on that Agent, pulls GitHub `main`, runs the standard installer, and restarts only that Agent. Older copied deployments without Git metadata are bootstrapped in place while preserving `.agent.env` and `agent_data`. The Hub also remembers the last viewed Agent in browser-local storage and restores it on the next visit.

The enlarged node-management area separates `Agent 组` and `Hub 组`. Each VPS can run both roles independently: Agent uses port `8787`, Hub uses `8788`. Enabled roles show their Git version and can be upgraded individually; disabled roles are shown in gray and require an explicit security confirmation before activation. Clicking an enabled Hub switches the browser to that Hub. Background Hub installation suppresses control-token output from systemd logs.

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

Tailscale and the Headless Agent have separate jobs. Tailscale only provides private network connectivity. Install the Agent only on machines that the Hub must control for media upload, sharing, health reporting, or FFmpeg streaming. The Hub host and ordinary operator devices can join Tailscale without installing the Agent.

For a new streaming node, enter only its `100.x` address in the Hub. The Hub verifies that the address is an online peer in the same tailnet, checks for the Headless Agent on port `8787`, and completes automatic pairing. The Agent releases its generated control credential only after local `tailscale whois` confirms that the caller is an authenticated tailnet peer. Set `STREAM_AGENT_TAILSCALE_PAIRING=0` to disable this convenience feature.

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
- Uploads and Agent-to-Agent sharing are public-only. If no public address is configured, the public probe fails, or a public transfer is interrupted, the operation stops with an explicit error and never falls back to the Tailscale/internal `base_url`.
- Each push writes a token-free audit event with policy, route, probe, speed, fallback, and cleanup details.
- The global media library aggregates every online Agent, sorts videos by upload time, supports persistent groups, and shows per-node disk usage.
- Uploads are assigned to the selected group and still go directly to the currently selected Agent.
- Smart Start can select any grouped library item. If the target Agent has no local copy, the Hub copies one from an online source Agent over the public-only transfer route before streaming.
- Media filenames preserve Unicode, including Chinese, Japanese, spaces, and full-width punctuation; only path/control characters and unsafe ASCII filename characters are removed.
- Every Agent-to-Agent copy is verified by SHA-256 and size. Verified source/new-copy pairs coexist for 72 hours; after that, automatic cleanup rechecks that both complete copies still exist before deleting the older source copy.
- Automatic and batch cleanup can delete verified duplicate copies only. It never deletes the final/only copy of a video. Single-copy deletion remains an explicit manual action.
- Agents record the last time each video was used for streaming. The resource manager fades filenames in three-day tiers and uses a dashed underline for long-idle manual-review candidates.

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

推荐使用统一 Linux 管理入口。Hub、Agent 和 Tailscale 的安装、升级、卸载与状态检查都使用同一条命令：

```bash
curl -fsSL -H 'Accept: application/vnd.github.raw+json' 'https://api.github.com/repos/himydearfriends1934-cmyk/stream-control-hub/contents/scripts/install.sh?ref=main' | sudo sh
```

运行后显示菜单：`1` 安装 Hub、`2` 升级/修复 Hub、`3` 卸载 Hub、`4` 安装 Agent、`5` 升级/修复 Agent、`6` 卸载 Agent、`7` 安装/修复/连接 Tailscale、`8` 查看状态、`0` 退出。卸载时会继续询问是否保留数据。

自动化可以直接指定编号，例如安装 Agent：

```bash
curl -fsSL -H 'Accept: application/vnd.github.raw+json' 'https://api.github.com/repos/himydearfriends1934-cmyk/stream-control-hub/contents/scripts/install.sh?ref=main' | sudo env CHOICE=4 sh
```

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

Hub 页面底部有 `Agent 快速连接` 面板，只需填写目标 Agent 的 Tailscale `100.x` 地址。弹窗中的 Agent 一键安装命令每次打开时都会从 GitHub 的 `config/install-commands.json` 读取最新版；GitHub 暂时不可达时使用随 Hub 发布的本地备用清单。

Tailscale 与 Agent 的职责不同：Tailscale 只建立私网连接，Agent 才提供视频上传、资源共享、状态上报和 FFmpeg 推流控制。Hub 主机以及只用于访问面板的电脑、手机无需安装 Agent；只有要作为推流节点受 Hub 管理的服务器才需要同时安装 Tailscale 和 Agent。

接入新推流节点时只需填写它的 `100.x` 地址。Hub 会检查该地址是否为同一 Tailnet 中的在线设备、探测 `8787` 端口上的 Headless Agent，并自动完成授权和节点保存。Agent 只有在本机 `tailscale whois` 确认请求来自已认证 Tailnet peer 后才返回配对凭据；如需关闭此功能，可设置 `STREAM_AGENT_TAILSCALE_PAIRING=0`。

Linux Hub 和 Headless Agent 一键安装脚本在传入 `TAILSCALE_AUTH_KEY=...` 时也会走同一个 helper，所以新 VPS 不需要提前手动装 Tailscale。auth key 不会写入仓库配置或审计日志。

### 推流说明

资源管理器会汇总所有在线 Agent 的视频并按上传时间倒序显示，支持分组新增、改名、删除和视频归组，同时用条形图显示每个节点的磁盘已用与剩余空间。上传仍以当前选中的 Agent 为目标，并可在上传前选择分组。Smart Start 可以从任意分组选择视频；若当前开播节点没有该视频，Hub 会先从拥有副本的在线节点通过公网复制一份，再启动推流。中文、日文、空格和全角标点文件名会原样保留。

Agent 间复制完成后会比较源文件和新副本的 SHA-256 与大小；只有完全一致才进入重复副本保留规则。两份副本共存 72 小时，到期后系统再次确认两份完整且哈希一致，才删除旧副本。自动清理和批量“清理视频”只能删除这种已验证重复副本，绝不会删除唯一文件；唯一文件只能人工逐个删除。Agent 还会记录最后开播使用时间，资源管理器按每 3 天一档降低长期未使用文件名亮度，最长档用虚线标记为人工评估候选。

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
