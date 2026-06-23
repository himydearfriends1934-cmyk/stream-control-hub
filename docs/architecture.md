# Architecture

This repository is organized around two deployable roles.

## Hub Role

`python -m stream_control_hub` starts the local control hub.

The Hub keeps local media, reads `config/nodes.json`, polls node health, and
pushes media to selected nodes through the node Agent API. Deployment planning
lives in `stream_control_hub/deployment.py` and is exposed by Hub API routes
without executing SSH commands directly.

## Agent Role

`python -m stream_control_hub agent` starts the VPS node agent.

The node agent is split into these boundaries:

- `settings.py`: environment-backed settings, filesystem paths, public upload
  policy values, and stream tuning defaults
- `state.py`: process-local locks, caches, transfer counters, upload windows,
  and stream watchdog/adaptive state
- `runtime.py`: auth guards, FFmpeg lifecycle and watchdog loops
- `agent_api.py`: node status endpoint consumed by the Hub and dashboard
- `chat_api.py`: chat-plan endpoints and the browser chat-helper script
- `chat.py`: chat-plan persistence, chat runtime snapshots, and the YouTube chat
  scheduler loop
- `youtube_api.py`: YouTube OAuth client, start, callback, and clear endpoints
- `youtube.py`: YouTube OAuth config/token handling, API client construction,
  active live chat lookup, and message sending
- `upload_api.py`: public upload-window, single upload, chunk upload, probe, and
  delete-video endpoints
- `uploads.py`: public upload-window control, transfer runtime state updates,
  uploaded media file helpers, and chunk-upload bookkeeping
- `stream_api.py`: streaming lifecycle, recommendation, and tuning endpoints
- `dashboard_ui.py`: browser-facing node dashboard routes
- `dashboard_templates.py`: the HTML templates for the node dashboard
- `streaming.py`: stream config/runtime state, metrics, media probing,
  recommendation, tuning, payload normalization, relay status, and adaptive
  state helpers

The first extraction keeps the higher-risk FFmpeg lifecycle implementation
inside `runtime.py`, while settings, mutable state, stream tuning, upload, chat,
and YouTube integration now live in dedicated modules. API callers go through
small route modules and operation surfaces such as `stream_api.py`/`streaming.py`,
`upload_api.py`/`uploads.py`, `chat_api.py`/`chat.py`, and
`youtube_api.py`/`youtube.py`. New changes should land behind those boundaries
first, then move the remaining streaming implementation out of `runtime.py` in
smaller, testable steps.

## Deployment Shape

Run one Hub on a trusted local machine, then run one Agent per VPS. The Hub
should reach Agents over a trusted private network when possible. Public upload
windows are temporary and token-protected, but long-lived secrets still belong
outside the repository.
