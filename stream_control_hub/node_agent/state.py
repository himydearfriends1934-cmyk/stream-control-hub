"""Process-local mutable state for the VPS stream node agent."""

from __future__ import annotations

import threading
import time

from .settings import (
    STREAM_ADAPTIVE_ENABLED,
    STREAM_ADAPTIVE_SHIFT_CONFIRM_SECONDS,
    STREAM_AUTORESTART_ENABLED,
)


START_TIME = time.time()

LAST_NET_SNAPSHOT = {
    "ts": None,
    "bytes_sent": None,
    "bytes_recv": None,
}

CHAT_RUNTIME = {
    "last_sent_at": 0.0,
    "last_message": "",
    "last_error": "",
    "status": "idle",
    "last_index": -1,
}
CHAT_RUNTIME_LOCK = threading.Lock()

PUBLIC_UPLOAD_STATE = {
    "enabled": False,
    "expires_at": 0.0,
    "last_changed_at": 0.0,
    "last_changed_by": "",
    "last_reason": "initial",
    "active_uploads": 0,
    "token": "",
    "timer": None,
}
PUBLIC_UPLOAD_LOCK = threading.Lock()
UPLOAD_PART_LOCK = threading.Lock()
UPLOAD_COMPLETED: dict[str, dict] = {}

TRANSFER_RUNTIME_LOCK = threading.Lock()
TRANSFER_UPLOADS: dict[str, dict] = {}
TRANSFER_RUNTIME = {
    "last_event_at": 0.0,
    "last_event": "startup",
    "last_error": "",
    "last_route": "",
    "bytes_received_total": 0,
    "chunks_received_total": 0,
    "completed_uploads_total": 0,
    "last_upload": {},
    "last_probe": {},
}

STREAM_LOCK = threading.Lock()
STREAM_ADAPTIVE_LOCK = threading.Lock()
STREAM_TUNING_LOCK = threading.Lock()
STREAM_RUNTIME_LOCK = threading.Lock()
MEDIA_PROBE_LOCK = threading.Lock()
MEDIA_PROBE_CACHE: dict[str, dict] = {}

STREAM_RESTART_STATE = {
    "enabled": STREAM_AUTORESTART_ENABLED,
    "last_restart_at": 0.0,
    "last_exit_at": 0.0,
    "last_error": "",
    "restart_count": 0,
    "next_restart_at": 0.0,
    "last_started_at": 0.0,
    "last_progress_at": 0.0,
    "last_progress_position": None,
    "stall_count": 0,
}

STREAM_ADAPTIVE_STATE = {
    "enabled": STREAM_ADAPTIVE_ENABLED,
    "active": False,
    "status": "idle",
    "last_error": "",
    "last_action": "",
    "last_reason": "",
    "last_evaluated_at": 0.0,
    "last_applied_at": 0.0,
    "cooldown_until": 0.0,
    "pending_direction": "",
    "pending_key": "",
    "pending_since": 0.0,
    "pending_streak": 0,
    "required_streak": 0,
    "required_delay_seconds": STREAM_ADAPTIVE_SHIFT_CONFIRM_SECONDS,
    "current_target": {},
    "recommended_target": {},
    "last_metrics": {},
}
