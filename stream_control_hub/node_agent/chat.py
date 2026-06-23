"""Chat-plan operations exposed by the VPS node agent."""

from __future__ import annotations

import json
import random
import time

from .settings import CHAT_PLAN_FILE
from .state import CHAT_RUNTIME, CHAT_RUNTIME_LOCK
from .youtube import send_youtube_chat_message, youtube_auth_status

def default_chat_plan() -> dict:
    return {
        "enabled": False,
        "interval_seconds": 300,
        "mode": "loop",
        "messages": [],
    }


def load_chat_plan() -> dict:
    if not CHAT_PLAN_FILE.exists():
        return default_chat_plan()
    try:
        data = json.loads(CHAT_PLAN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return default_chat_plan()
    plan = default_chat_plan()
    plan["enabled"] = bool(data.get("enabled", False))
    plan["interval_seconds"] = max(10, int(data.get("interval_seconds", 300)))
    plan["mode"] = "random" if data.get("mode") == "random" else "loop"
    plan["messages"] = [
        str(x).strip() for x in data.get("messages", []) if str(x).strip()
    ]
    return plan


def save_chat_plan_data(plan: dict) -> dict:
    CHAT_PLAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    normalized = default_chat_plan()
    normalized["enabled"] = bool(plan.get("enabled", False))
    normalized["interval_seconds"] = max(10, int(plan.get("interval_seconds", 300)))
    normalized["mode"] = "random" if plan.get("mode") == "random" else "loop"
    normalized["messages"] = [
        str(x).strip() for x in plan.get("messages", []) if str(x).strip()
    ]
    CHAT_PLAN_FILE.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def next_chat_message(plan: dict) -> str | None:
    messages = plan.get("messages") or []
    if not messages:
        return None
    with CHAT_RUNTIME_LOCK:
        if plan.get("mode") == "random":
            return random.choice(messages)
        CHAT_RUNTIME["last_index"] = (CHAT_RUNTIME["last_index"] + 1) % len(messages)
        return messages[CHAT_RUNTIME["last_index"]]


def update_chat_runtime(**kwargs):
    with CHAT_RUNTIME_LOCK:
        CHAT_RUNTIME.update(kwargs)


def chat_runtime_snapshot() -> dict:
    with CHAT_RUNTIME_LOCK:
        return dict(CHAT_RUNTIME)


def chat_scheduler_loop():
    while True:
        try:
            plan = load_chat_plan()
            auth = youtube_auth_status()
            if not plan.get("enabled"):
                update_chat_runtime(status="disabled")
                time.sleep(5)
                continue
            if not auth.get("authorized"):
                update_chat_runtime(status="auth_required", last_error="还没有完成 Google 授权")
                time.sleep(5)
                continue
            if not plan.get("messages"):
                update_chat_runtime(status="no_messages", last_error="聊天计划里还没有内容")
                time.sleep(5)
                continue

            snapshot = chat_runtime_snapshot()
            interval = max(10, int(plan.get("interval_seconds", 300)))
            wait_left = interval - (time.time() - snapshot.get("last_sent_at", 0.0))
            if snapshot.get("last_sent_at") and wait_left > 0:
                update_chat_runtime(status="waiting")
                time.sleep(min(5, max(1, wait_left)))
                continue

            message = next_chat_message(plan)
            if not message:
                update_chat_runtime(status="no_messages")
                time.sleep(5)
                continue

            result = send_youtube_chat_message(message)
            update_chat_runtime(
                status="sent",
                last_sent_at=time.time(),
                last_message=message,
                last_error="",
            )
            time.sleep(5)
        except Exception as exc:
            update_chat_runtime(status="error", last_error=str(exc))
            time.sleep(10)


__all__ = [
    "chat_runtime_snapshot",
    "chat_scheduler_loop",
    "default_chat_plan",
    "load_chat_plan",
    "save_chat_plan_data",
    "update_chat_runtime",
]
