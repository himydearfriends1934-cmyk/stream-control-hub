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
