# Stream Control Hub

Local control hub for managing multiple VPS stream nodes.

The hub is designed to run on a local server. VPS nodes stay lightweight: they keep streaming, receive media files, report health, and accept controlled updates.

## Goals

- Upload media once to the local hub, then push it to selected VPS nodes.
- Watch all stream nodes from one place.
- Check GitHub updates centrally and deploy them to nodes.
- Keep FFmpeg streaming processes independent from panel upgrades.
- Never store server secrets in the repository.

## Quick Start

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
  "id": "racknerd",
  "name": "RACKNERD Istanbul",
  "base_url": "http://100.112.98.95:8787",
  "role": "stream-node"
}
```

Future secure deployment should use a local secret bridge or per-node tokens outside this repo.

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
