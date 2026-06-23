"""Environment-backed settings for the VPS stream node agent."""

from __future__ import annotations

import os
import platform
import re
from pathlib import Path


APP_VERSION = "2026.06.22-stability-fix"
PORT = int(os.environ.get("PORT", "8787"))
STREAM_NODE_AGENT_MODE = os.environ.get("STREAM_NODE_AGENT_MODE", "0") != "0"
STREAM_NODE_AGENT_NAME = os.environ.get("STREAM_NODE_AGENT_NAME", platform.node()).strip() or platform.node()
CONTROL_HUB_URL = os.environ.get("CONTROL_HUB_URL", "").strip().rstrip("/")

PID_FILE = Path(os.environ.get("STREAM_PID_FILE", str(Path.home() / "youtube-live.pid")))
STREAM_LOG_FILE = Path(os.environ.get("STREAM_LOG_FILE", "/var/log/stream-control-node-agent/stream.log"))
STREAM_CONFIG_FILE = Path(os.environ.get("STREAM_CONFIG_FILE", "/opt/stream-control-hub/stream_config.json"))
STREAM_TUNING_FILE = Path(os.environ.get("STREAM_TUNING_FILE", "/opt/stream-control-hub/stream_tuning.json"))
STREAM_RUNTIME_FILE = Path(os.environ.get("STREAM_RUNTIME_FILE", "/opt/stream-control-hub/stream_runtime_state.json"))

STREAM_AUTORESTART_ENABLED = os.environ.get("STREAM_AUTORESTART_ENABLED", "1") != "0"
STREAM_AUTORESTART_DELAY_SECONDS = int(os.environ.get("STREAM_AUTORESTART_DELAY_SECONDS", "10"))
STREAM_AUTORESTART_CHECK_SECONDS = int(os.environ.get("STREAM_AUTORESTART_CHECK_SECONDS", "5"))
STREAM_STALL_TIMEOUT_SECONDS = int(os.environ.get("STREAM_STALL_TIMEOUT_SECONDS", "90"))
STREAM_STARTUP_STALL_GRACE_SECONDS = int(os.environ.get("STREAM_STARTUP_STALL_GRACE_SECONDS", "120"))

STREAM_ADAPTIVE_ENABLED = os.environ.get("STREAM_ADAPTIVE_ENABLED", "1") != "0"
STREAM_ADAPTIVE_DOWNSHIFT_STREAK = int(os.environ.get("STREAM_ADAPTIVE_DOWNSHIFT_STREAK", "2"))
STREAM_ADAPTIVE_UPSHIFT_STREAK = int(os.environ.get("STREAM_ADAPTIVE_UPSHIFT_STREAK", "12"))
STREAM_ADAPTIVE_CHANGE_COOLDOWN_SECONDS = int(os.environ.get("STREAM_ADAPTIVE_CHANGE_COOLDOWN_SECONDS", "120"))
STREAM_ADAPTIVE_UPSHIFT_COOLDOWN_SECONDS = int(os.environ.get("STREAM_ADAPTIVE_UPSHIFT_COOLDOWN_SECONDS", "600"))
STREAM_ADAPTIVE_WARMUP_SECONDS = int(os.environ.get("STREAM_ADAPTIVE_WARMUP_SECONDS", "45"))
STREAM_ADAPTIVE_SHIFT_CONFIRM_SECONDS = int(os.environ.get("STREAM_ADAPTIVE_SHIFT_CONFIRM_SECONDS", "5"))

STREAM_FIFO_ENABLED = os.environ.get("STREAM_FIFO_ENABLED", "1") != "0"
STREAM_FIFO_QUEUE_SIZE = int(os.environ.get("STREAM_FIFO_QUEUE_SIZE", "2048"))
STREAM_FIFO_TIMESHIFT_SECONDS = float(os.environ.get("STREAM_FIFO_TIMESHIFT_SECONDS", "12"))
STREAM_FIFO_RECOVERY_WAIT_SECONDS = float(os.environ.get("STREAM_FIFO_RECOVERY_WAIT_SECONDS", "1"))

STREAM_RELAY_ENABLED = os.environ.get("STREAM_RELAY_ENABLED", "0") != "0"
STREAM_RELAY_LOCAL_URL = os.environ.get("STREAM_RELAY_LOCAL_URL", "rtmp://127.0.0.1:1935/live/istanbul").strip()
STREAM_RELAY_HEALTH_TIMEOUT_SECONDS = float(os.environ.get("STREAM_RELAY_HEALTH_TIMEOUT_SECONDS", "0.35"))

TRAFFIC_QUOTA_BYTES = int(os.environ.get("TRAFFIC_QUOTA_TB", "10")) * 1024 ** 4

UPLOAD_DIR = Path(os.environ.get("STREAM_UPLOAD_DIR", "/srv/stream-videos"))
ALLOWED_UPLOAD_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}
UPLOAD_PART_DIR = UPLOAD_DIR / ".upload-parts"
MAX_UPLOAD_CHUNK_BYTES = int(os.environ.get("MAX_UPLOAD_CHUNK_BYTES", str(16 * 1024 ** 2)))
UPLOAD_COMPLETED_TTL_SECONDS = int(os.environ.get("UPLOAD_COMPLETED_TTL_SECONDS", "3600"))
UPLOAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,96}$")

CHAT_PLAN_FILE = Path(os.environ.get("CHAT_PLAN_FILE", "/opt/stream-control-hub/chat_plan.json"))
YOUTUBE_CLIENT_FILE = Path(os.environ.get("YOUTUBE_CLIENT_FILE", "/opt/stream-control-hub/youtube_client.json"))
YOUTUBE_TOKEN_FILE = Path(os.environ.get("YOUTUBE_TOKEN_FILE", "/opt/stream-control-hub/youtube_token.json"))
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

DASHBOARD_SECRET_FILE = Path(os.environ.get("DASHBOARD_SECRET_FILE", "/opt/stream-control-hub/dashboard_secret.key"))
DASHBOARD_PASSWORD_FILE = Path(os.environ.get("DASHBOARD_PASSWORD_FILE", "/opt/stream-control-hub/dashboard_password.txt"))

CANONICAL_PUBLIC_BASE = os.environ.get("CANONICAL_PUBLIC_BASE", "").rstrip("/")
PUBLIC_UPLOAD_PORT = int(os.environ.get("PUBLIC_UPLOAD_PORT", str(PORT)))
PUBLIC_UPLOAD_ORIGIN = os.environ.get("PUBLIC_UPLOAD_ORIGIN", f"http://127.0.0.1:{PUBLIC_UPLOAD_PORT}").rstrip("/")
PUBLIC_UPLOAD_FIREWALL = os.environ.get("PUBLIC_UPLOAD_FIREWALL", "none").strip().lower()
PUBLIC_UPLOAD_FIREWALL_HELPER = os.environ.get("PUBLIC_UPLOAD_FIREWALL_HELPER", "/usr/local/sbin/public-upload-window").strip()
PUBLIC_UPLOAD_INTERFACE = os.environ.get("PUBLIC_UPLOAD_TAILSCALE_INTERFACE", "tailscale0").strip() or "tailscale0"
PUBLIC_UPLOAD_RESTRICT = os.environ.get("PUBLIC_UPLOAD_RESTRICT", "1") != "0"
PUBLIC_UPLOAD_CLOSE_ON_START = os.environ.get("PUBLIC_UPLOAD_CLOSE_ON_START", "1") != "0"
