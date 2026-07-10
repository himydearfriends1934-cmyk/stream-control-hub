from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
import uuid
import hmac
import hashlib
import ipaddress
import threading
import unicodedata
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python always has zoneinfo in supported runtimes.
    ZoneInfo = None

import requests
from flask import Flask, jsonify, make_response, request
from werkzeug.utils import secure_filename

from .youtube_api import YouTubeAPIClient, YouTubeAPIError


ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ROOT / ".env")
CONFIG_DIR = ROOT / "config"
DATA_DIR = Path(os.environ.get("STREAM_HUB_DATA_DIR", str(ROOT / "data")))
MEDIA_DIR = DATA_DIR / "media"
WORK_DIR = DATA_DIR / "work"
NODES_FILE = Path(os.environ.get("STREAM_HUB_NODES_FILE", str(CONFIG_DIR / "nodes.json")))
PORT = int(os.environ.get("STREAM_HUB_PORT", "8788"))
SOURCE_REPO = os.environ.get(
    "STREAM_HUB_SOURCE_REPO",
    "https://github.com/himydearfriends1934-cmyk/stream-control-hub.git",
)
SOURCE_BRANCH = os.environ.get("STREAM_HUB_SOURCE_BRANCH", "main")
ALLOWED_MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}
NODE_UPLOAD_CHUNK_BYTES = int(os.environ.get("STREAM_HUB_NODE_UPLOAD_CHUNK_BYTES", str(8 * 1024 ** 2)))
NODE_PUBLIC_UPLOAD_CHUNK_BYTES = int(os.environ.get("STREAM_HUB_NODE_PUBLIC_UPLOAD_CHUNK_BYTES", str(16 * 1024 ** 2)))
DIRECT_AGENT_UPLOAD_CHUNK_BYTES = int(os.environ.get("STREAM_HUB_DIRECT_AGENT_UPLOAD_CHUNK_BYTES", str(8 * 1024 ** 2)))
NODE_UPLOAD_TIMEOUT_SECONDS = int(os.environ.get("STREAM_HUB_NODE_UPLOAD_TIMEOUT_SECONDS", "300"))
NODE_PUBLIC_UPLOAD_TTL_SECONDS = int(os.environ.get("STREAM_HUB_NODE_PUBLIC_UPLOAD_TTL_SECONDS", "900"))
NODE_UPLOAD_RETRIES = int(os.environ.get("STREAM_HUB_NODE_UPLOAD_RETRIES", "2"))
NODE_UPLOAD_PROBE_BYTES = int(os.environ.get("STREAM_HUB_NODE_UPLOAD_PROBE_BYTES", str(256 * 1024)))
NODE_UPLOAD_PROBE_TIMEOUT_SECONDS = int(os.environ.get("STREAM_HUB_NODE_UPLOAD_PROBE_TIMEOUT_SECONDS", "12"))
MIN_PUBLIC_UPLOAD_BYTES_PER_SECOND = int(os.environ.get("STREAM_HUB_MIN_PUBLIC_UPLOAD_BYTES_PER_SECOND", str(32 * 1024)))
MIN_FREE_AFTER_UPLOAD_BYTES = int(os.environ.get("STREAM_HUB_MIN_FREE_AFTER_UPLOAD_BYTES", str(2 * 1024 ** 3)))
UPLOAD_POLICY_NAME = os.environ.get("STREAM_HUB_UPLOAD_POLICY_NAME", "safe-stable-fast-v1")
PUSH_AUDIT_LOG = DATA_DIR / "push_audit.jsonl"
HUB_SETTINGS_FILE = DATA_DIR / "hub-settings.json"
MEDIA_GROUPS_FILE = DATA_DIR / "media-groups.json"
PUSH_AUDIT_LOG_MAX_BYTES = int(os.environ.get("STREAM_HUB_PUSH_AUDIT_LOG_MAX_BYTES", str(5 * 1024 ** 2)))
CONTROL_TOKEN = os.environ.get("STREAM_HUB_CONTROL_TOKEN", "").strip()
TRUSTED_REMOTE_WRITES = os.environ.get("STREAM_HUB_TRUSTED_REMOTE_WRITES", "").strip().lower() in {"1", "true", "yes"}
HUB_ENV_FILE = ROOT / ".env"
YOUTUBE_CREDENTIAL_FILE = Path(
    os.environ.get("YOUTUBE_CREDENTIAL_FILE", str(DATA_DIR / "youtube_credentials.json"))
)
YOUTUBE_PROFILES_FILE = DATA_DIR / "youtube_profiles.json"
YOUTUBE_USAGE_FILE = DATA_DIR / "youtube_api_usage.json"
YOUTUBE_AUTOTUNE_STATE_FILE = DATA_DIR / "youtube_autotune_state.json"
YOUTUBE_PROFILE_CREDENTIALS_DIR = DATA_DIR / "youtube_profile_credentials"
YOUTUBE_DEFAULT_PROFILE_ID = "default"
YOUTUBE_PROFILE_LOCK = threading.RLock()
YOUTUBE_AUTOTUNE_STOP = threading.Event()
YOUTUBE_AUTOTUNE_LOCK = threading.Lock()
YOUTUBE_CLIENT_CACHE: dict[str, YouTubeAPIClient] = {}


def youtube_quota_day() -> str:
    if ZoneInfo is not None:
        with suppress(Exception):
            return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_youtube_usage() -> dict[str, Any]:
    try:
        payload = json.loads(YOUTUBE_USAGE_FILE.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def save_youtube_usage(payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    YOUTUBE_USAGE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with suppress(OSError):
        YOUTUBE_USAGE_FILE.chmod(0o600)


def record_youtube_api_usage(profile_id: str, method: str, resource: str, units: int) -> None:
    profile_id = safe_youtube_profile_id(profile_id or YOUTUBE_DEFAULT_PROFILE_ID)
    day = youtube_quota_day()
    with YOUTUBE_PROFILE_LOCK:
        usage = load_youtube_usage()
        profiles = usage.setdefault("profiles", {})
        profile_usage = profiles.setdefault(profile_id, {})
        day_usage = profile_usage.setdefault(day, {
            "date": day,
            "calls": 0,
            "estimated_units": 0,
            "by_resource": {},
            "updated_at": "",
        })
        day_usage["calls"] = int(day_usage.get("calls") or 0) + 1
        day_usage["estimated_units"] = int(day_usage.get("estimated_units") or 0) + max(1, int(units or 1))
        key = f"{method.upper()} {resource.lstrip('/')}"
        resource_usage = day_usage.setdefault("by_resource", {}).setdefault(key, {"calls": 0, "estimated_units": 0})
        resource_usage["calls"] = int(resource_usage.get("calls") or 0) + 1
        resource_usage["estimated_units"] = int(resource_usage.get("estimated_units") or 0) + max(1, int(units or 1))
        day_usage["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_youtube_usage(usage)


def youtube_usage_for_profile(profile_id: str) -> dict[str, Any]:
    usage = load_youtube_usage()
    day = youtube_quota_day()
    day_usage = ((usage.get("profiles") or {}).get(profile_id) or {}).get(day) or {}
    return {
        "date": day,
        "calls": int(day_usage.get("calls") or 0),
        "estimated_units": int(day_usage.get("estimated_units") or 0),
        "daily_limit_units": 10000,
        "by_resource": day_usage.get("by_resource") or {},
        "updated_at": str(day_usage.get("updated_at") or ""),
    }


def safe_youtube_profile_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(value or "").strip().lower())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned[:64] or YOUTUBE_DEFAULT_PROFILE_ID


def default_youtube_profile() -> dict[str, Any]:
    return {
        "id": YOUTUBE_DEFAULT_PROFILE_ID,
        "name": "Default YouTube Profile",
        "client_id": os.environ.get("YOUTUBE_CLIENT_ID", ""),
        "client_secret": os.environ.get("YOUTUBE_CLIENT_SECRET", ""),
        "credential_file": str(YOUTUBE_CREDENTIAL_FILE),
        "auto_tune_enabled": False,
        "auto_tune_interval_seconds": 300,
        "auto_tune_cooldown_seconds": 900,
        "auto_tune_min_bitrate": 800,
        "auto_tune_max_bitrate": 6000,
        "created_at": "",
        "updated_at": "",
    }


def load_youtube_profiles_config() -> dict[str, Any]:
    with YOUTUBE_PROFILE_LOCK:
        try:
            payload = json.loads(YOUTUBE_PROFILES_FILE.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        profiles = payload.get("profiles")
        if not isinstance(profiles, list) or not profiles:
            profiles = [default_youtube_profile()]
        normalized = []
        seen: set[str] = set()
        for raw in profiles:
            if not isinstance(raw, dict):
                continue
            item = {**default_youtube_profile(), **raw}
            item["id"] = safe_youtube_profile_id(str(item.get("id") or item.get("name") or YOUTUBE_DEFAULT_PROFILE_ID))
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            if not item.get("credential_file"):
                item["credential_file"] = str(YOUTUBE_PROFILE_CREDENTIALS_DIR / f"{item['id']}.json")
            normalized.append(item)
        if not normalized:
            normalized = [default_youtube_profile()]
        active_id = safe_youtube_profile_id(str(payload.get("active_profile_id") or normalized[0]["id"]))
        if active_id not in {item["id"] for item in normalized}:
            active_id = normalized[0]["id"]
        return {"version": 1, "active_profile_id": active_id, "profiles": normalized}


def save_youtube_profiles_config(payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    YOUTUBE_PROFILE_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    YOUTUBE_PROFILES_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with suppress(OSError):
        YOUTUBE_PROFILES_FILE.chmod(0o600)


def make_youtube_client(profile: dict[str, Any]) -> YouTubeAPIClient:
    profile_id = safe_youtube_profile_id(str(profile.get("id") or YOUTUBE_DEFAULT_PROFILE_ID))
    return YouTubeAPIClient(
        client_id=str(profile.get("client_id") or ""),
        client_secret=str(profile.get("client_secret") or ""),
        credential_path=Path(str(profile.get("credential_file") or YOUTUBE_CREDENTIAL_FILE)),
        quota_recorder=lambda method, resource, units, pid=profile_id: record_youtube_api_usage(pid, method, resource, units),
    )


def public_youtube_profile(profile: dict[str, Any]) -> dict[str, Any]:
    profile_id = safe_youtube_profile_id(str(profile.get("id") or YOUTUBE_DEFAULT_PROFILE_ID))
    client = make_youtube_client(profile)
    status = client.local_status()
    return {
        "id": profile_id,
        "name": str(profile.get("name") or profile_id),
        "client_id": str(profile.get("client_id") or ""),
        "has_client_secret": bool(str(profile.get("client_secret") or "")),
        "configured": bool(status.get("configured")),
        "authorized": bool(status.get("authorized")),
        "authorized_at": str(status.get("authorized_at") or ""),
        "scope": str(status.get("scope") or ""),
        "auto_tune_enabled": bool(profile.get("auto_tune_enabled")),
        "auto_tune_interval_seconds": int(profile.get("auto_tune_interval_seconds") or 300),
        "auto_tune_cooldown_seconds": int(profile.get("auto_tune_cooldown_seconds") or 900),
        "auto_tune_min_bitrate": int(profile.get("auto_tune_min_bitrate") or 800),
        "auto_tune_max_bitrate": int(profile.get("auto_tune_max_bitrate") or 6000),
        "usage": youtube_usage_for_profile(profile_id),
    }


def youtube_profile_by_id(profile_id: str) -> dict[str, Any]:
    config = load_youtube_profiles_config()
    target = safe_youtube_profile_id(profile_id or str(config.get("active_profile_id") or YOUTUBE_DEFAULT_PROFILE_ID))
    for profile in config["profiles"]:
        if profile["id"] == target:
            return profile
    raise YouTubeAPIError("YouTube profile was not found", status_code=404, reason="profile_not_found")


def active_youtube_profile_id() -> str:
    return str(load_youtube_profiles_config().get("active_profile_id") or YOUTUBE_DEFAULT_PROFILE_ID)


def youtube_client_for_id(profile_id: str) -> YouTubeAPIClient:
    target = safe_youtube_profile_id(profile_id or active_youtube_profile_id())
    if target == YOUTUBE_DEFAULT_PROFILE_ID:
        return YOUTUBE_CLIENT
    profile = youtube_profile_by_id(target)
    signature = (
        str(profile.get("client_id") or ""),
        str(profile.get("client_secret") or ""),
        str(profile.get("credential_file") or ""),
    )
    with YOUTUBE_PROFILE_LOCK:
        cached = YOUTUBE_CLIENT_CACHE.get(target)
        if cached is not None and getattr(cached, "_profile_signature", None) == signature:
            return cached
        client = make_youtube_client(profile)
        setattr(client, "_profile_signature", signature)
        YOUTUBE_CLIENT_CACHE[target] = client
        return client


def youtube_client_from_payload(payload: dict[str, Any]) -> tuple[str, YouTubeAPIClient]:
    profile_id = safe_youtube_profile_id(str(payload.get("profile_id") or payload.get("youtube_profile_id") or active_youtube_profile_id()))
    return profile_id, youtube_client_for_id(profile_id)


def load_youtube_autotune_state() -> dict[str, Any]:
    try:
        payload = json.loads(YOUTUBE_AUTOTUNE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def save_youtube_autotune_state(payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    YOUTUBE_AUTOTUNE_STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with suppress(OSError):
        YOUTUBE_AUTOTUNE_STATE_FILE.chmod(0o600)


def youtube_autotune_payload_diff(current: dict[str, Any], recommendation: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("copy_mode", "preset", "audio_bitrate", "fps", "resolution", "keyframe_seconds"):
        if key in recommendation and recommendation.get(key) != current.get(key):
            result[key] = recommendation.get(key)
    if "video_bitrate" in recommendation:
        minimum = max(300, int(profile.get("auto_tune_min_bitrate") or 800))
        maximum = max(minimum, int(profile.get("auto_tune_max_bitrate") or 6000))
        next_bitrate = max(minimum, min(maximum, int(recommendation.get("video_bitrate") or 0)))
        if next_bitrate and next_bitrate != int(current.get("video_bitrate") or 0):
            result["video_bitrate"] = next_bitrate
    return result


def youtube_autotune_tick() -> dict[str, Any]:
    with YOUTUBE_AUTOTUNE_LOCK:
        now = time.time()
        config = load_youtube_profiles_config()
        profiles = {profile["id"]: profile for profile in config["profiles"] if profile.get("auto_tune_enabled")}
        if not profiles:
            return {"ok": True, "skipped": True, "reason": "no enabled profiles"}
        state = load_youtube_autotune_state()
        entries = state.setdefault("entries", {})
        checked = 0
        adjusted = 0
        for node in load_nodes():
            if not node.get("enabled", True):
                continue
            status = request_node_json(node, "/api/status", timeout=8)
            stream = status.get("stream") or {}
            stream_config = status.get("stream_config") or {}
            if not status.get("ok") or not stream.get("running"):
                continue
            if stream_config.get("stream_output_mode") != "youtube_api" or not stream_config.get("youtube_stream_id"):
                continue
            profile_id = safe_youtube_profile_id(str(stream_config.get("youtube_profile_id") or config.get("active_profile_id") or YOUTUBE_DEFAULT_PROFILE_ID))
            profile = profiles.get(profile_id)
            if not profile:
                continue
            interval = max(60, int(profile.get("auto_tune_interval_seconds") or 300))
            cooldown = max(60, int(profile.get("auto_tune_cooldown_seconds") or 900))
            key = f"{node.get('id')}:{profile_id}:{stream_config.get('youtube_stream_id')}"
            entry = entries.setdefault(key, {"consecutive_issues": 0, "last_check": 0, "last_adjusted": 0})
            if now - float(entry.get("last_check") or 0) < interval:
                continue
            entry["last_check"] = now
            checked += 1
            try:
                client = youtube_client_for_id(profile_id)
                if not client.local_status().get("authorized"):
                    entry["last_error"] = "profile is not authorized"
                    continue
                health = client.stream_health(str(stream_config.get("youtube_stream_id") or ""), stream_config)
                recommendation = health.get("recommendation") or {}
                diff = youtube_autotune_payload_diff(stream_config, recommendation, profile)
                severity = str(health.get("severity") or "").lower()
                entry["last_health"] = health.get("health") or {}
                entry["last_analysis"] = health.get("analysis") or {}
                if not diff or severity not in {"warning", "critical"}:
                    entry["consecutive_issues"] = 0
                    entry["last_error"] = ""
                    continue
                entry["consecutive_issues"] = int(entry.get("consecutive_issues") or 0) + 1
                entry["pending_diff"] = diff
                if entry["consecutive_issues"] < 2:
                    continue
                if now - float(entry.get("last_adjusted") or 0) < cooldown:
                    continue
                payload = dict(stream_config)
                payload.update(diff)
                payload["stream_key"] = ""
                payload["youtube_profile_id"] = profile_id
                if not payload.get("youtube_ingestion_url"):
                    payload["youtube_ingestion_url"] = client.ingestion_target(str(payload.get("youtube_stream_id") or ""))
                result = post_node_json(node, "/api/start-stream", payload, timeout=60)
                entry["last_adjusted"] = now
                entry["last_result"] = redacted_stream_result(result)
                if result.get("ok"):
                    adjusted += 1
                    entry["consecutive_issues"] = 0
                    entry["last_error"] = ""
                else:
                    entry["last_error"] = result.get("message") or "auto tune restart failed"
            except Exception as exc:
                entry["last_error"] = str(exc)
        save_youtube_autotune_state(state)
        return {"ok": True, "checked": checked, "adjusted": adjusted}


def youtube_autotune_loop() -> None:
    while not YOUTUBE_AUTOTUNE_STOP.wait(30):
        with suppress(Exception):
            youtube_autotune_tick()


YOUTUBE_CLIENT = YouTubeAPIClient(
    client_id=os.environ.get("YOUTUBE_CLIENT_ID", ""),
    client_secret=os.environ.get("YOUTUBE_CLIENT_SECRET", ""),
    credential_path=YOUTUBE_CREDENTIAL_FILE,
    quota_recorder=lambda method, resource, units: record_youtube_api_usage(YOUTUBE_DEFAULT_PROFILE_ID, method, resource, units),
)
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_HELPER = ROOT / "scripts" / "tailscale-install.sh"
INSTALL_COMMANDS_FILE = CONFIG_DIR / "install-commands.json"
INSTALL_COMMANDS_URL = os.environ.get(
    "STREAM_HUB_INSTALL_COMMANDS_URL",
    "https://raw.githubusercontent.com/himydearfriends1934-cmyk/stream-control-hub/main/config/install-commands.json",
).strip()
SHARE_TASKS: dict[str, dict[str, Any]] = {}
SHARE_TASKS_LOCK = threading.Lock()

APP = Flask(__name__)
APP.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("STREAM_HUB_MAX_UPLOAD_BYTES", str(200 * 1024 ** 3)))


def local_git_version() -> str:
    result = run_command(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], timeout=5)
    return str(result.get("stdout") or "unmanaged").strip() or "unmanaged"


def service_active(name: str) -> bool:
    result = run_command(["systemctl", "is-active", "--quiet", name], timeout=5)
    return bool(result.get("ok"))


HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stream Control Hub</title>
  <style>
    :root {
      --bg: #0c1110;
      --panel: #13201c;
      --panel-2: #192b25;
      --line: #31594c;
      --text: #effdf6;
      --muted: #9fc8b8;
      --accent: #36d399;
      --accent-2: #54c6eb;
      --bad: #fb7185;
      --warn: #fbbf24;
      --body-bg: radial-gradient(circle at 12% 10%, rgba(54, 211, 153, 0.16), transparent 28%), radial-gradient(circle at 88% 0%, rgba(84, 198, 235, 0.14), transparent 24%), linear-gradient(135deg, #08100d, #111917 45%, #090d0c);
      --panel-bg: rgba(19, 32, 28, 0.9);
    }
    :root[data-theme="midnight"] { --bg:#080d18; --panel:#101a2d; --panel-2:#17243b; --line:#304d78; --text:#eef5ff; --muted:#9fb4d2; --accent:#55a7ff; --accent-2:#7ee7ff; --body-bg:radial-gradient(circle at 85% 5%,rgba(85,167,255,.2),transparent 28%),linear-gradient(135deg,#050914,#0d1728 55%,#060b14); --panel-bg:rgba(16,26,45,.92); }
    :root[data-theme="violet"] { --bg:#130b1c; --panel:#25142f; --panel-2:#34203f; --line:#68437a; --text:#fff4ff; --muted:#d1acd9; --accent:#d97cff; --accent-2:#ff9fcf; --body-bg:radial-gradient(circle at 15% 5%,rgba(217,124,255,.2),transparent 30%),linear-gradient(135deg,#100718,#25102c 52%,#0d0713); --panel-bg:rgba(37,20,47,.92); }
    :root[data-theme="light"] { --bg:#edf4f1; --panel:#ffffff; --panel-2:#edf5f2; --line:#9ab8ad; --text:#15251f; --muted:#577268; --accent:#18a873; --accent-2:#2b93bc; --body-bg:linear-gradient(135deg,#e7f2ed,#f8fbfa 48%,#e8f1f6); --panel-bg:rgba(255,255,255,.94); }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--text);
      background: var(--body-bg);
      font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    }
    .wrap { max-width: 1680px; margin: 0 auto; padding: 12px; }
    .hero {
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
      background: var(--panel-bg);
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
    }
    .task-flow {
      margin-top: 10px;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }
    .task-card {
      min-height: 88px;
      display: grid;
      gap: 6px;
      align-content: start;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(19, 32, 28, 0.72);
      color: var(--text);
      text-align: left;
      font-weight: 700;
    }
    .task-card strong { font-size: 14px; }
    .task-card small { color: var(--muted); line-height: 1.35; }
    .task-card:hover { border-color: var(--accent); background: rgba(54,211,153,.08); }
    .flow-status {
      margin-top: 8px;
      min-height: 28px;
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 7px 9px;
      border: 1px dashed rgba(54, 211, 153, 0.46);
      border-radius: 9px;
      color: var(--muted);
      background: rgba(54, 211, 153, 0.06);
      font-size: 12px;
      font-weight: 800;
    }
    .checklist {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }
    .check-step {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: rgba(7, 18, 14, 0.35);
      font-size: 12px;
      font-weight: 800;
    }
    .check-step.done {
      border-color: rgba(54, 211, 153, 0.8);
      color: var(--text);
      background: rgba(54, 211, 153, 0.12);
    }
    .check-step.blocked {
      border-color: rgba(251, 191, 36, 0.68);
      color: #fde68a;
    }
    h1 { margin: 0; font-size: 26px; letter-spacing: 0; }
    p { color: var(--muted); margin: 5px 0 0; line-height: 1.45; }
    .grid { display: grid; grid-template-columns: minmax(620px, 1.05fr) minmax(540px, 0.95fr); gap: 12px; margin-top: 10px; align-items: start; }
    .left-stack, .side-stack { display: grid; gap: 10px; align-content: start; min-width: 0; }
    .grid > .side-stack { align-self: start; grid-template-rows: auto; }
    .media-workspace { grid-column: 1 / -1; display: grid; grid-template-columns: minmax(620px, 1.05fr) minmax(540px, 0.95fr); gap: 12px; align-items: start; }
    .bottom-section { grid-column: 1 / -1; display: grid; grid-template-columns: 0.9fr 0.9fr 1.15fr 1.35fr; gap: 10px; }
    .card {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px;
      background: var(--panel-bg);
      box-shadow: 0 18px 60px rgba(0,0,0,0.18);
    }
    .card h2 { margin: 0 0 8px; font-size: 16px; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; }
    .appearance-controls { display: flex; gap: 8px; align-items: center; justify-content: flex-end; flex-wrap: wrap; }
    .appearance-controls label { color: var(--muted); font-size: 12px; font-weight: 800; }
    .appearance-controls select { padding: 7px 9px; min-width: 110px; }
    .editable-title { display: inline-block; min-width: 220px; padding: 2px 5px; border: 1px dashed transparent; border-radius: 7px; outline: none; }
    .editable-title:hover { border-color: var(--line); }
    .editable-title:focus { border-color: var(--accent); background: rgba(54,211,153,.08); }
    button, input, select, textarea {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      background: var(--panel-2);
      color: var(--text);
      font: inherit;
    }
    textarea { resize: vertical; min-height: 88px; }
    button {
      position: relative;
      cursor: pointer;
      font-weight: 800;
      transition: transform .12s ease, filter .12s ease, box-shadow .12s ease, border-color .12s ease;
    }
    button:hover:not(:disabled) { filter: brightness(1.08); border-color: var(--accent); }
    button:active:not(:disabled), button.is-clicked:not(:disabled) { transform: translateY(1px) scale(0.99); }
    button[data-busy="1"]::after {
      content: "";
      display: inline-block;
      width: 10px;
      height: 10px;
      margin-left: 8px;
      border: 2px solid currentColor;
      border-right-color: transparent;
      border-radius: 50%;
      vertical-align: -1px;
      animation: spin .75s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    button.primary { background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: #04100c; border: none; }
    button.danger { background: #6f1d2d; color: #ffe4ea; }
    button.tiny { padding: 7px 8px; font-size: 12px; border-radius: 8px; white-space: nowrap; }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    .action-hint { margin-top: 6px; color: var(--muted); font-size: 12px; font-weight: 800; }
    .guided-empty {
      display: grid;
      gap: 10px;
      justify-items: center;
      align-content: center;
      min-height: 150px;
      padding: 18px;
      border: 1px dashed var(--line);
      border-radius: 10px;
      text-align: center;
      color: var(--muted);
      background: rgba(7, 18, 14, 0.35);
    }
    .guided-empty strong { color: var(--text); font-size: 16px; }
    .guided-empty .actions { justify-content: center; }
    input[type=file] { width: 100%; }
    .media-list, .log { display: grid; gap: 8px; }
    .node, .media {
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 10px;
      align-items: center;
      padding: 8px;
      border-radius: 10px;
      border: 1px solid rgba(49, 89, 76, 0.8);
      background: rgba(25, 43, 37, 0.78);
    }
    .media-name { min-width: 0; }
    .media-name strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .media-actions { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
    .media-toolbar {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: center;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }
    .media.current-agent { border-color: rgba(54, 211, 153, 0.85); }
    .media-window {
      border: 1px solid rgba(49, 89, 76, 0.85);
      border-radius: 8px;
      max-height: 360px;
      overflow-y: auto;
      overflow-x: hidden;
      background: rgba(7, 18, 14, 0.66);
    }
    .media-window-head,
    .media-file-row {
      display: grid;
      grid-template-columns: minmax(0, 1.7fr) minmax(52px, .55fr) minmax(70px, .72fr) minmax(60px, .65fr) minmax(82px, 1fr) minmax(72px, .82fr);
      gap: 4px;
      align-items: center;
    }
    .media-window-head {
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 7px 9px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
      background: rgba(19, 32, 28, 0.98);
      border-bottom: 1px solid rgba(49, 89, 76, 0.7);
    }
    .media-window-head label { display: grid; gap: 4px; }
    .media-window-head input,
    .media-window-head select { width: 100%; min-width: 0; padding: 5px 6px; border-radius: 7px; font-size: 12px; }
    .media-file-row {
      width: 100%;
      padding: 7px 9px;
      border: 0;
      border-bottom: 1px solid rgba(49, 89, 76, 0.42);
      border-radius: 0;
      background: transparent;
      color: var(--text);
      cursor: pointer;
      text-align: left;
      font-weight: 700;
    }
    .media-file-row:hover,
    .media-file-row.selected {
      background: rgba(54, 211, 153, 0.1);
    }
    .media-file-row.current-agent {
      box-shadow: inset 3px 0 0 rgba(54, 211, 153, 0.9);
    }
    .media-file-row.cleanup-candidate span:first-child { text-decoration: underline dashed; text-underline-offset: 4px; }
    .media-file-row span {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      min-width: 0;
    }
    .media-file-row .muted { color: var(--muted); font-size: 12px; }
    .media-library-tools { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
    .media-library-tools select { min-width: 130px; flex: 1; }
    .quick-group-bar {
      display: grid;
      grid-template-columns: repeat(var(--quick-group-count, 1), minmax(0, 1fr));
      gap: 7px;
      align-items: center;
      min-width: 0;
    }
    .quick-group-bar:empty { display: none; }
    .quick-group-bar button {
      width: 100%;
      min-width: 0;
      padding: 7px 9px;
      border-radius: 999px;
      font-size: 11px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .quick-group-bar button.active { border-color: rgba(54, 211, 153, 0.95); background: rgba(54, 211, 153, 0.14); }
    .resource-tool-row { display: grid; grid-template-columns: 112px minmax(0, 1fr) auto auto; gap: 7px; align-items: center; }
    .quick-group-manage { position: relative; }
    .quick-group-manage-menu { position: absolute; right: 0; top: calc(100% + 5px); z-index: 20; display: flex; gap: 5px; min-width: 142px; padding: 6px; border: 1px solid var(--line); border-radius: 9px; background: var(--panel); box-shadow: 0 12px 28px rgba(0,0,0,.35); }
    .quick-group-manage-menu[hidden] { display: none; }
    .quick-group-manage-menu button { flex: 1; padding: 7px 8px; font-size: 12px; }
    .resource-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
    .resource-header p { margin: 0; font-size: 12px; }
    .upload-stack { display: grid; gap: 12px; align-content: start; min-width: 0; }
    .node-space-card { padding-bottom: 10px; }
    .node-space-card h2 { margin-bottom: 2px; }
    .node-space-card p { margin: 0 0 5px; font-size: 12px; line-height: 1.35; }
    .node-space-rings { display: grid; grid-template-columns: repeat(auto-fit, minmax(104px, 1fr)); grid-auto-rows: 88px; gap: 7px; max-height: 88px; overflow-x: hidden; overflow-y: auto; scrollbar-gutter: stable; }
    .node-space-ring-item { min-width: 0; min-height: 0; height: 88px; display: grid; justify-items: center; align-content: center; gap: 2px; padding: 4px; border: 1px solid var(--line); border-radius: 10px; background: rgba(7, 18, 14, 0.5); text-align: center; cursor: pointer; }
    .node-space-ring-item:hover, .node-space-ring-item.open { border-color: var(--accent); background: rgba(54,211,153,.09); }
    .node-space-ring-item strong { font-size: 11px; line-height: 1.15; }
    .node-space-ring-item strong, .node-space-ring-item small { width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .node-space-ring-item small { color: var(--muted); font-size: 10px; }
    .node-space-ring { width: clamp(48px, 4vw, 54px); height: clamp(48px, 4vw, 54px); display: grid; place-items: center; border-radius: 50%; background: conic-gradient(var(--accent) calc(var(--disk-percent) * 1%), rgba(255,255,255,.09) 0); }
    .node-space-ring::before { content: ""; grid-area: 1 / 1; width: 74%; height: 74%; border-radius: 50%; background: var(--panel); }
    .node-space-ring span { grid-area: 1 / 1; z-index: 1; font-size: 12px; font-weight: 900; }
    .node-space-ring.offline { filter: grayscale(1); opacity: .55; }
    .resource-filter-chip {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: center;
      padding: 7px 9px;
      border: 1px dashed rgba(54, 211, 153, 0.45);
      border-radius: 8px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      background: rgba(54, 211, 153, 0.06);
    }
    .resource-filter-chip button { padding: 5px 7px; font-size: 12px; border-radius: 7px; }
    .disk-grid { display: none; }
    .disk-card { padding: 7px 9px; border: 1px solid var(--line); border-radius: 8px; background: rgba(7,18,14,.5); cursor: pointer; }
    .disk-card:hover, .disk-card.open { border-color: rgba(54, 211, 153, 0.9); background: rgba(54, 211, 153, 0.08); }
    .disk-card-head { display: flex; justify-content: space-between; gap: 8px; font-size: 11px; }
    .disk-bar { height: 8px; margin-top: 6px; overflow: hidden; border-radius: 999px; background: rgba(255,255,255,.08); }
    .disk-bar > span { display: block; height: 100%; background: linear-gradient(90deg,var(--accent),#4cc9f0); }
    .media-context-menu {
      position: fixed;
      z-index: 100;
      min-width: 150px;
      display: none;
      padding: 6px;
      border: 1px solid rgba(49, 89, 76, 0.9);
      border-radius: 8px;
      background: #10201b;
      box-shadow: 0 18px 40px rgba(0,0,0,0.35);
    }
    .media-context-menu.open { display: grid; gap: 4px; }
    .media-context-menu button {
      width: 100%;
      padding: 8px 9px;
      border-radius: 7px;
      text-align: left;
      background: transparent;
      border: 0;
    }
    .media-context-menu button:hover { background: rgba(54, 211, 153, 0.12); }
    .media-context-menu button.danger:hover { background: rgba(251, 113, 133, 0.16); }
    .media-context-label { padding: 7px 9px 3px; color: var(--muted); font-size: 11px; font-weight: 900; border-top: 1px solid var(--line); }
    .media-context-targets { display: grid; gap: 3px; max-height: 190px; overflow: auto; }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      z-index: 90;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 18px;
      background: rgba(1, 7, 5, 0.78);
    }
    .modal-backdrop.open { display: flex; }
    .wizard-modal {
      width: min(920px, 100%);
      max-height: min(760px, calc(100vh - 36px));
      display: grid;
      gap: 12px;
      overflow: auto;
      border: 1px solid rgba(54, 211, 153, 0.45);
      border-radius: 12px;
      padding: 14px;
      background: #0d1a16;
      box-shadow: 0 24px 80px rgba(0,0,0,0.45);
    }
    .wizard-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }
    .wizard-head h2 { margin: 0 0 4px; }
    .wizard-head p { margin: 0; font-size: 13px; }
    .wizard-close { min-width: 42px; padding: 8px 10px; }
    .wizard-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .wizard-role-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
    .wizard-role {
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.025);
    }
    .wizard-role strong { display: block; margin-bottom: 4px; color: #effdf6; }
    .wizard-role small { color: var(--muted); line-height: 1.45; }
    .wizard-role.required { border-color: rgba(52, 211, 153, 0.35); }
    .wizard-existing-grid { display: grid; grid-template-columns: repeat(2, minmax(150px, 1fr)); gap: 10px; align-items: end; }
    .wizard-existing-grid .wide-action { grid-column: 1 / -1; }
    .wizard-field { display: grid; gap: 5px; min-width: 0; }
    .wizard-field label { color: var(--muted); font-size: 12px; font-weight: 900; }
    .youtube-import-panel {
      display: grid;
      gap: 8px;
      margin: 8px 0 10px;
      padding: 10px;
      border: 1px solid rgba(54, 211, 153, .48);
      border-radius: 12px;
      background: linear-gradient(135deg, rgba(54, 211, 153, .12), rgba(84, 198, 235, .08));
    }
    .youtube-import-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }
    .youtube-import-head strong { color: #d6fff0; font-size: 15px; }
    .youtube-import-head small { display: block; margin-top: 2px; color: var(--muted); }
    .youtube-json-file-hidden { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
    .youtube-json-upload-label {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 40px;
      padding: 9px 14px;
      border-radius: 10px;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #04100c;
      font-weight: 900;
      cursor: pointer;
      white-space: nowrap;
    }
    .youtube-import-panel textarea, .oauth-manual-field { display: none; }
    .youtube-control-strip {
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: minmax(245px, 1.7fr) repeat(4, minmax(112px, 1fr));
      gap: 7px;
      align-items: start;
    }
    .youtube-control-item {
      display: grid;
      grid-template-rows: auto auto;
      gap: 3px;
      min-width: 0;
    }
    .youtube-control-item label {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
      line-height: 1.05;
    }
    .youtube-control-item > input,
    .youtube-control-item > select {
      width: 100%;
      min-width: 0;
      min-height: 38px;
      padding: 8px 10px;
      border-radius: 9px;
    }
    .youtube-profile-row,
    .youtube-agent-row {
      grid-column: 1 / -1;
    }
    .youtube-profile-picker {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
    }
    .profile-quick-row {
      --youtube-profile-slots: 6;
      display: flex;
      flex-wrap: nowrap;
      gap: 7px;
      align-items: stretch;
      min-width: 0;
      overflow-x: auto;
      overflow-y: hidden;
      padding-bottom: 2px;
      scrollbar-width: thin;
    }
    .profile-chip {
      flex: 0 0 calc((100% - 35px) / var(--youtube-profile-slots));
      min-width: 128px;
      min-height: 38px;
      padding: 7px 9px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.04);
      color: var(--muted);
      display: flex;
      align-items: center;
      justify-content: center;
      line-height: 1.2;
      overflow-wrap: anywhere;
      white-space: normal;
      text-align: center;
    }
    .profile-chip.active {
      border-color: #ff3b4f;
      color: #fff7f8;
      background: rgba(255,59,79,.22);
      box-shadow: 0 0 0 1px rgba(255,59,79,.3) inset;
    }
    .youtube-profile-actions {
      display: flex;
      gap: 6px;
      align-items: center;
      justify-content: flex-end;
    }
    .profile-stepper {
      width: 30px;
      height: 30px;
      padding: 0;
      border-radius: 999px;
      font-size: 15px;
      line-height: 1;
    }
    .profile-chip.profile-chip-editing { padding: 4px; }
    .profile-chip-input {
      width: 100%;
      height: 30px;
      padding: 4px 7px;
      border-radius: 6px;
      border: 1px solid #ff3b4f;
      color: #fff7f8;
      background: rgba(7,18,14,.72);
      font: inherit;
      font-size: 12px;
      font-weight: 900;
      text-align: center;
      outline: none;
    }
    .node-profile-select {
      width: 100%;
      min-width: 120px;
      padding: 6px 8px;
      border-radius: 8px;
      font-size: 12px;
    }
    .node-profile-label {
      display: block;
      margin-top: 3px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
    }
    .youtube-agent-head { color: var(--muted); font-size: 12px; font-weight: 900; }
    .youtube-agent-list {
      display: flex;
      flex-wrap: nowrap;
      gap: 7px;
      overflow-x: auto;
      overflow-y: hidden;
      padding: 7px;
      border: 1px solid rgba(49, 89, 76, .78);
      border-radius: 10px;
      background: rgba(7, 18, 14, .5);
      scrollbar-width: thin;
    }
    .youtube-agent-card {
      flex: 0 0 calc((100% - 35px) / 6);
      min-width: 150px;
      display: grid;
      gap: 3px;
      padding: 8px 9px;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--text);
      background: rgba(25,43,37,.72);
      text-align: left;
      overflow-wrap: anywhere;
      white-space: normal;
    }
    .youtube-agent-card.active {
      border-color: #ff3b4f;
      background: rgba(255,59,79,.22);
      box-shadow: 0 0 0 1px rgba(255,59,79,.28) inset;
    }
    .youtube-agent-card.streaming:not(.active) { opacity: .5; filter: grayscale(.5); }
    .youtube-agent-card small { color: var(--muted); line-height: 1.25; }
    .youtube-agent-card strong { line-height: 1.25; }
    .youtube-details {
      display: grid;
      gap: 10px;
      padding: 10px;
      border: 1px solid rgba(49, 89, 76, 0.78);
      border-radius: 12px;
      background: rgba(7, 18, 14, 0.58);
    }
    .youtube-details-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .youtube-details-head strong { color: #d6fff0; }
    .youtube-details-head span { color: var(--muted); font-size: 12px; font-weight: 800; }
    .youtube-details-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .youtube-detail-card {
      min-width: 0;
      padding: 10px;
      border: 1px solid rgba(54, 211, 153, .28);
      border-radius: 10px;
      background: rgba(11, 31, 25, .78);
    }
    .youtube-detail-card strong {
      display: block;
      margin-bottom: 6px;
      color: #effdf6;
      overflow-wrap: anywhere;
    }
    .youtube-detail-card dl {
      display: grid;
      grid-template-columns: minmax(94px, .42fr) minmax(0, 1fr);
      gap: 4px 8px;
      margin: 0;
      font-size: 12px;
    }
    .youtube-detail-card dt { color: var(--muted); font-weight: 800; }
    .youtube-detail-card dd { margin: 0; color: #dffcf0; overflow-wrap: anywhere; }
    .youtube-detail-card a { color: #77e7ff; }
    .youtube-detail-empty { color: var(--muted); font-size: 13px; }
    @media (max-width: 760px) { .youtube-details-grid { grid-template-columns: 1fr; } }
    .wizard-step-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .wizard-step {
      display: grid;
      gap: 5px;
      align-content: start;
      min-height: 92px;
      padding: 10px;
      border: 1px solid rgba(49, 89, 76, 0.78);
      border-radius: 10px;
      background: rgba(7, 18, 14, 0.58);
    }
    .wizard-step strong { font-size: 13px; }
    .wizard-step small { color: var(--muted); line-height: 1.35; }
    .wizard-step.done { border-color: rgba(54, 211, 153, 0.9); }
    .wizard-step.fail { border-color: rgba(251, 113, 133, 0.85); }
    .wizard-actions {
      display: grid;
      grid-template-columns: repeat(3, minmax(140px, 1fr)) minmax(96px, .7fr);
      gap: 8px;
      align-items: start;
    }
    .youtube-more-actions {
      position: relative;
      min-width: 0;
    }
    .youtube-more-actions summary {
      min-height: 42px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel-2);
      color: var(--text);
      font-weight: 900;
      cursor: pointer;
      user-select: none;
    }
    .youtube-more-actions summary::-webkit-details-marker { display: none; }
    .youtube-more-actions summary::after {
      content: "v";
      font-size: 11px;
      line-height: 1;
      color: var(--muted);
    }
    .youtube-more-actions[open] summary {
      border-color: var(--accent);
      filter: brightness(1.08);
    }
    .youtube-more-menu {
      position: absolute;
      right: 0;
      top: calc(100% + 6px);
      z-index: 35;
      display: grid;
      gap: 5px;
      width: min(220px, 70vw);
      padding: 7px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: var(--panel);
      box-shadow: 0 12px 28px rgba(0,0,0,.35);
    }
    .youtube-more-menu button {
      width: 100%;
      min-height: 36px;
      padding: 8px 10px;
      text-align: left;
      border-radius: 8px;
    }
    .wizard-status {
      min-height: 128px;
      max-height: 240px;
      overflow: auto;
      display: grid;
      gap: 7px;
      padding: 12px;
      border-radius: 12px;
      border: 1px solid rgba(49, 89, 76, 0.75);
      background: rgba(7, 18, 14, 0.78);
      color: #d6fff0;
      line-height: 1.45;
    }
    .wizard-status-line { color: var(--muted); }
    .wizard-status-line strong { color: #effdf6; }
    .wizard-status-line.fail { color: #fecdd3; }
    .wizard-status-line.done { color: #b7f7dc; }
    .agent-compact,
    .network-compact {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      align-items: center;
      margin: 0;
      padding: 6px 8px;
      border: 1px solid rgba(49, 89, 76, 0.7);
      border-radius: 10px;
      background: rgba(8, 17, 14, 0.38);
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }
    .agent-compact span,
    .network-compact span {
      display: inline-flex;
      min-height: 22px;
      align-items: center;
      padding: 2px 7px;
      border-radius: 999px;
      background: rgba(54, 211, 153, 0.08);
    }
    .agent-compact strong,
    .network-compact strong { color: #d6fff0; }
    .monitor-compact-row { display: grid; grid-template-columns: 0.95fr 1.2fr; gap: 8px; margin-bottom: 8px; }
    .network-compact .compact-title {
      background: transparent;
      color: #d6fff0;
      padding-left: 0;
      font-size: 13px;
    }
    .command-strip {
      margin-top: 10px;
      padding: 10px;
      border-color: rgba(251, 191, 36, 0.45);
      background:
        radial-gradient(circle at 8% 0%, rgba(251, 191, 36, 0.14), transparent 24%),
        rgba(25, 35, 27, 0.95);
    }
    .command-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 8px;
    }
    .command-head h2 { margin: 0 0 3px; font-size: 17px; }
    .command-head p { margin: 0; font-size: 12px; }
    .command-grid {
      display: grid;
      grid-template-columns: minmax(150px, 1fr) minmax(190px, 1.05fr) minmax(250px, 1.35fr) minmax(210px, 1.1fr) 136px;
      gap: 8px;
      align-items: end;
    }
    .command-field { display: grid; gap: 5px; min-width: 0; }
    .command-field label { color: var(--muted); font-size: 12px; font-weight: 800; }
    .command-field input,
    .command-field select { min-width: 0; padding: 8px 10px; }
    .command-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .command-actions {
      display: grid;
      grid-template-columns: 1fr;
      gap: 7px;
      min-width: 0;
    }
    .command-actions button { padding: 8px 9px; }
    .command-advanced {
      grid-column: 1 / -1;
      margin-top: 2px;
      border: 1px solid rgba(49, 89, 76, 0.7);
      border-radius: 10px;
      background: rgba(7, 18, 14, 0.48);
    }
    .command-advanced summary {
      cursor: pointer;
      padding: 8px 10px;
      color: #d6fff0;
      font-size: 12px;
      font-weight: 900;
      list-style-position: inside;
    }
    .command-advanced[open] summary {
      border-bottom: 1px solid rgba(49, 89, 76, 0.55);
    }
    .command-advanced-grid {
      display: grid;
      grid-template-columns: 1.2fr 1fr 1.15fr 1fr 126px;
      gap: 8px;
      padding: 8px;
      align-items: end;
    }
    .tune-output {
      grid-column: 1 / -1;
      min-height: 54px;
      max-height: 96px;
      margin-top: 2px;
    }
    .monitor-card { min-height: 0; }
    .monitor-heading {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }
    .monitor-heading p { margin: 0; font-size: 12px; }
    .node-monitor {
      min-height: 0;
      border-radius: 12px;
      padding: 10px;
      border: 1px solid rgba(54, 211, 153, 0.35);
      background:
        linear-gradient(rgba(54, 211, 153, 0.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(54, 211, 153, 0.035) 1px, transparent 1px),
        radial-gradient(circle at 10% 0%, rgba(54, 211, 153, 0.18), transparent 26%),
        radial-gradient(circle at 100% 12%, rgba(84, 198, 235, 0.12), transparent 24%),
        #07110e;
      background-size: 24px 24px, 24px 24px, auto, auto, auto;
      box-shadow: inset 0 0 42px rgba(54, 211, 153, 0.06);
    }
    .monitor-hero {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: start;
      margin-bottom: 8px;
      padding-bottom: 8px;
      border-bottom: 1px solid rgba(49, 89, 76, 0.55);
    }
    .monitor-hero h3 { margin: 0; font-size: 21px; letter-spacing: 0; }
    .monitor-hero small { color: var(--muted); display: block; margin-top: 3px; }
    .machine-compact {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 6px;
    }
    .machine-compact span {
      display: inline-flex;
      min-height: 20px;
      align-items: center;
      padding: 2px 6px;
      border-radius: 999px;
      background: rgba(54, 211, 153, 0.08);
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }
    .machine-compact strong { color: #d6fff0; }
    .health-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 7px; margin-bottom: 8px; }
    .health-donut {
      display: grid;
      grid-template-columns: 46px minmax(0, 1fr);
      gap: 7px;
      align-items: center;
      padding: 6px;
      border: 1px solid rgba(49, 89, 76, 0.75);
      border-radius: 10px;
      background: rgba(8, 17, 14, 0.38);
      min-width: 0;
    }
    .donut {
      width: 44px;
      height: 44px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background:
        radial-gradient(circle at center, #07110e 0 54%, transparent 55%),
        conic-gradient(var(--donut-color, var(--accent)) var(--value, 0%), rgba(255,255,255,0.08) 0);
      box-shadow: inset 0 0 14px rgba(0,0,0,0.24);
      font-size: 11px;
      font-weight: 900;
    }
    .donut-info small { color: var(--muted); display: block; font-size: 11px; }
    .donut-info strong { display: block; font-size: 14px; line-height: 1.15; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .monitor-panel-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .monitor-panel {
      border: 1px solid rgba(49, 89, 76, 0.75);
      border-radius: 10px;
      padding: 8px;
      background: rgba(9, 17, 14, 0.58);
    }
    .monitor-panel h4 { margin: 0 0 5px; font-size: 13px; color: #d6fff0; }
    .node-table-card { min-height: 0; overflow: hidden; }
    .node-role-split { display: block; height: var(--node-role-split-height, auto); min-height: 330px; font-size: 14px; overflow-y: auto; overflow-x: hidden; padding-right: 3px; }
    .node-role-pane { min-height: 0; display: block; }
    .node-role-pane .node-table { max-height: none; min-height: 0; overflow: visible; padding-right: 0; }
    .node-role-splitter { position: relative; height: 12px; cursor: default; touch-action: none; user-select: none; pointer-events: none; }
    .node-role-splitter::before { content: ""; position: absolute; left: 0; right: 0; top: 5px; height: 2px; border-radius: 2px; background: var(--line); }
    .node-role-splitter:hover::before, .node-role-splitter.dragging::before { height: 4px; top: 4px; background: var(--accent); box-shadow: 0 0 8px rgba(54,211,153,.45); }
    .node-table-toolbar {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      margin-bottom: 8px;
    }
    .node-table-toolbar p { margin: 0; font-size: 13px; }
    .node-table {
      display: grid;
      gap: 6px;
      max-height: 320px;
      overflow: auto;
      padding-right: 3px;
      align-content: start;
    }
    .node-table-head,
    .node-row {
      display: grid;
      grid-template-columns: 22px minmax(150px, 1fr) 76px 82px minmax(260px, 1.05fr);
      gap: 6px;
      align-items: center;
      min-width: 620px;
    }
    .node-table-head {
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 6px 8px;
      color: var(--muted);
      font-size: 12px;
      background: rgba(19, 32, 28, 0.96);
      border-bottom: 1px solid rgba(49, 89, 76, 0.55);
    }
    .node-role-split .node-table-head { position: static; }
    .node-row {
      min-height: 68px;
      padding: 6px 8px;
      border: 1px solid rgba(49, 89, 76, 0.75);
      border-radius: 10px;
      background: rgba(25, 43, 37, 0.72);
      cursor: pointer;
      transition: border-color 0.16s ease, transform 0.16s ease, background 0.16s ease;
      font-size: 14px;
    }
    .node-row:hover {
      border-color: rgba(54, 211, 153, 0.85);
      background: rgba(20, 55, 43, 0.86);
    }
    .node-row.selected,
    .node-row.control-hub {
      border-color: #ff3b4f;
      background: linear-gradient(90deg, rgba(255, 59, 79, .28), rgba(72, 25, 32, .96) 38%, rgba(25, 43, 37, .9));
      box-shadow: inset 5px 0 0 #ff3b4f, 0 0 0 2px rgba(255, 59, 79, .58), 0 0 22px rgba(255, 59, 79, .36);
      transform: translateY(-1px);
    }
    .node-row.selected .node-name strong,
    .node-row.control-hub .node-name strong { color: #ffe8ec; text-shadow: 0 0 10px rgba(255, 59, 79, .55); }
    .node-row.selected .node-state,
    .node-row.control-hub .node-state { color: var(--text); }
    .node-row.offline-node, .node-space-ring-item.offline-node { opacity: .68; }
    .node-row.offline-node:hover, .node-space-ring-item.offline-node:hover { opacity: .9; }
    .node-name { min-width: 0; display: grid; gap: 2px; align-content: center; }
    .node-name strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .node-name small { color: var(--muted); display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .node-note { display: block; justify-self: start; max-width: 9em; padding: 2px 6px; border: 1px dashed var(--line); border-radius: 999px; color: var(--muted); background: transparent; font-size: 11px; line-height: 1.15; font-weight: 700; cursor: pointer; }
    .node-note:hover { color: var(--text); border-color: var(--accent); }
    .node-state { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 800; }
    .dot { width: 14px; height: 14px; flex: 0 0 14px; border: 2px solid rgba(255,255,255,0.2); border-radius: 999px; background: #fbbf24; box-shadow: inset 0 0 3px rgba(255,255,255,0.5), 0 0 10px rgba(251, 191, 36, 0.65); }
    .dot.ok { background: #28e39f; box-shadow: inset 0 0 3px rgba(255,255,255,0.65), 0 0 12px rgba(40, 227, 159, 0.85); }
    .dot.off { background: #52615c; border-color: rgba(255,255,255,0.1); box-shadow: inset 0 0 3px rgba(0,0,0,0.55); }
    .dot.stream-live { background: #ff334f; box-shadow: inset 0 0 3px rgba(255,255,255,0.7), 0 0 13px rgba(255, 51, 79, 0.95); }
    .dot.stream-idle { background: #4a5551; border-color: rgba(255,255,255,0.08); box-shadow: inset 0 0 3px rgba(0,0,0,0.6); }
    .row-actions { display: grid; grid-template-columns: repeat(4, minmax(54px, 1fr)); gap: 4px; align-items: center; }
    .role-row { min-height: 54px; }
    .role-row .row-actions { grid-template-columns: repeat(2, minmax(72px, 1fr)); }
    .role-group + .role-group { margin-top: 12px; }
    .role-group-title { display: flex; justify-content: space-between; align-items: center; margin: 0 0 6px; color: #d6fff0; }
    .role-group-title .role-count { margin-left: 5px; color: var(--accent); font-size: 13px; }
    .role-row.disabled-role { opacity: 0.58; border-style: dashed; }
    .role-row.disabled-role:hover { opacity: 0.82; }
    .row-actions button.tiny { min-width: 54px; padding: 6px 7px; font-size: 12px; overflow: hidden; text-overflow: ellipsis; }
    .settings-button { min-width: 34px !important; font-size: 14px !important; line-height: 1; }
    .role-settings-modal { width: min(520px, 100%); }
    .role-settings-status { display: grid; gap: 8px; }
    .role-settings-item { display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: center; padding: 10px; border: 1px solid var(--line); border-radius: 10px; background: rgba(7, 18, 14, 0.58); }
    .role-settings-item small { display: block; margin-top: 3px; color: var(--muted); }
    .control-transfer-box { display: grid; gap: 8px; margin-top: 10px; padding: 10px; border: 1px dashed var(--line); border-radius: 10px; background: rgba(7,18,14,.42); }
    .control-transfer-box h3 { margin: 0; font-size: 15px; }
    .control-transfer-box input { width: 100%; }
    .choice-modal { width: min(560px, calc(100vw - 28px)); text-align: left; }
    .choice-modal .choice-icon { width: 46px; height: 46px; display: grid; place-items: center; border-radius: 16px; background: rgba(251, 191, 36, .16); color: #ffd166; font-size: 24px; box-shadow: inset 0 0 0 1px rgba(251, 191, 36, .32); }
    .choice-modal .wizard-head { align-items: center; }
    .choice-message { padding: 12px; border: 1px solid rgba(49, 89, 76, .78); border-radius: 12px; background: rgba(7,18,14,.58); color: var(--muted); white-space: pre-line; line-height: 1.55; }
    .choice-actions { display: grid; grid-template-columns: 1fr; gap: 8px; }
    .choice-actions button { width: 100%; min-height: 44px; }
    .choice-actions .danger-soft { border-color: rgba(251,113,133,.55); background: rgba(111,29,45,.7); color: #ffe4ea; }
    .empty-state {
      min-height: 180px;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
      border: 1px dashed rgba(49, 89, 76, 0.8);
      border-radius: 10px;
    }
    .node-detail {
      display: grid;
      grid-template-columns: 1.05fr 1.15fr 0.9fr;
      gap: 12px;
      padding: 14px;
      border-radius: 16px;
      border: 1px solid rgba(49, 89, 76, 0.85);
      background: rgba(25, 43, 37, 0.78);
    }
    .node-title {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      margin-bottom: 10px;
    }
    .node-title strong { display: block; font-size: 18px; }
    .node-title small { color: var(--muted); display: block; margin-top: 3px; }
    .metric-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 5px; }
    .metric {
      border: 1px solid rgba(49, 89, 76, 0.75);
      border-radius: 8px;
      padding: 6px;
      background: rgba(8, 17, 14, 0.35);
    }
    .metric small, .mini-table small { color: var(--muted); display: block; font-size: 12px; }
    .metric strong { display: block; font-size: 17px; margin-top: 2px; }
    .bar {
      height: 8px;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
      overflow: hidden;
      margin-top: 8px;
    }
    .bar > span {
      display: block;
      height: 100%;
      width: var(--value, 0%);
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
    }
    .mini-table { display: grid; gap: 8px; }
    .mini-row {
      display: grid;
      grid-template-columns: 112px minmax(0, 1fr);
      gap: 7px;
      align-items: start;
      padding: 4px 0;
      border-bottom: 1px solid rgba(49, 89, 76, 0.4);
    }
    .mini-row > span { justify-self: stretch; text-align: left; }
    .mini-row:last-child { border-bottom: none; }
    .health-summary {
      display: grid;
      gap: 3px;
      justify-items: start;
      text-align: left;
      font-size: 13px;
      line-height: 1.32;
      font-weight: 500;
    }
    .health-summary strong { color: #d6fff0; font-size: 14px; line-height: 1.25; font-weight: 850; }
    .health-summary ul { margin: 1px 0 0 0; padding-left: 1.05em; color: var(--muted); }
    .health-summary li { margin: 1px 0; }
    .health-summary.bad strong { color: #fecdd3; }
    .mono { font-family: "Cascadia Mono", "Consolas", monospace; word-break: break-word; }
    .compact-card { padding: 10px; }
    .log-card { display: grid; gap: 8px; }
    .log-card pre { min-height: 58px; max-height: 120px; }
    .node strong, .media strong { display: block; }
    .node small, .media small { color: var(--muted); }
    .resource-card, .upload-card { display: grid; gap: 8px; }
    .resource-wide { grid-column: 1 / -1; }
    .upload-card .split { grid-template-columns: 1fr; gap: 8px; }
    .upload-card .actions { display: grid; grid-template-columns: 1fr; }
    .resource-card .media-list { overflow: visible; padding-right: 0; }
    .transfer-box {
      border: 1px solid rgba(49, 89, 76, 0.85);
      border-radius: 8px;
      padding: 8px;
      background: rgba(7, 18, 14, 0.78);
      min-height: 106px;
      display: grid;
      gap: 7px;
    }
    .transfer-title { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-weight: 900; }
    .progress-track { height: 10px; border-radius: 999px; background: rgba(255,255,255,0.1); overflow: hidden; }
    .progress-fill { width: var(--value, 0%); height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent-2)); transition: width 0.25s ease; }
    .transfer-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .transfer-grid small { display: block; color: var(--muted); font-size: 11px; }
    .transfer-grid strong { display: block; margin-top: 2px; font-size: 15px; }
    .transfer-message { color: var(--muted); line-height: 1.45; word-break: break-word; }
    .transfer-box.fail { border-color: rgba(251, 113, 133, 0.75); }
    .transfer-box.done { border-color: rgba(54, 211, 153, 0.9); }
    .pill {
      display: inline-flex;
      padding: 5px 8px;
      border-radius: 999px;
      background: rgba(54, 211, 153, 0.14);
      color: #b7f7dc;
      font-size: 12px;
      font-weight: 800;
    }
    .pill.bad { background: rgba(251, 113, 133, 0.14); color: #fecdd3; }
    .pill.warn { background: rgba(251, 191, 36, 0.15); color: #fde68a; }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      min-height: 90px;
      max-height: 240px;
      overflow: auto;
      padding: 12px;
      border-radius: 12px;
      background: #09110e;
      border: 1px solid var(--line);
      color: #c9f7e7;
    }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    @media (max-width: 760px) { .node-space-rings { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
    @media (max-width: 1080px) {
      .grid, .split, .hero, .task-flow, .node-detail, .bottom-section, .media-workspace, .health-strip, .monitor-panel-grid, .command-grid, .command-advanced-grid, .monitor-compact-row { grid-template-columns: 1fr; }
      .bottom-section { grid-column: auto; }
      .monitor-card, .node-table-card { min-height: auto; }
      .node-monitor { min-height: 420px; }
      .node-table { max-height: none; }
      .node-table-head { display: none; }
      .node-row { grid-template-columns: 24px minmax(0, 1fr); }
      .node-state, .row-actions { grid-column: 2; }
      .wizard-grid, .wizard-role-grid, .wizard-existing-grid, .wizard-step-grid, .wizard-actions { grid-template-columns: 1fr; }
      .youtube-control-strip { grid-template-columns: 1fr; }
      .youtube-agent-card { flex-basis: min(220px, 70vw); }
      .profile-chip { flex-basis: min(190px, 70vw); }
      .youtube-more-menu { position: static; width: 100%; margin-top: 6px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div>
        <h1 class="editable-title" id="editableHubTitle" contenteditable="true" spellcheck="false" title="点击编辑标题">Stream Control Hub</h1>
        <p>本地总控台：集中监控 VPS 推流节点，视频从浏览器直传 Agent，也可以在 Agent 之间共享。升级面板时不触碰正在运行的 FFmpeg 推流。</p>
      </div>
      <div>
        <div class="appearance-controls">
          <label for="themeSelect">界面风格</label>
          <select id="themeSelect">
            <option value="forest">森林绿</option>
            <option value="midnight">深海蓝</option>
            <option value="violet">霓虹紫</option>
            <option value="light">清爽亮色</option>
          </select>
        </div>
        <div class="actions" style="margin-top:8px">
          <button class="primary" id="refreshBtn">刷新状态</button>
          <button id="policyBtn">Upload Policy</button>
          <button id="auditBtn">Push Audit</button>
        </div>
      </div>
    </section>

    <section class="task-flow" aria-label="常用任务入口">
      <button class="task-card" type="button" data-scroll-target=".command-strip">
        <strong>我要开播</strong>
        <small>按顺序选择推流服务器、视频和 YouTube 目标，再一键 Smart Start。</small>
      </button>
      <button class="task-card" type="button" data-scroll-target=".upload-card">
        <strong>我要上传视频</strong>
        <small>先选推流服务器，再把视频直接上传到该服务器。</small>
      </button>
      <button class="task-card" type="button" data-open-connect>
        <strong>我要接入新 VPS</strong>
        <small>输入 Tailscale 100.x 地址，系统自动检测并接入 Agent。</small>
      </button>
      <button class="task-card" type="button" data-scroll-target=".monitor-card">
        <strong>我要看异常</strong>
        <small>查看在线、推流、磁盘、网络和最近操作反馈。</small>
      </button>
    </section>
    <div class="flow-status" id="flowStatus" aria-live="polite">先接入或选择一台推流服务器，然后上传/选择视频即可开播。</div>

    <section class="card command-strip">
      <div class="command-head">
        <div>
          <h2>开播指挥条 / Smart Start</h2>
          <p>和右侧 VPS 节点表联动：先核对目标节点，再选择手动直播码或 YouTube API、选视频、调优、开播。</p>
        </div>
        <span class="pill warn">核对节点后再开播</span>
      </div>
      <div class="checklist" id="smartStartChecklist" aria-live="polite"></div>
      <div class="command-grid">
        <div class="command-field">
          <label>当前控制节点</label>
          <input id="streamNodeInput" type="text" readonly value="选择右侧 VPS 节点">
          <small id="streamNodeHint" class="mono">等待选择节点</small>
        </div>
        <div class="command-field">
          <label>服务器视频</label>
          <select id="streamVideoSelect">
            <option value="">先选择节点...</option>
          </select>
        </div>
        <div class="command-field">
          <label>YouTube 目标</label>
          <div class="command-pair">
            <input id="streamKeyInput" type="password" autocomplete="off" placeholder="手动直播码">
            <select id="youtubeStreamSelect" disabled>
              <option value="">先连接 YouTube API</option>
            </select>
          </div>
        </div>
        <div class="command-field">
          <label>输出 / 自适应</label>
          <div class="command-pair">
            <select id="streamOutputModeInput">
              <option value="direct">直接推 YouTube</option>
              <option value="youtube_api">YouTube API</option>
              <option value="local_relay">本地中继</option>
            </select>
            <select id="adaptiveModeInput">
              <option value="auto">自动调优</option>
              <option value="off">固定参数</option>
            </select>
          </div>
        </div>
        <div class="command-actions">
          <button id="previewTuneBtn">预览调优</button>
          <button class="primary" id="smartStartBtn">Smart Start</button>
        </div>
        <details class="command-advanced" id="commandAdvanced">
          <summary>高级参数 / 调优输出</summary>
          <div class="command-advanced-grid">
            <div class="command-field">
              <label>RTMP 地址</label>
              <input id="streamUrlInput" type="text" value="rtmp://a.rtmp.youtube.com/live2">
            </div>
            <div class="command-field">
              <label>分辨率 / FPS</label>
              <div class="command-pair">
                <input id="resolutionInput" type="text" value="1280x720" placeholder="分辨率">
                <input id="fpsInput" type="number" value="30" min="15" max="60" placeholder="FPS">
              </div>
            </div>
            <div class="command-field">
              <label>码率</label>
              <div class="command-pair">
                <input id="videoBitrateInput" type="number" value="4500" min="800" placeholder="视频 kbps">
                <input id="audioBitrateInput" type="number" value="192" min="64" placeholder="音频 kbps">
              </div>
            </div>
            <div class="command-field">
              <label>编码 / 关键帧</label>
              <div class="command-pair">
                <input id="presetInput" type="text" value="veryfast" placeholder="preset">
                <input id="keyframeInput" type="number" value="2" min="1" max="4" placeholder="关键帧秒">
              </div>
            </div>
            <div class="command-actions">
              <button id="applyTuneBtn">应用推荐</button>
            </div>
            <pre id="tuneBox" class="tune-output">选择右侧节点和服务器视频后，可以预览推荐参数；Smart Start 会停止重复推流并启动一个干净 FFmpeg。</pre>
          </div>
        </details>
      </div>
    </section>

    <section class="grid">
      <div class="card monitor-card">
        <div class="monitor-heading">
          <div>
            <h2>节点监控屏</h2>
            <p>点击右侧 VPS 节点，集中显示健康状态、网络吞吐、推流码率和节点配置。</p>
          </div>
          <span class="pill">live view</span>
        </div>
        <div class="node-monitor" id="nodeMonitor">
          <div class="empty-state">正在读取节点状态...</div>
        </div>
      </div>

      <div class="side-stack">
        <div class="card node-table-card">
          <div class="node-table-toolbar">
            <div>
              <h2>VPS 节点表</h2>
              <p>一屏预留约 10 台：在线、推流、重启推流、重启 VPS。</p>
            </div>
            <span class="pill warn">protected</span>
          </div>
          <div class="node-role-split" id="nodeRoleSplit">
          <div class="role-group node-role-pane">
            <h3 class="role-group-title"><span>Agent 组 <strong class="role-count"><span id="agentNodeCount">0</span> 台</strong></span><small>推流 / 媒体 / Agent 更新</small></h3>
            <div class="node-table" id="nodeList">加载中...</div>
          </div>
          <div class="node-role-splitter" id="nodeRoleSplitter" aria-hidden="true"></div>
          <div class="role-group node-role-pane">
            <h3 class="role-group-title"><span>Hub 组 <strong class="role-count"><span id="hubNodeCount">0</span> 台</strong></span><small>控制台 / Hub 更新 / 切换</small></h3>
            <div class="node-table" id="hubNodeList">加载中...</div>
          </div>
          </div>
        </div>

      </div>

      <div class="media-workspace">
        <div class="card resource-card">
          <div class="resource-header">
            <div>
              <h2>资源管理模块</h2>
              <p>左侧管理全部节点视频：快捷分组、表格筛选、右键属性/移动分组；节点资源条形柜已取消。</p>
            </div>
            <span class="pill">resource table</span>
          </div>
          <div class="resource-tool-row">
            <select id="mediaGroupFilter"><option value="">全部分组</option></select>
            <div class="quick-group-bar" id="quickGroupBar"></div>
            <div class="quick-group-manage">
              <button id="quickGroupManageBtn">分组增减</button>
              <div class="quick-group-manage-menu" id="quickGroupManageMenu" hidden>
                <button id="quickGroupCreateBtn">＋增加</button>
                <button id="quickGroupDeleteBtn">－减少</button>
              </div>
            </div>
            <button id="resourceMoreBtn">其他功能</button>
          </div>
          <div class="disk-grid" id="mediaDiskList" hidden></div>
          <div class="resource-filter-chip" id="resourceFilterChip" hidden></div>
          <div class="media-list" id="mediaList">加载中...</div>
          <div class="media-context-menu" id="mediaContextMenu">
            <button data-media-menu-action="property">属性</button>
            <button data-media-menu-action="inspect">查看详情</button>
            <button data-media-menu-action="use">选用开播</button>
            <button data-media-menu-action="rename">重命名 / 移动</button>
            <button class="danger" data-media-menu-action="delete">删除文件</button>
            <div class="media-context-label">移动到分组</div>
            <div class="media-context-targets" id="mediaGroupTargets"></div>
            <div class="media-context-label">发送到节点</div>
            <div class="media-context-targets" id="mediaSendTargets"></div>
            <div class="media-context-label">移动到节点（成功后删除源文件）</div>
            <div class="media-context-targets" id="mediaMoveTargets"></div>
          </div>
        </div>

        <div class="upload-stack">
          <div class="card node-space-card">
            <h2>节点空间</h2>
            <p>各 Agent 媒体磁盘的已用比例与剩余容量；双击节点可在左侧筛出它的视频。</p>
            <div class="node-space-rings" id="nodeSpaceRings">加载中...</div>
          </div>
          <div class="card upload-card">
            <h2>上传模块</h2>
            <p>上传保持在右侧：先选择目标 Agent，再把视频直传到该节点，可指定初始分组。</p>
            <div class="split">
              <div>
                <input id="mediaInput" type="file" accept=".mp4,.mov,.mkv,.m4v,.webm">
                <select id="uploadGroupInput"><option value="">上传到未分组</option></select>
                <div class="actions" style="margin-top: 8px;">
                  <button class="primary" id="uploadBtn">上传到当前 Agent</button>
                  <button class="danger" id="cancelUploadBtn" disabled>取消上传</button>
                </div>
              </div>
              <div id="uploadBox" class="transfer-box"></div>
            </div>
          </div>
        </div>
      </div>

      <div class="bottom-section">
        <div class="card compact-card">
          <h2>GitHub 更新</h2>
          <p>每天首次打开自动检查；有新版本会弹窗确认。也可复制 GitHub 一键安装/升级命令。</p>
          <div class="actions">
            <button id="checkUpdatesBtn">检查并更新</button>
            <button id="showInstallCommandsBtn">显示安装命令</button>
            <button id="copyHubInstallBtn">复制 Hub 命令</button>
            <button id="copyAgentInstallQuickBtn">复制 Agent 命令</button>
          </div>
        </div>
        <div class="card compact-card">
          <h2>YouTube API</h2>
          <p>Hub 统一保存 YouTube 授权；可上传 Google OAuth JSON，自动填入 Client ID / Secret。</p>
          <div class="actions">
            <button class="primary" id="youtubeWizardBtn">打开 YouTube 向导</button>
            <button id="youtubeImportJsonBtn">上传 API JSON</button>
          </div>
        </div>
        <div class="card compact-card">
          <h2>Agent 快速连接</h2>
          <p>输入目标服务器的 Tailscale IP，自动检测同一 Tailnet、Agent 服务并完成授权。</p>
          <div class="actions">
            <button class="primary" id="tailscaleWizardBtn">连接 Agent</button>
          </div>
        </div>
        <div class="card compact-card log-card">
          <h2>策略 / 审计 / 操作日志</h2>
          <pre id="updateBox">点击 Upload Policy 或 Push Audit 查看系统规则与最近推送记录。</pre>
          <pre id="logBox">就绪。</pre>
        </div>
      </div>
    </section>
  </div>

  <div class="modal-backdrop" id="roleSettingsModal" aria-hidden="true">
    <div class="wizard-modal role-settings-modal" role="dialog" aria-modal="true" aria-labelledby="roleSettingsTitle">
      <div class="wizard-head">
        <div>
          <h2 id="roleSettingsTitle">节点角色设置</h2>
          <p id="roleSettingsSummary">查看当前状态后选择需要执行的低频维护操作。</p>
        </div>
        <button class="wizard-close" id="roleSettingsClose" aria-label="关闭">×</button>
      </div>
      <div class="wizard-existing-grid">
        <div class="wizard-field">
          <label>机器显示名称</label>
          <input id="roleSettingsNameInput" type="text" maxlength="80" placeholder="输入容易识别的名称">
        </div>
        <button id="roleSettingsSaveNameBtn">保存名称</button>
      </div>
      <div class="role-settings-status" id="roleSettingsActions"></div>
      <div class="role-settings-item">
        <span><strong>节点记录</strong><small>删除前会先迁移该节点独有资源；失败则保留节点记录。</small></span>
        <button class="danger" id="roleSettingsDeleteNodeBtn">删除节点</button>
      </div>
      <div class="control-transfer-box">
        <h3>Hub 控制转移 / 换平台</h3>
        <p>把当前 Hub 的节点信息合并导入到新 Hub，新 Hub 会接管这些 Agent / Hub 节点的控制入口。</p>
        <input id="transferHubUrlInput" placeholder="新 Hub 地址，例如 http://100.x.x.x:8788">
        <input id="transferHubTokenInput" placeholder="新 Hub 控制 Token（如果新 Hub 设置了 Token）">
        <button id="transferHubNodesBtn" class="primary">转移当前 Hub 节点信息到新 Hub</button>
        <button id="syncAllHubsBtn">同步节点信息到所有已激活 Hub</button>
      </div>
      <p>保护规则：点击操作后还会显示当前状态与影响范围，必须再次确认才会执行。</p>
    </div>
  </div>

  <div class="modal-backdrop" id="choiceModal" aria-hidden="true">
    <div class="wizard-modal choice-modal" role="dialog" aria-modal="true" aria-labelledby="choiceTitle">
      <div class="wizard-head">
        <div class="choice-icon" id="choiceIcon">!</div>
        <div>
          <h2 id="choiceTitle">确认操作</h2>
          <p id="choiceSubtitle">请选择处理方式。</p>
        </div>
      </div>
      <div class="choice-message" id="choiceMessage"></div>
      <div class="choice-actions" id="choiceActions"></div>
    </div>
  </div>

  <div class="modal-backdrop" id="resourceToolsModal" aria-hidden="true">
    <div class="wizard-modal role-settings-modal" role="dialog" aria-modal="true" aria-labelledby="resourceToolsTitle">
      <div class="wizard-head">
        <div>
          <h2 id="resourceToolsTitle">资源管理 · 其他功能</h2>
          <p>视频清理模块只处理已验证重复副本，永远不会删除最后一份视频。</p>
        </div>
        <button class="wizard-close" id="resourceToolsClose" aria-label="关闭">×</button>
      </div>
      <div class="wizard-existing-grid">
        <div class="wizard-field">
          <label>清理创建时间</label>
          <select id="mediaCleanupAge"><option value="3">创建 3 天前</option><option value="7">创建 7 天前</option><option value="15">创建 15 天前</option><option value="30" selected>创建 30 天前</option><option value="60">创建 60 天前</option><option value="90">创建 90 天前</option></select>
        </div>
        <div class="wizard-field">
          <label>使用记录条件</label>
          <select id="mediaCleanupUsage"><option value="any">不限制使用记录</option><option value="never">从未开播使用</option><option value="unused">超过所选天数未使用</option></select>
        </div>
        <button class="danger wide-action" id="mediaCleanupBtn">预览并清理重复视频</button>
      </div>
      <div class="wizard-status">
        <div class="wizard-status-line"><strong>清理规则</strong>：只删除已通过 SHA-256 验证、保留满 72 小时的重复旧副本。</div>
        <div class="wizard-status-line">单副本视频、未验证副本、正在使用的视频不会被自动删除。</div>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="tailscaleWizardModal" aria-hidden="true">
    <div class="wizard-modal" role="dialog" aria-modal="true" aria-labelledby="tailscaleWizardTitle">
      <div class="wizard-head">
        <div>
          <h2 id="tailscaleWizardTitle">Agent 快速连接</h2>
          <p>输入 Agent 的 Tailscale IP。Hub 会自动完成网络、服务和权限检查。</p>
        </div>
        <button class="wizard-close" id="tailscaleWizardClose" title="关闭">X</button>
      </div>
      <div class="wizard-existing-grid">
        <div class="wizard-field">
          <label>Tailscale IP</label>
          <input id="tailscaleExistingIpInput" type="text" autocomplete="off" placeholder="100.x.x.x">
        </div>
        <button class="primary" id="tailscaleUseExistingIpBtn">检测并连接</button>
      </div>
      <div class="wizard-status">
        <div class="wizard-status-line"><strong>目标 VPS 尚未安装 Agent？</strong></div>
        <pre id="agentInstallCommand">正在从 GitHub 获取最新一键安装命令...</pre>
        <button id="copyAgentInstallBtn">复制一键安装命令</button>
      </div>
      <div class="wizard-status" id="tailscaleWizardLog">
        <div class="wizard-status-line">请输入目标 Agent 的 100.x Tailscale IP。</div>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="youtubeWizardModal" aria-hidden="true">
    <div class="wizard-modal" role="dialog" aria-modal="true" aria-labelledby="youtubeWizardTitle">
      <div class="wizard-head">
        <div>
          <h2 id="youtubeWizardTitle">YouTube Live API</h2>
          <p>授权保存在当前 Hub；Hub 可把 YouTube 直播流分配给一台或多台 Agent。</p>
        </div>
        <button class="wizard-close" id="youtubeWizardClose" title="关闭">X</button>
      </div>
      <div class="youtube-import-panel">
        <div class="youtube-import-head">
          <div>
            <strong>导入 Google OAuth JSON</strong>
            <small>上传 Google 下载的 client_secret_*.json，Hub 会自动读取 OAuth Client ID / Secret。</small>
          </div>
          <label class="youtube-json-upload-label" for="youtubeJsonFileInput">上传 JSON 文件</label>
          <button id="youtubeJsonPickBtn" type="button">选择 JSON</button>
          <input class="youtube-json-file-hidden" id="youtubeJsonFileInput" type="file" accept=".json,application/json">
        </div>
        <textarea id="youtubeJsonInput" spellcheck="false" placeholder="也可以把 Google OAuth client JSON 直接粘贴到这里。"></textarea>
      </div>
      <div class="wizard-grid">
        <div class="youtube-control-strip">
          <div class="wizard-field youtube-control-item">
            <label>API Usage Today（total 10000）</label>
            <input id="youtubeUsageInput" type="text" readonly value="0 calls / 0 units">
          </div>
          <div class="wizard-field youtube-control-item">
            <label>Auto Tune State</label>
            <select id="youtubeAutoTuneEnabledInput">
              <option value="0">Off</option>
              <option value="1">On</option>
            </select>
          </div>
          <div class="wizard-field youtube-control-item">
            <label>Auto Tune Time (s)</label>
            <input id="youtubeAutoTuneIntervalInput" type="number" min="60" max="3600" step="60" value="300" title="Check interval seconds">
          </div>
          <div class="wizard-field youtube-control-item">
            <label>Cooldown (s)</label>
            <input id="youtubeAutoTuneCooldownInput" type="number" min="60" max="7200" step="60" value="900" title="Cooldown seconds">
          </div>
          <div class="wizard-field youtube-control-item">
            <label>Max Kbps</label>
            <input id="youtubeAutoTuneMaxBitrateInput" type="number" min="800" max="30000" step="100" value="6000" title="Max video bitrate Kbps">
          </div>
        </div>
        <div class="wizard-field youtube-profile-row">
          <label>YouTube Profile</label>
          <select id="youtubeProfileSelect" hidden></select>
          <input id="youtubeProfileNameInput" type="hidden" value="">
          <div class="youtube-profile-picker">
            <div class="profile-quick-row" id="youtubeProfileQuickBar"></div>
            <div class="youtube-profile-actions" aria-label="Profile actions">
              <button class="profile-stepper" id="youtubeProfileAddBtn" type="button" title="Add Profile">+</button>
              <button class="profile-stepper" id="youtubeProfileDeleteBtn" type="button" title="Delete current Profile">-</button>
            </div>
          </div>
        </div>
        <div class="wizard-field youtube-agent-row">
          <label class="youtube-agent-head">当前 Agent</label>
          <input id="youtubeNodeInput" type="hidden" value="">
          <div class="youtube-agent-list" id="youtubeAgentList"></div>
        </div>
        <div class="wizard-field">
          <label>已有可复用直播流</label>
          <select id="youtubePrepareStreamSelect">
            <option value="">创建新的可复用直播流</option>
          </select>
        </div>
        <div class="wizard-field">
          <label>直播标题</label>
          <input id="youtubeTitleInput" type="text" maxlength="100" placeholder="直播标题">
        </div>
        <div class="wizard-field oauth-manual-field">
          <label>OAuth Client ID</label>
          <input id="youtubeClientIdInput" type="text" autocomplete="off" placeholder="Google TV / Limited Input Client ID">
        </div>
        <div class="wizard-field oauth-manual-field">
          <label>Client Secret（可选）</label>
          <input id="youtubeClientSecretInput" type="password" autocomplete="off" placeholder="部分 OAuth 客户端没有 secret">
        </div>
        <div class="wizard-field">
          <label>可见范围 / 计划时间</label>
          <div class="command-pair">
            <select id="youtubePrivacyInput">
              <option value="private">私享</option>
              <option value="unlisted">不公开</option>
              <option value="public">公开</option>
            </select>
            <input id="youtubeScheduleInput" type="datetime-local">
          </div>
        </div>
      </div>
      <div class="wizard-actions">
        <button id="youtubeSaveConfigBtn">保存 API 配置</button>
        <button class="primary" id="youtubeAuthorizeBtn">连接 YouTube</button>
        <button id="youtubePrepareBtn">创建并绑定直播</button>
        <details class="youtube-more-actions" id="youtubeMoreActions">
          <summary>更多</summary>
          <div class="youtube-more-menu">
            <button id="youtubeRefreshBtn">检查 / 刷新</button>
            <button id="youtubeHealthBtn">读取健康反馈</button>
            <button class="danger" id="youtubeRevokeBtn">断开授权</button>
          </div>
        </details>
      </div>
      <div class="wizard-status" id="youtubeWizardLog">
        <div class="wizard-status-line">选择 Profile 和 Agent 后检查状态。首次使用只需上传 client_secret_*.json。</div>
      </div>
      <div class="youtube-details" id="youtubeResourceDetails">
        <div class="youtube-details-head">
          <strong>YouTube Studio Details</strong>
          <span>Click refresh to load broadcast and stream settings.</span>
        </div>
        <div class="youtube-detail-empty">No YouTube resources loaded yet.</div>
      </div>
    </div>
  </div>

  <script>
    const TOKEN_FROM_URL = new URLSearchParams(window.location.search).get("token") || "";
    const CONTROL_TOKEN = TOKEN_FROM_URL || sessionStorage.getItem("streamHubControlToken") || localStorage.getItem("streamHubControlToken") || "";
    const CURRENT_ORIGIN = window.location.origin.replace(/\/+$/, "");
    if (TOKEN_FROM_URL) {
      sessionStorage.setItem("streamHubControlToken", TOKEN_FROM_URL);
      const cleanUrl = new URL(window.location.href);
      cleanUrl.searchParams.delete("token");
      window.history.replaceState({}, document.title, cleanUrl.pathname + cleanUrl.search + cleanUrl.hash);
    }
    function authHeaders(extra = {}) {
      return CONTROL_TOKEN ? { ...extra, "X-Control-Token": CONTROL_TOKEN } : extra;
    }

    function sameOriginUrl(value) {
      try {
        return new URL(value, window.location.href).origin.replace(/\/+$/, "") === CURRENT_ORIGIN;
      } catch (_) {
        return false;
      }
    }
    const refs = {
      nodeList: document.getElementById("nodeList"),
      hubNodeList: document.getElementById("hubNodeList"),
      nodeRoleSplit: document.getElementById("nodeRoleSplit"),
      nodeRoleSplitter: document.getElementById("nodeRoleSplitter"),
      agentNodeCount: document.getElementById("agentNodeCount"),
      hubNodeCount: document.getElementById("hubNodeCount"),
      roleSettingsModal: document.getElementById("roleSettingsModal"),
      roleSettingsTitle: document.getElementById("roleSettingsTitle"),
      roleSettingsSummary: document.getElementById("roleSettingsSummary"),
      roleSettingsActions: document.getElementById("roleSettingsActions"),
      roleSettingsNameInput: document.getElementById("roleSettingsNameInput"),
      roleSettingsSaveNameBtn: document.getElementById("roleSettingsSaveNameBtn"),
      roleSettingsDeleteNodeBtn: document.getElementById("roleSettingsDeleteNodeBtn"),
      roleSettingsClose: document.getElementById("roleSettingsClose"),
      choiceModal: document.getElementById("choiceModal"),
      choiceIcon: document.getElementById("choiceIcon"),
      choiceTitle: document.getElementById("choiceTitle"),
      choiceSubtitle: document.getElementById("choiceSubtitle"),
      choiceMessage: document.getElementById("choiceMessage"),
      choiceActions: document.getElementById("choiceActions"),
      transferHubUrlInput: document.getElementById("transferHubUrlInput"),
      transferHubTokenInput: document.getElementById("transferHubTokenInput"),
      transferHubNodesBtn: document.getElementById("transferHubNodesBtn"),
      syncAllHubsBtn: document.getElementById("syncAllHubsBtn"),
      editableHubTitle: document.getElementById("editableHubTitle"),
      themeSelect: document.getElementById("themeSelect"),
      flowStatus: document.getElementById("flowStatus"),
      smartStartChecklist: document.getElementById("smartStartChecklist"),
      nodeMonitor: document.getElementById("nodeMonitor"),
      mediaList: document.getElementById("mediaList"),
      nodeSpaceRings: document.getElementById("nodeSpaceRings"),
      mediaContextMenu: document.getElementById("mediaContextMenu"),
      mediaSendTargets: document.getElementById("mediaSendTargets"),
      mediaMoveTargets: document.getElementById("mediaMoveTargets"),
      mediaGroupTargets: document.getElementById("mediaGroupTargets"),
      mediaGroupFilter: document.getElementById("mediaGroupFilter"),
      quickGroupBar: document.getElementById("quickGroupBar"),
      quickGroupManageBtn: document.getElementById("quickGroupManageBtn"),
      quickGroupManageMenu: document.getElementById("quickGroupManageMenu"),
      quickGroupCreateBtn: document.getElementById("quickGroupCreateBtn"),
      quickGroupDeleteBtn: document.getElementById("quickGroupDeleteBtn"),
      resourceMoreBtn: document.getElementById("resourceMoreBtn"),
      resourceToolsModal: document.getElementById("resourceToolsModal"),
      resourceToolsClose: document.getElementById("resourceToolsClose"),
      mediaGroupAddBtn: document.getElementById("mediaGroupAddBtn"),
      mediaGroupRenameBtn: document.getElementById("mediaGroupRenameBtn"),
      mediaGroupDeleteBtn: document.getElementById("mediaGroupDeleteBtn"),
      mediaAssignGroupBtn: document.getElementById("mediaAssignGroupBtn"),
      mediaCleanupAge: document.getElementById("mediaCleanupAge"),
      mediaCleanupUsage: document.getElementById("mediaCleanupUsage"),
      mediaCleanupBtn: document.getElementById("mediaCleanupBtn"),
      uploadGroupInput: document.getElementById("uploadGroupInput"),
      mediaDiskList: document.getElementById("mediaDiskList"),
      resourceFilterChip: document.getElementById("resourceFilterChip"),
      refreshBtn: document.getElementById("refreshBtn"),
      checkUpdatesBtn: document.getElementById("checkUpdatesBtn"),
      showInstallCommandsBtn: document.getElementById("showInstallCommandsBtn"),
      copyHubInstallBtn: document.getElementById("copyHubInstallBtn"),
      copyAgentInstallQuickBtn: document.getElementById("copyAgentInstallQuickBtn"),
      policyBtn: document.getElementById("policyBtn"),
      auditBtn: document.getElementById("auditBtn"),
      tailscaleWizardBtn: document.getElementById("tailscaleWizardBtn"),
      tailscaleWizardModal: document.getElementById("tailscaleWizardModal"),
      tailscaleWizardClose: document.getElementById("tailscaleWizardClose"),
      tailscaleWizardLog: document.getElementById("tailscaleWizardLog"),
      tailscaleExistingIpInput: document.getElementById("tailscaleExistingIpInput"),
      tailscaleUseExistingIpBtn: document.getElementById("tailscaleUseExistingIpBtn"),
      agentInstallCommand: document.getElementById("agentInstallCommand"),
      copyAgentInstallBtn: document.getElementById("copyAgentInstallBtn"),
      mediaInput: document.getElementById("mediaInput"),
      uploadBtn: document.getElementById("uploadBtn"),
      pushSelectedBtn: document.getElementById("pushSelectedBtn"),
      streamNodeInput: document.getElementById("streamNodeInput"),
      streamNodeHint: document.getElementById("streamNodeHint"),
      streamVideoSelect: document.getElementById("streamVideoSelect"),
      streamKeyInput: document.getElementById("streamKeyInput"),
      youtubeStreamSelect: document.getElementById("youtubeStreamSelect"),
      streamUrlInput: document.getElementById("streamUrlInput"),
      streamOutputModeInput: document.getElementById("streamOutputModeInput"),
      adaptiveModeInput: document.getElementById("adaptiveModeInput"),
      resolutionInput: document.getElementById("resolutionInput"),
      fpsInput: document.getElementById("fpsInput"),
      videoBitrateInput: document.getElementById("videoBitrateInput"),
      audioBitrateInput: document.getElementById("audioBitrateInput"),
      presetInput: document.getElementById("presetInput"),
      keyframeInput: document.getElementById("keyframeInput"),
      previewTuneBtn: document.getElementById("previewTuneBtn"),
      smartStartBtn: document.getElementById("smartStartBtn"),
      applyTuneBtn: document.getElementById("applyTuneBtn"),
      commandAdvanced: document.getElementById("commandAdvanced"),
      tuneBox: document.getElementById("tuneBox"),
      youtubeWizardBtn: document.getElementById("youtubeWizardBtn"),
      youtubeImportJsonBtn: document.getElementById("youtubeImportJsonBtn"),
      youtubeWizardModal: document.getElementById("youtubeWizardModal"),
      youtubeWizardClose: document.getElementById("youtubeWizardClose"),
      youtubeWizardLog: document.getElementById("youtubeWizardLog"),
      youtubeResourceDetails: document.getElementById("youtubeResourceDetails"),
      youtubeProfileSelect: document.getElementById("youtubeProfileSelect"),
      youtubeProfileQuickBar: document.getElementById("youtubeProfileQuickBar"),
      youtubeProfileNameInput: document.getElementById("youtubeProfileNameInput"),
      youtubeUsageInput: document.getElementById("youtubeUsageInput"),
      youtubeProfileAddBtn: document.getElementById("youtubeProfileAddBtn"),
      youtubeProfileDeleteBtn: document.getElementById("youtubeProfileDeleteBtn"),
      youtubeAutoTuneEnabledInput: document.getElementById("youtubeAutoTuneEnabledInput"),
      youtubeAutoTuneIntervalInput: document.getElementById("youtubeAutoTuneIntervalInput"),
      youtubeAutoTuneCooldownInput: document.getElementById("youtubeAutoTuneCooldownInput"),
      youtubeAutoTuneMaxBitrateInput: document.getElementById("youtubeAutoTuneMaxBitrateInput"),
      youtubeNodeInput: document.getElementById("youtubeNodeInput"),
      youtubeAgentList: document.getElementById("youtubeAgentList"),
      youtubePrepareStreamSelect: document.getElementById("youtubePrepareStreamSelect"),
      youtubeTitleInput: document.getElementById("youtubeTitleInput"),
      youtubeClientIdInput: document.getElementById("youtubeClientIdInput"),
      youtubeClientSecretInput: document.getElementById("youtubeClientSecretInput"),
      youtubeJsonPickBtn: document.getElementById("youtubeJsonPickBtn"),
      youtubeJsonFileInput: document.getElementById("youtubeJsonFileInput"),
      youtubeJsonInput: document.getElementById("youtubeJsonInput"),
      youtubePrivacyInput: document.getElementById("youtubePrivacyInput"),
      youtubeScheduleInput: document.getElementById("youtubeScheduleInput"),
      youtubeRefreshBtn: document.getElementById("youtubeRefreshBtn"),
      youtubeSaveConfigBtn: document.getElementById("youtubeSaveConfigBtn"),
      youtubeAuthorizeBtn: document.getElementById("youtubeAuthorizeBtn"),
      youtubePrepareBtn: document.getElementById("youtubePrepareBtn"),
      youtubeHealthBtn: document.getElementById("youtubeHealthBtn"),
      youtubeRevokeBtn: document.getElementById("youtubeRevokeBtn"),
      youtubeMoreActions: document.getElementById("youtubeMoreActions"),
      updateBox: document.getElementById("updateBox"),
      uploadBox: document.getElementById("uploadBox"),
      logBox: document.getElementById("logBox"),
    };
    refs.cancelUploadBtn = document.getElementById("cancelUploadBtn");
    let nodes = [];
    let mediaLibrary = { groups: [], resources: [], nodes: [] };
    let openResourceNodeId = "";
    let resourceTableFilters = { name: "", size: "", age: "", group: "", copyNode: "", ownerNode: "" };
    let resourceNameFilterTimer = null;
    const QUICK_GROUP_LIMIT = 6;
    const LAST_NODE_STORAGE_KEY = "streamHubLastSelectedNodeId";
    let selectedNodeId = localStorage.getItem(LAST_NODE_STORAGE_KEY) || "";
    function rememberSelectedNode(nodeId) {
      selectedNodeId = String(nodeId || "");
      if (selectedNodeId) localStorage.setItem(LAST_NODE_STORAGE_KEY, selectedNodeId);
      else localStorage.removeItem(LAST_NODE_STORAGE_KEY);
    }
    let lastTuneRecommendation = null;
    let activeUpload = null;
    let contextMediaRow = null;
    let youtubeOauthSession = "";
    let youtubeOauthPollTimer = null;
    let youtubeProfiles = [];
    let activeYouTubeProfileId = "default";
    const YOUTUBE_PROFILE_VISIBLE_SLOTS = 6;
    let editingYouTubeProfileId = "";

    const HUB_HEIGHT_STORAGE_KEY = "streamHubHubPanelHeight";
    const HUB_PANEL_MIN_HEIGHT = 96;
    const AGENT_PANEL_MIN_HEIGHT = 120;
    const NODE_SPLIT_MIN_HEIGHT = 330;

    function uiMessage(message) {
      if (refs.flowStatus) refs.flowStatus.textContent = message;
    }

    function flashButton(button) {
      if (!button || button.disabled) return;
      button.classList.remove("is-clicked");
      void button.offsetWidth;
      button.classList.add("is-clicked");
      window.setTimeout(() => button.classList.remove("is-clicked"), 180);
    }

    function setButtonReady(button, ready, reason = "") {
      if (!button || button.dataset.busy === "1") return;
      button.disabled = !ready;
      button.title = ready ? "" : reason;
      button.dataset.disabledReason = ready ? "" : reason;
    }

    function prerequisiteSnapshot() {
      const node = selectedNode();
      const hasNode = Boolean(node?.id);
      const mediaOption = refs.streamVideoSelect?.selectedOptions?.[0];
      const hasVideo = Boolean(refs.streamVideoSelect?.value || mediaOption?.dataset?.libraryName);
      const mode = refs.streamOutputModeInput?.value || "direct";
      const youtubeApiMode = mode === "youtube_api";
      const relayMode = mode === "local_relay";
      const hasTarget = relayMode || (youtubeApiMode ? Boolean(refs.youtubeStreamSelect?.value) : Boolean(refs.streamKeyInput?.value.trim()));
      const hasUploadFile = Boolean(refs.mediaInput?.files?.[0]);
      const nodeReady = hasNode && node?.enabled !== false;
      return { node, hasNode, nodeReady, hasVideo, mode, youtubeApiMode, relayMode, hasTarget, hasUploadFile };
    }

    function renderChecklist(state) {
      if (!refs.smartStartChecklist) return;
      const targetLabel = state.relayMode ? "本地中继" : state.youtubeApiMode ? "YouTube API 直播流" : "手动直播码";
      const steps = [
        { label: "推流服务器", done: state.nodeReady, reason: "先接入或选择一台推流服务器" },
        { label: "服务器视频", done: state.hasVideo, reason: "先上传或选择一个视频" },
        { label: targetLabel, done: state.hasTarget, reason: state.youtubeApiMode ? "先连接 YouTube 并选择直播流" : "填写直播码或切换到本地中继" },
      ];
      refs.smartStartChecklist.innerHTML = steps.map((step) => `
        <span class="check-step ${step.done ? "done" : "blocked"}" title="${escapeHtml(step.done ? "已完成" : step.reason)}">
          ${step.done ? "✓" : "·"} ${escapeHtml(step.label)}
        </span>
      `).join("");
    }

    function updatePrimaryActionStates() {
      const state = prerequisiteSnapshot();
      renderChecklist(state);
      const missing = [];
      if (!state.nodeReady) missing.push("选择推流服务器");
      if (!state.hasVideo) missing.push("选择视频");
      if (!state.hasTarget) missing.push(state.youtubeApiMode ? "选择 YouTube 直播流" : state.relayMode ? "" : "填写直播码");
      const smartReady = state.nodeReady && state.hasVideo && state.hasTarget;
      const previewReady = state.nodeReady && state.hasVideo;
      const uploadReady = state.nodeReady && state.hasUploadFile && !activeUpload;
      setButtonReady(refs.previewTuneBtn, previewReady, previewReady ? "" : "先选择推流服务器和视频");
      setButtonReady(refs.smartStartBtn, smartReady, smartReady ? "" : `还缺少：${missing.filter(Boolean).join("、")}`);
      setButtonReady(refs.uploadBtn, uploadReady, uploadReady ? "" : state.nodeReady ? "先选择一个视频文件" : "先选择推流服务器");
      if (!nodes.length) {
        uiMessage("还没有推流服务器。点击“我要接入新 VPS”，输入 Tailscale 100.x 地址即可接入。");
      } else if (!state.nodeReady) {
        uiMessage("请选择一台在线推流服务器。");
      } else if (!state.hasVideo) {
        uiMessage(`已选择 ${state.node?.name || state.node?.id}。下一步：上传视频或从资源管理里选一个视频。`);
      } else if (!state.hasTarget) {
        uiMessage(state.youtubeApiMode ? "已选择视频。下一步：连接 YouTube API 并选择直播流。" : "已选择视频。下一步：填写直播码，或切换为本地中继。");
      } else {
        uiMessage("开播条件已就绪，可以点击 Smart Start。");
      }
    }

    function scrollToSelector(selector) {
      document.querySelector(selector)?.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function naturalRolePaneHeight(pane) {
      if (!pane) return 0;
      const title = pane.querySelector(".role-group-title");
      const table = pane.querySelector(".node-table");
      const styles = getComputedStyle(pane);
      const padding = parseFloat(styles.paddingTop) + parseFloat(styles.paddingBottom);
      const titleStyles = title ? getComputedStyle(title) : null;
      const titleMargins = titleStyles ? parseFloat(titleStyles.marginTop) + parseFloat(titleStyles.marginBottom) : 0;
      return Math.ceil((title?.offsetHeight || 0) + titleMargins + (table?.scrollHeight || 0) + padding + 8);
    }

    function syncNodeRoleSplitHeight() {
      const split = refs.nodeRoleSplit;
      if (!split) return;
      if (window.matchMedia("(max-width: 1080px)").matches) {
        split.style.removeProperty("--node-role-split-height");
        clampHubPanelHeight();
        return;
      }
      const panes = split.querySelectorAll(".node-role-pane");
      const splitterHeight = refs.nodeRoleSplitter?.offsetHeight || 12;
      const naturalHeight = naturalRolePaneHeight(panes[0]) + splitterHeight + naturalRolePaneHeight(panes[1]);
      const splitTop = split.getBoundingClientRect().top;
      const monitorBottom = document.querySelector(".monitor-card")?.getBoundingClientRect().bottom || 0;
      const viewportBottom = window.innerHeight - 14;
      const rowBottom = monitorBottom > splitTop ? monitorBottom : viewportBottom;
      const availableHeight = Math.max(NODE_SPLIT_MIN_HEIGHT, Math.floor(rowBottom - splitTop));
      const targetHeight = Math.max(NODE_SPLIT_MIN_HEIGHT, Math.min(naturalHeight, availableHeight));
      split.style.setProperty("--node-role-split-height", `${targetHeight}px`);
      clampHubPanelHeight();
    }

    function setHubPanelHeight(height) {
      const split = refs.nodeRoleSplit;
      if (!split) return;
      const available = Math.max(HUB_PANEL_MIN_HEIGHT + AGENT_PANEL_MIN_HEIGHT, split.getBoundingClientRect().height - 12);
      const value = Math.max(HUB_PANEL_MIN_HEIGHT, Math.min(available - AGENT_PANEL_MIN_HEIGHT, Number(height) || 150));
      split.style.setProperty("--hub-panel-height", `${value}px`);
      refs.nodeRoleSplitter?.setAttribute("aria-valuenow", String(Math.round(value)));
      refs.nodeRoleSplitter?.setAttribute("aria-valuemin", String(HUB_PANEL_MIN_HEIGHT));
      refs.nodeRoleSplitter?.setAttribute("aria-valuemax", String(Math.round(available - AGENT_PANEL_MIN_HEIGHT)));
      localStorage.setItem(HUB_HEIGHT_STORAGE_KEY, String(Math.round(value)));
    }

    function clampHubPanelHeight() {
      const current = parseFloat(getComputedStyle(refs.nodeRoleSplit).getPropertyValue("--hub-panel-height"))
        || Number(localStorage.getItem(HUB_HEIGHT_STORAGE_KEY))
        || 150;
      setHubPanelHeight(current);
    }

    function initNodeRoleSplitter() {
      if (!refs.nodeRoleSplitter || !refs.nodeRoleSplit) return;
      setHubPanelHeight(localStorage.getItem(HUB_HEIGHT_STORAGE_KEY) || 150);
      let dragging = false;
      const move = (event) => {
        if (!dragging) return;
        const bounds = refs.nodeRoleSplit.getBoundingClientRect();
        setHubPanelHeight(bounds.bottom - event.clientY);
      };
      const stop = () => {
        dragging = false;
        refs.nodeRoleSplitter.classList.remove("dragging");
        document.body.style.cursor = "";
      };
      refs.nodeRoleSplitter.addEventListener("pointerdown", (event) => {
        dragging = true;
        refs.nodeRoleSplitter.classList.add("dragging");
        refs.nodeRoleSplitter.setPointerCapture(event.pointerId);
        document.body.style.cursor = "ns-resize";
        event.preventDefault();
      });
      refs.nodeRoleSplitter.addEventListener("pointermove", move);
      refs.nodeRoleSplitter.addEventListener("pointerup", stop);
      refs.nodeRoleSplitter.addEventListener("pointercancel", stop);
      refs.nodeRoleSplitter.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
        const current = parseFloat(getComputedStyle(refs.nodeRoleSplit).getPropertyValue("--hub-panel-height")) || 150;
        setHubPanelHeight(current + (event.key === "ArrowUp" ? 20 : -20));
        event.preventDefault();
      });
      window.addEventListener("resize", syncNodeRoleSplitHeight);
    }

    renderTransfer({
      title: "传输状态",
      badge: "ready",
      message: "选择右侧当前 Agent 后，文件会从浏览器直接传到该 Agent；共享时由源 Agent 直接复制到目标 Agent。",
    });

    function selectedNodeIds() {
      return [...document.querySelectorAll("[data-node-check]:checked")].map((el) => el.value);
    }

    function selectedMediaName() {
      const checked = document.querySelector("[data-media-check]:checked");
      return checked ? checked.value : "";
    }

    function selectedMediaPath() {
      const checked = document.querySelector("[data-media-check]:checked");
      return checked ? (checked.dataset.videoPath || checked.value) : "";
    }

    function selectedMediaNodeId() {
      const checked = document.querySelector("[data-media-check]:checked");
      return checked ? (checked.dataset.nodeId || "") : "";
    }

    function log(message) {
      refs.logBox.textContent = `${new Date().toLocaleTimeString()} ${message}\n${refs.logBox.textContent}`.trim();
    }

    function nodeStatusPill(node) {
      if (node.enabled === false) return `<span class="pill warn">disabled</span>`;
      if (!node.health?.ok) return `<span class="pill bad">offline</span>`;
      return `<span class="pill">online</span>`;
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function stateDot(ok, warn = false) {
      return `<span class="dot ${ok ? "ok" : warn ? "" : "off"}"></span>`;
    }

    function streamDot(streaming) {
      return `<span class="dot ${streaming ? "stream-live" : "stream-idle"}"></span>`;
    }

    function fmtBytes(bytes) {
      const units = ["B", "KB", "MB", "GB", "TB"];
      let value = Number(bytes || 0);
      let index = 0;
      while (value >= 1024 && index < units.length - 1) {
        value /= 1024;
        index += 1;
      }
      return index ? `${value.toFixed(1)} ${units[index]}` : `${Math.round(value)} B`;
    }

    function fmtRate(bytesPerSecond) {
      return `${fmtBytes(bytesPerSecond)}/s`;
    }

    function fmtDuration(seconds) {
      const value = Math.max(0, Number(seconds || 0));
      if (!Number.isFinite(value) || value <= 0) return "--";
      if (value < 60) return `${Math.ceil(value)} 秒`;
      const minutes = Math.floor(value / 60);
      const rest = Math.ceil(value % 60);
      if (minutes < 60) return `${minutes} 分 ${rest} 秒`;
      const hours = Math.floor(minutes / 60);
      return `${hours} 小时 ${minutes % 60} 分`;
    }

    function friendlyError(error, fallback = "操作失败") {
      if (!error) return fallback;
      const message = String(error.message || error.messageText || error || fallback);
      if (message.includes("Failed to fetch")) return "网络连接失败：浏览器无法连接到目标 Agent，请检查 Tailscale、Agent 服务和端口。";
      if (message.includes("cross-origin")) return "Hub 写入被跨域保护拦截，请刷新页面或确认通过 Tailscale/内网地址访问。";
      if (message.includes("unsupported media")) return "文件格式不支持，请使用 mp4、mov、mkv、m4v 或 webm。";
      if (message.includes("already exists")) return "目标文件名已经存在，请换一个名称。";
      return message;
    }

    function renderTransfer(state = {}) {
      const status = state.status || "idle";
      const percent = pct(state.percent || 0);
      const boxClass = status === "failed" ? "fail" : status === "done" ? "done" : "";
      refs.uploadBox.className = `transfer-box ${boxClass}`;
      refs.uploadBox.innerHTML = `
        <div class="transfer-title">
          <span>${escapeHtml(state.title || "传输状态")}</span>
          <span class="pill ${status === "failed" ? "bad" : status === "done" ? "" : "warn"}">${escapeHtml(state.badge || status)}</span>
        </div>
        <div class="progress-track"><div class="progress-fill" style="--value:${percent}%"></div></div>
        <div class="transfer-grid">
          <div><small>复制源 / 上传源</small><strong>${escapeHtml(state.source || "本机浏览器")}</strong></div>
          <div><small>目标节点</small><strong>${escapeHtml(state.target || "--")}</strong></div>
          <div><small>传输线路</small><strong>${escapeHtml(state.routeLabel || "公网直连")}</strong></div>
          <div><small>进度</small><strong>${Math.round(percent)}%</strong></div>
          <div><small>已传 / 总量</small><strong>${escapeHtml(fmtBytes(state.doneBytes || 0))} / ${escapeHtml(fmtBytes(state.totalBytes || 0))}</strong></div>
          <div><small>当前速度</small><strong>${escapeHtml(fmtRate(state.currentBps || 0))}</strong></div>
          <div><small>平均速度</small><strong>${escapeHtml(fmtRate(state.averageBps || 0))}</strong></div>
          <div><small>预计剩余</small><strong>${escapeHtml(fmtDuration(state.etaSeconds))}</strong></div>
        </div>
        <div class="transfer-message">${escapeHtml(state.message || "等待操作。")}</div>
      `;
    }

    function uploadFormWithProgress(url, headers, form, onProgress, uploadState = null) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        if (uploadState) uploadState.xhr = xhr;
        xhr.open("POST", url, true);
        Object.entries(headers || {}).forEach(([key, value]) => xhr.setRequestHeader(key, value));
        xhr.upload.onprogress = (event) => {
          if (event.lengthComputable) onProgress(event.loaded, event.total);
        };
        xhr.onload = () => {
          let payload = {};
          try {
            payload = JSON.parse(xhr.responseText || "{}");
          } catch {
            payload = { ok: false, message: xhr.responseText || xhr.statusText || "目标 Agent 返回了无法识别的响应" };
          }
          if (xhr.status >= 200 && xhr.status < 300 && payload.ok) {
            resolve(payload);
          } else {
            reject(new Error(payload.message || xhr.statusText || `上传失败，HTTP ${xhr.status}`));
          }
        };
        xhr.onerror = () => reject(new Error("网络连接失败：浏览器无法连接到目标 Agent"));
        xhr.onabort = () => reject(new Error("上传已取消"));
        xhr.ontimeout = () => reject(new Error("上传超时：目标 Agent 响应太慢"));
        xhr.onloadend = () => {
          if (uploadState?.xhr === xhr) uploadState.xhr = null;
        };
        xhr.timeout = 0;
        xhr.send(form);
      });
    }

    async function sendUploadChunkWithRetry({ route, target, form, onProgress, uploadState, chunkIndex, totalChunks }) {
      const maxAttempts = 3;
      let lastError = null;
      for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
        if (uploadState?.canceled) throw new Error("上传已取消");
        try {
          return await uploadFormWithProgress(
            route.upload_url,
            route.headers || target.headers || {},
            form,
            onProgress,
            uploadState,
          );
        } catch (error) {
          lastError = error;
          if (uploadState?.canceled || attempt >= maxAttempts) break;
          renderTransfer({
            status: "running",
            badge: "重试中",
            title: "公网分片重试",
            target: uploadState?.targetLabel || route.label,
            percent: uploadState?.percent || 0,
            doneBytes: uploadState?.doneBytes || 0,
            totalBytes: uploadState?.totalBytes || 0,
            message: `第 ${chunkIndex + 1}/${totalChunks} 块上传失败，正在第 ${attempt + 1} 次重试：${friendlyError(error)}`,
          });
          await new Promise((resolve) => setTimeout(resolve, 800 * attempt));
        }
      }
      throw lastError || new Error("分片上传失败");
    }

    async function cancelUploadState(uploadState) {
      if (!uploadState || uploadState.cancelSent) return;
      uploadState.cancelSent = true;
      const cancelUrl = uploadState.route?.cancel_url || uploadState.target?.cancel_url;
      const cancelHeaders = uploadState.route?.headers || uploadState.target?.headers || {};
      if (!cancelUrl) return;
      await fetch(cancelUrl, {
        method: "POST",
        headers: { ...cancelHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({ upload_id: uploadState.uploadId }),
      }).catch(() => null);
    }

    async function cancelActiveUpload() {
      const uploadState = activeUpload;
      if (!uploadState) return;
      uploadState.canceled = true;
      refs.cancelUploadBtn.disabled = true;
      if (uploadState.xhr) uploadState.xhr.abort();
      await cancelUploadState(uploadState);
      renderTransfer({
        status: "failed",
        badge: "已取消",
        title: "上传已取消",
        target: uploadState.targetLabel || "--",
        percent: uploadState.percent || 0,
        doneBytes: uploadState.doneBytes || 0,
        totalBytes: uploadState.totalBytes || 0,
        message: "已取消上传，Agent 上的临时分片已经清理。",
      });
    }

    async function probeUploadRoute(candidate) {
      const startedAt = performance.now();
      const payload = new Uint8Array(256 * 1024);
      try {
        const resp = await fetch(candidate.probe_url, {
          method: "POST",
          headers: candidate.headers || {},
          body: payload,
          cache: "no-store",
        });
        const elapsed = Math.max(0.001, (performance.now() - startedAt) / 1000);
        const data = await resp.json().catch(() => ({}));
        return {
          ...candidate,
          ok: resp.ok && data.ok !== false,
          elapsed,
          bps: payload.byteLength / elapsed,
          message: data.message || "",
        };
      } catch (error) {
        return {
          ...candidate,
          ok: false,
          elapsed: 9999,
          bps: 0,
          message: friendlyError(error, "线路测速失败"),
        };
      }
    }

    async function chooseUploadRoute(target) {
      const candidates = target.candidates?.length ? target.candidates : [{
        label: "默认线路",
        upload_url: target.upload_url,
        cancel_url: target.cancel_url,
        probe_url: target.probe_url || target.upload_url.replace("/api/upload-chunk", "/api/upload-probe"),
        headers: target.headers || {},
      }];
      const results = await Promise.all(candidates.map(probeUploadRoute));
      const usable = results.filter((item) => item.ok).sort((a, b) => b.bps - a.bps);
      if (!usable.length) {
        const reason = results.find((item) => item.message)?.message || "所有上传线路测速失败";
        throw new Error(reason);
      }
      return { selected: usable[0], results };
    }

    function pct(value) {
      return Math.max(0, Math.min(100, Number(value || 0)));
    }

    function metric(label, value, percent) {
      const hasPercent = percent !== undefined && percent !== null;
      return `
        <div class="metric">
          <small>${escapeHtml(label)}</small>
          <strong>${escapeHtml(value)}</strong>
          ${hasPercent ? `<div class="bar" style="--value:${pct(percent)}%"><span></span></div>` : ""}
        </div>
      `;
    }

    function donut(label, value, percent, color = "var(--accent)") {
      const safePercent = pct(percent);
      return `
        <div class="health-donut">
          <div class="donut" style="--value:${safePercent}%; --donut-color:${color};">${Math.round(safePercent)}%</div>
          <div class="donut-info">
            <small>${escapeHtml(label)}</small>
            <strong>${escapeHtml(value)}</strong>
          </div>
        </div>
      `;
    }

    function miniRow(label, value) {
      return `<div class="mini-row"><small>${escapeHtml(label)}</small><span>${escapeHtml(value)}</span></div>`;
    }

    function miniRowHtml(label, html) {
      return `<div class="mini-row"><small>${escapeHtml(label)}</small><span>${html}</span></div>`;
    }

    function healthSummaryHtml(node) {
      const h = node?.health || {};
      const stream = h.stream || {};
      const autoRestart = stream.auto_restart || {};
      const adaptive = stream.adaptive || {};
      const transfer = h.transfer || {};
      const youtube = h.youtube || {};
      const lines = [];
      let headline = "健康采集正常，节点可以参与调度。";
      let bad = false;
      if (!h.ok) {
        bad = true;
        headline = h.message ? `健康采集失败：${h.message}` : "健康采集失败：节点不可达。";
        lines.push("检查 Agent 服务、端口和网络。");
      } else {
        lines.push(`Agent 在线 · ${h.agent?.mode || "compatible"} · ${h.agent?.version || "--"}`);
        lines.push(stream.running ? `FFmpeg 推流中 · ${stream.current_bitrate_label || "码率待采集"}` : "FFmpeg 未推流。");
      }
      if (autoRestart.enabled) {
        lines.push(autoRestart.last_error
          ? `自动恢复错误：${autoRestart.last_error}`
          : `自动恢复开启 · ${autoRestart.status || "正常"}`);
      }
      if (adaptive.enabled) {
        lines.push(adaptive.last_error
          ? `智能调参错误：${adaptive.last_error}`
          : `智能调参 · ${adaptive.status || "待命"}`);
      }
      if (youtube.configured || youtube.authorized) {
        lines.push(youtube.authorized ? "YouTube API 已授权。" : "YouTube API 已配置，待授权。");
      }
      if (transfer.last_error) {
        bad = true;
        lines.push(`传输错误：${transfer.last_error}`);
      }
      if (!lines.length) lines.push("暂无更多采集信息。");
      return `
        <div class="health-summary ${bad ? "bad" : ""}">
          <strong>${escapeHtml(headline)}</strong>
          <ul>${lines.slice(0, 7).map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>
        </div>
      `;
    }

    function nodeOnline(node) {
      return Boolean(node.enabled !== false && node.health?.ok);
    }

    function nodeStreaming(node) {
      return Boolean(node.health?.stream?.running);
    }

    function profileName(profileId) {
      const profile = youtubeProfiles.find((item) => String(item.id) === String(profileId));
      return profile?.name || profileId || "Default";
    }

    function profileOptions(selectedId) {
      const profiles = youtubeProfiles.length ? youtubeProfiles : [{ id: "default", name: "Default YouTube Profile" }];
      const selected = selectedId || activeYouTubeProfileId || profiles[0]?.id || "default";
      return profiles.map((profile) => `<option value="${escapeHtml(profile.id)}" ${String(profile.id) === String(selected) ? "selected" : ""}>${escapeHtml(profile.name || profile.id)}</option>`).join("");
    }

    function nodeProfileId(node) {
      return String(node?.youtube_profile_id || activeYouTubeProfileId || "default");
    }

    function nodesForActiveProfile() {
      const profileId = selectedYouTubeProfileId();
      return nodes.filter((node) => node.enabled !== false && Boolean(node.roles?.agent?.enabled) && nodeProfileId(node) === profileId);
    }

    function selectedNode() {
      return nodes.find((node) => String(node.id) === String(selectedNodeId)) || nodes[0] || null;
    }

    function renderMonitor(node) {
      if (!node) {
        return `
          <div class="guided-empty">
            <strong>还没有推流服务器</strong>
            <span>先在目标 VPS 安装 Agent，然后输入它的 Tailscale 100.x 地址，系统会自动检测并接入。</span>
            <div class="actions">
              <button class="primary" data-open-connect>接入推流服务器</button>
              <button data-scroll-target=".bottom-section">查看安装命令</button>
            </div>
          </div>
        `;
      }
      if (!node) {
        return `<div class="empty-state">还没有配置节点。把 VPS 节点加入 config/nodes.json 后会显示在这里。</div>`;
      }
      const h = node.health || {};
      const stream = h.stream || {};
      const adaptive = stream.adaptive || {};
      const autoRestart = stream.auto_restart || {};
      const relay = stream.relay || {};
      const tuning = stream.tuning || {};
      const config = h.stream_config || {};
      const net = h.net || {};
      const quota = h.quota || {};
      const agent = h.agent || {};
      const transfer = h.transfer || {};
      const publicUpload = h.public_upload || {};
      const videos = h.videos || [];
      const loadText = Array.isArray(h.load_avg) && h.load_avg.length ? h.load_avg.join(" / ") : (h.load_avg || "--");
      const bitrate = stream.current_bitrate_label || (stream.current_bitrate_kbps ? `${stream.current_bitrate_kbps} Kbps` : "未知");
      const processText = stream.processes?.length ? `${stream.processes.length} 个进程` : "未检测到";
      const videoList = videos.length
        ? videos.slice(0, 6).map((item) => `${escapeHtml(item.name)} (${escapeHtml(fmtBytes(item.size))})`).join("<br>")
        : "服务器暂无视频";
      const processList = stream.processes?.length
        ? stream.processes.slice(0, 4).map((item) => {
            const pid = item.pid || item.PID || "-";
            const cpu = item.cpu_percent !== undefined ? ` CPU ${Number(item.cpu_percent || 0).toFixed(1)}%` : "";
            return `${escapeHtml(pid)}${escapeHtml(cpu)}`;
          }).join("<br>")
        : "未检测到 FFmpeg 进程";

      return `
        <div class="monitor-hero">
          <div>
            <h3>${escapeHtml(node.name || node.id)}</h3>
            <small>${escapeHtml(h.hostname || node.id)} · ${escapeHtml(h.platform || "未知系统")}</small>
            <small class="mono">${escapeHtml(node.base_url || "")}</small>
            <div class="machine-compact">
              <span>核心 <strong>${escapeHtml(h.cpu_count || "--")}</strong></span>
              <span>负载 <strong>${escapeHtml(loadText)}</strong></span>
              <span>系统在线 <strong>${escapeHtml(h.uptime || "--")}</strong></span>
              <span>面板在线 <strong>${escapeHtml(h.app_uptime || "--")}</strong></span>
              <span>内存 <strong>${escapeHtml(`${fmtBytes(h.memory?.used || 0)} / ${fmtBytes(h.memory?.total || 0)}`)}</strong></span>
              <span>硬盘 <strong>${escapeHtml(`${fmtBytes(h.disk?.used || 0)} / ${fmtBytes(h.disk?.total || 0)}`)}</strong></span>
            </div>
          </div>
          ${nodeStatusPill(node)}
        </div>

        <div class="health-strip">
          ${donut("CPU", `${Number(h.cpu_percent || 0).toFixed(1)}%`, h.cpu_percent)}
          ${donut("内存", `${Number(h.memory?.percent || 0).toFixed(1)}%`, h.memory?.percent)}
          ${donut("硬盘", `${Number(h.disk?.percent || 0).toFixed(1)}%`, h.disk?.percent)}
          ${donut("推流", stream.running ? "运行中" : "未推流", stream.running ? 100 : 0, stream.running ? "var(--accent)" : "var(--danger)")}
        </div>

        <div class="monitor-compact-row">
          <div class="agent-compact">
            <span>Agent <strong>${escapeHtml(agent.mode || "compatible")}</strong></span>
            <span>版本 <strong>${escapeHtml(agent.version || "--")}</strong></span>
            <span>${agent.headless ? "Headless" : "兼容模式"}</span>
            <span>上传 <strong>${publicUpload.supported === false ? "直传" : "票据直传"}</strong></span>
            <span>路由 <strong>${escapeHtml(transfer.last_route || "--")}</strong></span>
            <span>错误 <strong>${escapeHtml(transfer.last_error || "无")}</strong></span>
          </div>

          <div class="network-compact">
            <span class="compact-title">网络</span>
            <span>上行 <strong>${escapeHtml(fmtRate(net.current_upload_bps || 0))}</strong></span>
            <span>下行 <strong>${escapeHtml(fmtRate(net.current_download_bps || 0))}</strong></span>
            <span>累计发 <strong>${escapeHtml(fmtBytes(net.bytes_sent || 0))}</strong></span>
            <span>累计收 <strong>${escapeHtml(fmtBytes(net.bytes_recv || 0))}</strong></span>
            <span>流量 <strong>${escapeHtml(`${Number(quota.total_percent || 0).toFixed(2)}%`)}</strong></span>
            <span>剩余 <strong>${escapeHtml(fmtBytes(quota.remaining || 0))}</strong></span>
            <span>线路 <strong>${escapeHtml(net.rate_label || "--")}</strong></span>
          </div>
        </div>

        <div class="monitor-panel-grid">
          <div class="monitor-panel">
            <h4>推流引擎</h4>
            <div class="metric-grid">
              ${metric("FFmpeg", stream.running ? "运行中" : "未运行")}
              ${metric("进程", processText)}
              ${metric("视频数", `${videos.length}`)}
              ${metric("推流目标", config.stream_output_mode === "youtube_api" ? "YouTube API" : (config.has_stream_key ? "直播码" : "未配置"))}
            </div>
            <div class="mini-table" style="margin-top: 10px;">
              ${miniRow("自动重启", autoRestart.enabled ? `开启 · ${autoRestart.last_error || "正常"}` : "关闭")}
              ${miniRow("智能调参", adaptive.enabled ? `${adaptive.status || "idle"} · ${adaptive.last_error || "正常"}` : "关闭")}
              ${miniRow("本地中继", relay.enabled ? `${relay.mode || "relay"} · ${relay.reachable ? "可达" : "不可达"}` : relay.message || "关闭")}
              ${miniRow("FIFO 缓冲", tuning.fifo_enabled ? `${tuning.fifo_timeshift_seconds || 0}s / queue ${tuning.fifo_queue_size || 0}` : "关闭")}
              ${miniRowHtml("FFmpeg PID", `<span class="mono">${processList}</span>`)}
            </div>
          </div>

          <div class="monitor-panel">
            <h4>节点资源</h4>
            <div class="mini-table" style="margin-top: 10px;">
              ${miniRow("节点 ID", node.id || "--")}
              ${miniRow("启用状态", node.enabled === false ? "已禁用" : "已启用")}
              ${miniRowHtml("健康采集", healthSummaryHtml(node))}
              ${miniRowHtml("服务器视频", `<span class="mono">${videoList}</span>`)}
            </div>
          </div>
        </div>
      `;
    }

    function renderNodeRow(node, checkedIds) {
      const h = node.health || {};
      const online = Boolean(node.roles?.agent?.enabled ?? nodeOnline(node));
      const streaming = nodeStreaming(node);
      const selected = String(node.id) === String(selectedNodeId);
      const checked = checkedIds.has(String(node.id));
      const note = String(node.note || "").trim();
      const notePreview = note ? `${[...note].slice(0, 6).join("")}${[...note].length > 6 ? "…" : ""}` : "添加备注";
      return `
        <div class="node-row ${selected ? "selected" : ""} ${online ? "" : "offline-node"}" data-node-row data-node-id="${escapeHtml(node.id)}" title="点击选中；删除/取消角色请打开后面的设置">
          <input data-node-check type="checkbox" value="${escapeHtml(node.id)}" ${checked ? "checked" : ""} ${node.enabled === false ? "disabled" : ""} title="选中后可推送资源或升级">
          <span class="node-name">
            <strong>${escapeHtml(node.name || node.id)}</strong>
            <small>${escapeHtml(h.hostname || node.id)} · 版本 ${escapeHtml(h.agent?.version || "未识别")}</small>
            <span class="node-profile-label">Profile</span>
            <select class="node-profile-select" data-node-profile-select data-node-id="${escapeHtml(node.id)}" title="选择这个 Agent 隶属的 YouTube Profile">${profileOptions(nodeProfileId(node))}</select>
            <button class="node-note" data-node-note data-node-id="${escapeHtml(node.id)}" title="${escapeHtml(note || "点击添加备注")}">${escapeHtml(notePreview)}</button>
          </span>
          <span class="node-state">${stateDot(online, node.enabled === false)}${online ? "在线" : node.enabled === false ? "禁用" : "离线"}</span>
          <span class="node-state">${streamDot(streaming)}${streaming ? "推流中" : "未推流"}</span>
          <span class="row-actions">
            <button class="tiny" data-node-action="stop-stream" data-node-id="${escapeHtml(node.id)}" ${online ? "" : "disabled"}>停止推流</button>
            <button class="tiny" data-node-action="restart-stream" data-node-id="${escapeHtml(node.id)}" ${online ? "" : "disabled"}>重启推流</button>
            <button class="tiny danger" data-node-action="reboot-vps" data-node-id="${escapeHtml(node.id)}" ${online ? "" : "disabled"}>重启 VPS</button>
            <button class="tiny settings-button" data-role-settings data-node-id="${escapeHtml(node.id)}" title="节点角色设置" aria-label="节点角色设置">⚙</button>
          </span>
        </div>
      `;
    }

    function renderHubRow(node) {
      const role = node.roles?.hub || {};
      const enabled = Boolean(role.enabled);
      const version = role.version || "未安装";
      const current = enabled && role.url && sameOriginUrl(role.url);
      return `
        <div class="node-row role-row ${current ? "control-hub" : ""}" data-hub-row data-node-id="${escapeHtml(node.id)}" data-hub-url="${escapeHtml(role.url || "")}">
          <span>${stateDot(enabled, false)}</span>
          <span class="node-name"><strong>${escapeHtml(node.name || node.id)}</strong><small>${current ? "当前控制 Hub · " : ""}Hub 版本 ${escapeHtml(version)}</small><span class="node-profile-label">Profile</span><select class="node-profile-select" data-node-profile-select data-node-id="${escapeHtml(node.id)}" title="选择这个 Hub 隶属的 YouTube Profile">${profileOptions(nodeProfileId(node))}</select></span>
          <span class="node-state">${current ? "控制中" : enabled ? "已启用" : "未启用"}</span>
          <span class="node-state">8788</span>
          <span class="row-actions">
            <button class="tiny" data-role-action="switch-hub" data-node-id="${escapeHtml(node.id)}">${current ? "当前 Hub" : "切换 Hub"}</button>
            <button class="tiny settings-button" data-role-settings data-node-id="${escapeHtml(node.id)}" title="节点角色设置" aria-label="节点角色设置">⚙</button>
          </span>
        </div>
      `;
    }

    function renderNodes() {
      const checkedIds = new Set(selectedNodeIds().map(String));
      const nodeHasResources = (nodeId) => (mediaLibrary.resources || []).some((resource) => resourceHasNode(resource, nodeId));
      const shouldShowAgentRow = (node) => {
        const nodeId = String(node.id || "");
        const agentEnabled = Boolean(node.roles?.agent?.enabled);
        return node.enabled !== false && (agentEnabled || nodeHasResources(nodeId));
      };
      const agentRows = nodes.filter(shouldShowAgentRow);
      const activeHubs = nodes.filter((node) => Boolean(node.roles?.hub?.enabled));
      const onlineAgentCount = agentRows.filter((node) => Boolean(node.roles?.agent?.enabled)).length;
      refs.agentNodeCount.textContent = `${onlineAgentCount}/${agentRows.length}`;
      refs.hubNodeCount.textContent = String(activeHubs.length);
      if (!nodes.length) {
        refs.nodeMonitor.innerHTML = renderMonitor(null);
        refs.nodeList.innerHTML = `<div class="guided-empty"><strong>等待接入 Agent</strong><span>点击“接入推流服务器”，或先复制一键安装命令到目标 VPS。</span><button class="primary" data-open-connect>连接 Agent</button></div>`;
        refs.hubNodeList.innerHTML = `<div class="empty-state">还没有激活的 Hub。</div>`;
        updatePrimaryActionStates();
        return;
      }
      if (!nodes.some((node) => String(node.id) === String(selectedNodeId))) {
        rememberSelectedNode(nodes[0].id || "");
      }
      refs.nodeMonitor.innerHTML = renderMonitor(selectedNode());
      refs.nodeList.innerHTML = agentRows.length ? `
        <div class="node-table-head">
          <span></span>
          <span>节点</span>
          <span>在线</span>
          <span>推流</span>
          <span>操作</span>
        </div>
        ${agentRows.map((node) => renderNodeRow(node, checkedIds)).join("")}
      ` : `<div class="empty-state">还没有配置 Agent 节点。</div>`;
      refs.hubNodeList.innerHTML = activeHubs.length ? `
        <div class="node-table-head"><span></span><span>Hub 节点</span><span>状态</span><span>端口</span><span>操作</span></div>
        ${activeHubs.map((node) => renderHubRow(node)).join("")}
      ` : `<div class="empty-state">还没有已激活的 Hub。</div>`;
      window.requestAnimationFrame(syncNodeRoleSplitHeight);
      updatePrimaryActionStates();
    }

    function mediaGroupName(id) {
      return (mediaLibrary.groups || []).find((item) => item.id === id)?.name || "未分组";
    }

    function mediaResourceByName(name) {
      return (mediaLibrary.resources || []).find((item) => String(item.name || "") === String(name || ""));
    }

    function resourceHasNode(item, nodeId) {
      if (!nodeId) return true;
      return (item.copies || []).some((copy) => String(copy.node_id || "") === String(nodeId));
    }

    function renderNodeSpaceRings(nodeDisks) {
      const diskByNodeId = new Map((nodeDisks || []).map((item) => [String(item.node_id || ""), item]));
      const nodeHasResources = (nodeId) => (mediaLibrary.resources || []).some((resource) => resourceHasNode(resource, nodeId));
      const merged = nodes.filter((node) => {
        const nodeId = String(node.id || "");
        return Boolean(node.roles?.agent?.enabled) || nodeHasResources(nodeId);
      }).map((node) => {
        const nodeId = String(node.id || "");
        return diskByNodeId.get(nodeId) || {
          node_id: nodeId,
          node_name: node.name || node.id || "Agent",
          online: false,
          total: 0,
          used: 0,
          free: 0,
          percent: 0,
        };
      });
      if (!merged.length) {
        refs.nodeSpaceRings.innerHTML = `<div class="muted">暂无节点数据。</div>`;
        return;
      }
      refs.nodeSpaceRings.innerHTML = merged.map((item) => {
        const percent = Math.max(0, Math.min(100, Number(item.percent || 0)));
        const online = Boolean(item.online);
        const open = String(item.node_id || "") === String(openResourceNodeId || "");
        return `<div class="node-space-ring-item ${open ? "open" : ""} ${online ? "" : "offline-node"}" role="button" tabindex="0" data-space-node-id="${escapeHtml(item.node_id || "")}" title="双击在资源管理模块查看 ${escapeHtml(item.node_name)} 的视频；删除/取消角色请打开后面的设置；已用 ${escapeHtml(fmtBytes(item.used))} / ${escapeHtml(fmtBytes(item.total))}">
          <div class="node-space-ring ${online ? "" : "offline"}" style="--disk-percent:${percent.toFixed(1)}"><span>${online ? `${percent.toFixed(0)}%` : "离线"}</span></div>
          <strong>${escapeHtml(item.node_name)}</strong>
          <small>${online ? `剩余 ${escapeHtml(fmtBytes(item.free))}` : "无法读取空间"}</small>
        </div>`;
      }).join("");
    }

    function renderMedia() {
      const checkedPath = selectedMediaPath();
      const checkedNodeId = selectedMediaNodeId();
      const groupId = refs.mediaGroupFilter.value || "";
      const groups = mediaLibrary.groups || [];
      const allResources = [...(mediaLibrary.resources || [])];
      const openedNode = (mediaLibrary.nodes || []).find((item) => String(item.node_id || "") === String(openResourceNodeId));
      const entries = allResources
        .filter((item) => !groupId || (groupId === "__ungrouped__" ? !item.group_id : item.group_id === groupId))
        .filter((item) => resourceHasNode(item, openResourceNodeId))
        .sort((a, b) => Number(b.modified || 0) - Number(a.modified || 0));
      const options = `<option value="">全部分组</option><option value="__ungrouped__">未分组</option>`
        + groups.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("");
      const currentFilter = refs.mediaGroupFilter.value;
      refs.mediaGroupFilter.innerHTML = options;
      if ([...refs.mediaGroupFilter.options].some((option) => option.value === currentFilter)) refs.mediaGroupFilter.value = currentFilter;
      const currentUploadGroup = refs.uploadGroupInput.value;
      refs.uploadGroupInput.innerHTML = `<option value="">上传到未分组</option>`
        + groups.map((item) => `<option value="${escapeHtml(item.id)}">上传到：${escapeHtml(item.name)}</option>`).join("");
      if ([...refs.uploadGroupInput.options].some((option) => option.value === currentUploadGroup)) refs.uploadGroupInput.value = currentUploadGroup;
      refs.mediaDiskList.innerHTML = (mediaLibrary.nodes || []).map((item) => {
        const percent = Math.max(0, Math.min(100, Number(item.percent || 0)));
        const nodeId = String(item.node_id || "");
        const open = nodeId && nodeId === String(openResourceNodeId);
        const count = allResources.filter((resource) => resourceHasNode(resource, nodeId)).length;
        return `<div class="disk-card ${open ? "open" : ""}" role="button" tabindex="0" data-resource-node-id="${escapeHtml(nodeId)}" title="双击打开 ${escapeHtml(item.node_name)} 的视频资源">
          <div class="disk-card-head"><strong>${escapeHtml(item.node_name)}</strong><span>${item.online ? `剩余 ${escapeHtml(fmtBytes(item.free))}` : "离线"}</span></div>
          <div class="disk-bar"><span style="width:${percent}%"></span></div>
          <small>已用 ${escapeHtml(fmtBytes(item.used))} / ${escapeHtml(fmtBytes(item.total))}（${percent.toFixed(1)}%） · ${count} 个视频</small>
        </div>`;
      }).join("");
      refs.resourceFilterChip.innerHTML = openedNode
        ? `<span>正在查看：${escapeHtml(openedNode.node_name)} · ${entries.length} 个视频</span><button data-clear-resource-node>显示全部资源</button>`
        : `<span>双击容量条打开节点资源；当前显示全部节点。</span>`;
      if (!entries.length) {
        refs.mediaList.innerHTML = `<div class="empty-state">当前筛选下没有视频资源。</div>`;
        return;
      }
      refs.mediaList.innerHTML = `
        <div class="media-toolbar">
          <strong>${escapeHtml(openedNode ? openedNode.node_name : "所有 Agent / Hub")} · ${escapeHtml(groupId ? mediaGroupName(groupId) : "全部资源")}</strong>
          <small>共 ${entries.length} 个。双击打开详情；右键可查看属性、移动分组、复制/移动到节点。</small>
        </div>
        <div class="media-window">
          <div class="media-window-head">
            <span>文件名</span>
            <span>大小</span>
            <span>上传时间</span>
            <span>分组 / 副本节点</span>
          </div>
          ${entries.map((item) => {
            const copies = item.copies || [];
            const copy = copies.find((entry) => String(entry.node_id) === String(openResourceNodeId || selectedNodeId)) || copies[0] || {};
            const videoPath = copy.video_path || item.name;
            const nodeId = String(copy.node_id || "");
            const selected = checkedPath && checkedNodeId === nodeId && checkedPath === videoPath;
            const current = nodeId === String(selectedNodeId);
            const name = item.name || videoPath;
            const copyNames = copies.map((entry) => entry.node_name || entry.node_id).join("、");
            const lastUsedLabels = copies.map((entry) => `${entry.node_name || entry.node_id}: ${entry.last_used_label || "从未开播"}`).join("；");
            const ageBase = Number(item.last_used_at || item.created_at || item.modified || 0);
            const ageDays = ageBase ? Math.max(0, (Date.now() / 1000 - ageBase) / 86400) : 0;
            const ageTier = Math.floor(ageDays / 3);
            const nameOpacity = Math.max(0.32, 1 - ageTier * 0.13);
            const cleanupCandidate = ageTier >= 5;
            return `
              <div role="button" tabindex="0" class="media-file-row ${current ? "current-agent" : ""} ${selected ? "selected" : ""} ${cleanupCandidate ? "cleanup-candidate" : ""}" data-media-row data-node-id="${escapeHtml(nodeId)}" data-media-name="${escapeHtml(name)}" data-video-path="${escapeHtml(videoPath)}" data-group-id="${escapeHtml(item.group_id || "")}" data-group-name="${escapeHtml(mediaGroupName(item.group_id))}" data-copy-names="${escapeHtml(copyNames)}" data-copy-count="${escapeHtml(copies.length)}" data-last-used-label="${escapeHtml(lastUsedLabels || "从未开播")}" data-size="${escapeHtml(item.size || 0)}" data-modified-label="${escapeHtml(item.modified_label || "--")}">
                <span style="opacity:${nameOpacity.toFixed(2)}" title="${escapeHtml(name)}｜冷却 ${ageDays.toFixed(1)} 天｜${cleanupCandidate ? "可人工评估删除" : "活跃"}">${escapeHtml(name)}</span>
                <span class="muted">${escapeHtml(fmtBytes(item.size || 0))}</span>
                <span class="muted">${escapeHtml(item.modified_label || "--")}</span>
                <span title="${escapeHtml(copyNames)}">${escapeHtml(mediaGroupName(item.group_id))} · ${copies.length} 副本</span>
                <input data-media-check type="radio" name="media" value="${escapeHtml(name)}" data-node-id="${escapeHtml(nodeId)}" data-video-path="${escapeHtml(videoPath)}" ${selected ? "checked" : ""} hidden>
              </div>
            `;
          }).join("")}
        </div>
      `;
    }

    function renderQuickGroups(groups) {
      const visible = groups.slice(0, QUICK_GROUP_LIMIT);
      refs.quickGroupBar.style.setProperty("--quick-group-count", String(Math.max(1, visible.length)));
      if (!visible.length) {
        refs.quickGroupBar.innerHTML = `<span class="muted">暂无快捷分组</span>`;
        return;
      }
      refs.quickGroupBar.innerHTML = visible.map((group, index) => {
        const active = String(refs.mediaGroupFilter.value || "") === String(group.id);
        return `<button class="${active ? "active" : ""}" data-quick-group-index="${index}" data-quick-group-id="${escapeHtml(group.id)}" title="右键改名">${escapeHtml(group.name)}</button>`;
      }).join("");
    }

    function resourceOwnerCopy(item) {
      const copies = item.copies || [];
      return copies.find((entry) => String(entry.node_id) === String(selectedNodeId)) || copies[0] || {};
    }

    function resourceMatchesFilters(item) {
      const filters = resourceTableFilters;
      const copies = item.copies || [];
      const owner = resourceOwnerCopy(item);
      const name = String(item.name || "").toLowerCase();
      if (filters.name && !name.includes(filters.name.toLowerCase())) return false;
      if (filters.group && (filters.group === "__ungrouped__" ? item.group_id : item.group_id || "") !== (filters.group === "__ungrouped__" ? "" : filters.group)) return false;
      if (filters.copyNode && !copies.some((copy) => String(copy.node_id || "") === String(filters.copyNode))) return false;
      if (filters.ownerNode && String(owner.node_id || "") !== String(filters.ownerNode)) return false;
      const size = Number(item.size || 0);
      if (filters.size === "small" && size >= 500 * 1024 * 1024) return false;
      if (filters.size === "medium" && (size < 500 * 1024 * 1024 || size >= 2 * 1024 * 1024 * 1024)) return false;
      if (filters.size === "large" && size < 2 * 1024 * 1024 * 1024) return false;
      if (filters.age) {
        const modified = Number(item.modified || 0);
        const days = modified ? (Date.now() / 1000 - modified) / 86400 : Infinity;
        if (days > Number(filters.age)) return false;
      }
      return true;
    }

    function clearResourceFilters() {
      resourceTableFilters = { name: "", size: "", age: "", group: "", copyNode: "", ownerNode: "" };
      openResourceNodeId = "";
      refs.mediaGroupFilter.value = "";
      renderMedia();
    }

    function renderMedia() {
      const checkedPath = selectedMediaPath();
      const checkedNodeId = selectedMediaNodeId();
      const groups = mediaLibrary.groups || [];
      const nodeDisks = mediaLibrary.nodes || [];
      renderNodeSpaceRings(nodeDisks);
      const allResources = [...(mediaLibrary.resources || [])];
      const groupOptions = `<option value="">全部分组</option><option value="__ungrouped__">未分组</option>`
        + groups.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("");
      const currentFilter = refs.mediaGroupFilter.value;
      refs.mediaGroupFilter.innerHTML = groupOptions;
      if ([...refs.mediaGroupFilter.options].some((option) => option.value === currentFilter)) refs.mediaGroupFilter.value = currentFilter;
      else refs.mediaGroupFilter.value = "";
      resourceTableFilters.group = refs.mediaGroupFilter.value || "";
      const currentUploadGroup = refs.uploadGroupInput.value;
      refs.uploadGroupInput.innerHTML = `<option value="">上传到未分组</option>`
        + groups.map((item) => `<option value="${escapeHtml(item.id)}">上传到：${escapeHtml(item.name)}</option>`).join("");
      if ([...refs.uploadGroupInput.options].some((option) => option.value === currentUploadGroup)) refs.uploadGroupInput.value = currentUploadGroup;
      if (refs.mediaDiskList) refs.mediaDiskList.innerHTML = "";
      if (refs.resourceFilterChip) refs.resourceFilterChip.hidden = true;
      renderQuickGroups(groups);

      const entries = allResources.filter(resourceMatchesFilters).sort((a, b) => Number(b.modified || 0) - Number(a.modified || 0));
      const allGroupOptions = `<option value="">全部分组</option><option value="__ungrouped__">未分组</option>`
        + groups.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("");
      const nodeOptions = `<option value="">全部节点</option>`
        + nodeDisks.map((item) => `<option value="${escapeHtml(item.node_id)}">${escapeHtml(item.node_name)}</option>`).join("");
      const selectedGroupOptions = allGroupOptions.replace('value="' + escapeHtml(resourceTableFilters.group) + '"', 'value="' + escapeHtml(resourceTableFilters.group) + '" selected');
      const selectedCopyNodeOptions = nodeOptions.replace('value="' + escapeHtml(resourceTableFilters.copyNode) + '"', 'value="' + escapeHtml(resourceTableFilters.copyNode) + '" selected');
      const selectedOwnerNodeOptions = nodeOptions.replace('value="' + escapeHtml(resourceTableFilters.ownerNode) + '"', 'value="' + escapeHtml(resourceTableFilters.ownerNode) + '" selected');
      const resourceNameOptions = allResources.map((item) => `<option value="${escapeHtml(item.name || "")}"></option>`).join("");
      refs.mediaList.innerHTML = `
        <div class="media-toolbar">
          <strong>所有节点的所有视频</strong>
          <span><small>共 ${entries.length} 个。表头可筛选；右键可操作。</small> <button class="tiny" data-clear-resource-filters>全部资源</button></span>
        </div>
        <div class="media-window">
          <div class="media-window-head">
            <label>文件名<input data-resource-filter="name" list="resourceNameOptions" type="search" value="${escapeHtml(resourceTableFilters.name)}" placeholder="输入或点选"></label>
            <label>大小<select data-resource-filter="size"><option value="">全部</option><option value="small" ${resourceTableFilters.size === "small" ? "selected" : ""}>小于500M</option><option value="medium" ${resourceTableFilters.size === "medium" ? "selected" : ""}>500M-2G</option><option value="large" ${resourceTableFilters.size === "large" ? "selected" : ""}>大于2G</option></select></label>
            <label>上传时间<select data-resource-filter="age"><option value="">全部</option><option value="7" ${resourceTableFilters.age === "7" ? "selected" : ""}>近7天</option><option value="30" ${resourceTableFilters.age === "30" ? "selected" : ""}>近30天</option><option value="90" ${resourceTableFilters.age === "90" ? "selected" : ""}>近90天</option></select></label>
            <label>分组<select data-resource-filter="group">${selectedGroupOptions}</select></label>
            <label>副本节点<select data-resource-filter="copyNode">${selectedCopyNodeOptions}</select></label>
            <label>归属节点<select data-resource-filter="ownerNode">${selectedOwnerNodeOptions}</select></label>
          </div>
          <datalist id="resourceNameOptions">${resourceNameOptions}</datalist>
          ${entries.length ? entries.map((item) => {
            const copies = item.copies || [];
            const copy = resourceOwnerCopy(item);
            const videoPath = copy.video_path || item.name;
            const nodeId = String(copy.node_id || "");
            const selected = checkedPath && checkedNodeId === nodeId && checkedPath === videoPath;
            const current = nodeId === String(selectedNodeId);
            const name = item.name || videoPath;
            const copyNames = copies.map((entry) => entry.node_name || entry.node_id).join("、");
            const lastUsedLabels = copies.map((entry) => `${entry.node_name || entry.node_id}: ${entry.last_used_label || "从未开播"}`).join("；");
            const ageBase = Number(item.last_used_at || item.created_at || item.modified || 0);
            const ageDays = ageBase ? Math.max(0, (Date.now() / 1000 - ageBase) / 86400) : 0;
            const cleanupCandidate = Math.floor(ageDays / 3) >= 5;
            return `
              <div role="button" tabindex="0" class="media-file-row ${current ? "current-agent" : ""} ${selected ? "selected" : ""} ${cleanupCandidate ? "cleanup-candidate" : ""}" data-media-row data-node-id="${escapeHtml(nodeId)}" data-media-name="${escapeHtml(name)}" data-video-path="${escapeHtml(videoPath)}" data-group-id="${escapeHtml(item.group_id || "")}" data-group-name="${escapeHtml(mediaGroupName(item.group_id))}" data-copy-names="${escapeHtml(copyNames)}" data-copy-count="${escapeHtml(copies.length)}" data-last-used-label="${escapeHtml(lastUsedLabels || "从未开播")}" data-size="${escapeHtml(item.size || 0)}" data-modified-label="${escapeHtml(item.modified_label || "--")}">
                <span title="${escapeHtml(name)}">${escapeHtml(name)}</span>
                <span class="muted">${escapeHtml(fmtBytes(item.size || 0))}</span>
                <span class="muted">${escapeHtml(item.modified_label || "--")}</span>
                <span>${escapeHtml(mediaGroupName(item.group_id))}</span>
                <span title="${escapeHtml(copyNames)}">${copies.length} 副本：${escapeHtml(copyNames || "--")}</span>
                <span>${escapeHtml(copy.node_name || copy.node_id || "--")}</span>
                <input data-media-check type="radio" name="media" value="${escapeHtml(name)}" data-node-id="${escapeHtml(nodeId)}" data-video-path="${escapeHtml(videoPath)}" ${selected ? "checked" : ""} hidden>
              </div>
            `;
          }).join("") : `<div class="empty-state">当前筛选下没有视频资源。</div>`}
        </div>
      `;
      updatePrimaryActionStates();
    }

    function syncStreamOutputMode() {
      const mode = refs.streamOutputModeInput.value || "direct";
      refs.streamKeyInput.disabled = mode !== "direct";
      refs.youtubeStreamSelect.disabled = mode !== "youtube_api";
      updatePrimaryActionStates();
    }

    function setYouTubeModalOpen(open) {
      refs.youtubeWizardModal.classList.toggle("open", open);
      refs.youtubeWizardModal.setAttribute("aria-hidden", open ? "false" : "true");
      if (!open && youtubeOauthPollTimer) {
        clearTimeout(youtubeOauthPollTimer);
        youtubeOauthPollTimer = null;
      }
      if (open) {
        const node = ensureSelectedNodeForProfile();
        refs.youtubeNodeInput.value = node ? `${node.name || node.id} (${node.id})` : "先选择 Agent";
        renderYouTubeAgentList();
        if (!refs.youtubeScheduleInput.value) {
          const planned = new Date(Date.now() + 5 * 60 * 1000);
          const local = new Date(planned.getTime() - planned.getTimezoneOffset() * 60 * 1000);
          refs.youtubeScheduleInput.value = local.toISOString().slice(0, 16);
        }
        loadYouTubeProfiles().then(() => refreshYouTubeResources()).catch(() => refreshYouTubeResources());
      }
    }

    function openYouTubeJsonImport() {
      setYouTubeModalOpen(true);
      refs.youtubeWizardLog.textContent = "请选择 Google 下载的 client_secret_*.json，系统会自动读取 Client ID / Secret。";
      refs.youtubeJsonFileInput.click();
    }

    function renderYouTubeStreams(streams = [], selectedStreamId = "") {
      const previousMain = selectedStreamId || refs.youtubeStreamSelect.value;
      const previousPrepare = refs.youtubePrepareStreamSelect.value;
      const streamOptions = streams.map((item) => {
        const status = item.stream_status || "ready";
        return `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title || item.id)} (${escapeHtml(status)})</option>`;
      }).join("");
      refs.youtubeStreamSelect.innerHTML = streams.length
        ? streamOptions
        : `<option value="">没有可用 YouTube 直播流</option>`;
      refs.youtubePrepareStreamSelect.innerHTML = `<option value="">创建新的可复用直播流</option>${streamOptions}`;
      if (streams.some((item) => item.id === previousMain)) refs.youtubeStreamSelect.value = previousMain;
      if (streams.some((item) => item.id === previousPrepare)) refs.youtubePrepareStreamSelect.value = previousPrepare;
      syncStreamOutputMode();
    }

    function selectedYouTubeProfileId() {
      return refs.youtubeProfileSelect?.value || activeYouTubeProfileId || "default";
    }

    async function youtubeProfileApi(path, payload = null) {
      const options = payload
        ? { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(payload) }
        : { headers: authHeaders() };
      const response = await fetch(path, options);
      try {
        return await response.json();
      } catch (_) {
        return { ok: false, message: response.statusText || "YouTube Profile request failed" };
      }
    }

    function youtubeProfilePayload() {
      return {
        profile_id: selectedYouTubeProfileId(),
        profile_name: refs.youtubeProfileNameInput?.value?.trim() || selectedYouTubeProfileId(),
        auto_tune_enabled: refs.youtubeAutoTuneEnabledInput?.value === "1",
        auto_tune_interval_seconds: Number(refs.youtubeAutoTuneIntervalInput?.value || 300),
        auto_tune_cooldown_seconds: Number(refs.youtubeAutoTuneCooldownInput?.value || 900),
        auto_tune_max_bitrate: Number(refs.youtubeAutoTuneMaxBitrateInput?.value || 6000),
      };
    }

    function currentYouTubeProfile() {
      const profileId = selectedYouTubeProfileId();
      return youtubeProfiles.find((item) => String(item.id) === String(profileId)) || null;
    }

    function setYouTubeProfileNameEditing(profileId = "") {
      editingYouTubeProfileId = String(profileId || "");
      renderYouTubeProfileQuickBar();
      if (!editingYouTubeProfileId) return;
      window.setTimeout(() => {
        const input = Array.from(refs.youtubeProfileQuickBar?.querySelectorAll("[data-youtube-profile-edit]") || [])
          .find((item) => String(item.dataset.youtubeProfileEdit || "") === String(editingYouTubeProfileId));
        if (input) {
          input.focus();
          input.select();
        }
      }, 0);
    }

    async function saveYouTubeProfileName(nextName, profileId = editingYouTubeProfileId || selectedYouTubeProfileId()) {
      profileId = String(profileId || selectedYouTubeProfileId());
      const profile = youtubeProfiles.find((item) => String(item.id) === String(profileId)) || currentYouTubeProfile();
      const previousName = profile?.name || profileId || "YouTube Profile";
      const name = String(nextName || "").replace(/\s+/g, " ").trim().slice(0, 80);
      if (!name) {
        if (refs.youtubeProfileNameInput) refs.youtubeProfileNameInput.value = previousName;
        setYouTubeProfileNameEditing("");
        return;
      }
      if (name === previousName) {
        setYouTubeProfileNameEditing("");
        return;
      }
      setYouTubeProfileNameEditing("");
      const data = await youtubeProfileApi("/api/youtube/profiles", {
        ...youtubeProfilePayload(),
        profile_id: profileId,
        name,
      });
      if (data.ok) {
        renderYouTubeProfiles(data.profiles || [], data.active_profile_id || data.profile?.id || profileId);
        refs.youtubeWizardLog.textContent = `Profile renamed: ${name}`;
      } else {
        if (refs.youtubeProfileNameInput) refs.youtubeProfileNameInput.value = previousName;
        refs.youtubeWizardLog.textContent = data.message || "Profile rename failed";
        renderYouTubeProfileQuickBar();
      }
    }

    function cancelYouTubeProfileNameEdit() {
      const profile = currentYouTubeProfile();
      if (refs.youtubeProfileNameInput) refs.youtubeProfileNameInput.value = profile?.name || selectedYouTubeProfileId();
      setYouTubeProfileNameEditing("");
    }

    function applyYouTubeProfileToForm(profile = {}) {
      activeYouTubeProfileId = profile.id || activeYouTubeProfileId || "default";
      if (refs.youtubeProfileNameInput) {
        refs.youtubeProfileNameInput.value = profile.name || activeYouTubeProfileId;
        editingYouTubeProfileId = "";
      }
      if (refs.youtubeClientIdInput) refs.youtubeClientIdInput.value = profile.client_id || "";
      if (refs.youtubeClientSecretInput) refs.youtubeClientSecretInput.value = "";
      if (refs.youtubeAutoTuneEnabledInput) refs.youtubeAutoTuneEnabledInput.value = profile.auto_tune_enabled ? "1" : "0";
      if (refs.youtubeAutoTuneIntervalInput) refs.youtubeAutoTuneIntervalInput.value = profile.auto_tune_interval_seconds || 300;
      if (refs.youtubeAutoTuneCooldownInput) refs.youtubeAutoTuneCooldownInput.value = profile.auto_tune_cooldown_seconds || 900;
      if (refs.youtubeAutoTuneMaxBitrateInput) refs.youtubeAutoTuneMaxBitrateInput.value = profile.auto_tune_max_bitrate || 6000;
      const usage = profile.usage || {};
      if (refs.youtubeUsageInput) {
        refs.youtubeUsageInput.value = `${usage.calls || 0} calls / ${usage.estimated_units || 0} units`;
      }
    }

    function renderYouTubeProfiles(profiles = [], activeId = "") {
      youtubeProfiles = profiles;
      activeYouTubeProfileId = activeId || profiles[0]?.id || "default";
      if (!refs.youtubeProfileSelect) return;
      refs.youtubeProfileSelect.innerHTML = profiles.map((profile) => {
        const label = `${profile.name || profile.id} (${profile.usage?.estimated_units || 0}u)`;
        return `<option value="${escapeHtml(profile.id)}">${escapeHtml(label)}</option>`;
      }).join("");
      if (profiles.some((profile) => profile.id === activeYouTubeProfileId)) {
        refs.youtubeProfileSelect.value = activeYouTubeProfileId;
      }
      applyYouTubeProfileToForm(profiles.find((profile) => profile.id === refs.youtubeProfileSelect.value) || profiles[0] || {});
      renderYouTubeProfileQuickBar();
      renderNodes();
      renderYouTubeAgentList();
    }

    function renderYouTubeProfileQuickBar() {
      if (!refs.youtubeProfileQuickBar) return;
      const profiles = youtubeProfiles.length ? youtubeProfiles : [{ id: "default", name: "Default YouTube Profile", usage: {} }];
      refs.youtubeProfileQuickBar.style.setProperty("--youtube-profile-slots", String(YOUTUBE_PROFILE_VISIBLE_SLOTS));
      refs.youtubeProfileQuickBar.innerHTML = profiles.map((profile) => {
        const active = String(profile.id) === String(selectedYouTubeProfileId());
        const usage = profile.usage?.estimated_units || 0;
        const label = profile.name || profile.id;
        if (String(profile.id) === String(editingYouTubeProfileId)) {
          return `<span class="profile-chip profile-chip-editing ${active ? "active" : ""}" data-youtube-profile-chip="${escapeHtml(profile.id)}"><input class="profile-chip-input" data-youtube-profile-edit="${escapeHtml(profile.id)}" type="text" maxlength="80" value="${escapeHtml(label)}" aria-label="Rename YouTube profile"></span>`;
        }
        return `<button type="button" class="profile-chip ${active ? "active" : ""}" data-youtube-profile-chip="${escapeHtml(profile.id)}" aria-pressed="${active ? "true" : "false"}" title="Double-click to rename: ${escapeHtml(label)}">${escapeHtml(label)} · ${usage}u</button>`;
      }).join("");
    }

    async function loadYouTubeProfiles() {
      const data = await youtubeProfileApi("/api/youtube/profiles");
      if (data.ok) renderYouTubeProfiles(data.profiles || [], data.active_profile_id || "");
      return data;
    }

    async function createYouTubeProfile() {
      const base = `youtube-${youtubeProfiles.length + 1}`;
      const data = await youtubeProfileApi("/api/youtube/profiles", {
          profile_id: base,
          name: `YouTube Profile ${youtubeProfiles.length + 1}`,
          auto_tune_enabled: false,
          auto_tune_interval_seconds: 300,
          auto_tune_cooldown_seconds: 900,
          auto_tune_max_bitrate: 6000,
      });
      if (data.ok) {
        renderYouTubeProfiles(data.profiles || [], data.active_profile_id || data.profile?.id || "");
        refs.youtubeWizardLog.textContent = "YouTube Profile created. Upload client_secret_*.json, then save.";
      }
    }

    async function deleteYouTubeProfile() {
      const profileId = selectedYouTubeProfileId();
      if (!profileId || youtubeProfiles.length <= 1) {
        refs.youtubeWizardLog.textContent = "At least one YouTube Profile is required.";
        return;
      }
      const data = await youtubeProfileApi("/api/youtube/profiles/delete", { profile_id: profileId });
      if (data.ok) {
        renderYouTubeProfiles(data.profiles || [], data.active_profile_id || "");
        refs.youtubeWizardLog.textContent = "YouTube Profile deleted.";
        await refreshYouTubeResources();
      } else {
        refs.youtubeWizardLog.textContent = data.message || "Profile delete failed";
      }
    }

    async function selectYouTubeProfile() {
      const profileId = selectedYouTubeProfileId();
      const profile = youtubeProfiles.find((item) => item.id === profileId) || {};
      applyYouTubeProfileToForm(profile);
      renderYouTubeProfileQuickBar();
      ensureSelectedNodeForProfile();
      renderYouTubeAgentList();
      renderNodes();
      await youtubeProfileApi("/api/youtube/profiles/select", { profile_id: profileId });
      await refreshYouTubeResources();
    }

    function ensureSelectedNodeForProfile() {
      const matches = nodesForActiveProfile();
      if (!matches.length) {
        rememberSelectedNode("");
        return null;
      }
      if (!matches.some((node) => String(node.id) === String(selectedNodeId))) {
        const idle = matches.find((node) => !nodeStreaming(node));
        rememberSelectedNode((idle || matches[0]).id || "");
      }
      return selectedYouTubeAgent();
    }

    function selectedYouTubeAgent() {
      return nodesForActiveProfile().find((node) => String(node.id) === String(selectedNodeId)) || null;
    }

    function renderYouTubeAgentList() {
      if (!refs.youtubeAgentList) return;
      const matches = nodesForActiveProfile();
      if (!matches.length) {
        refs.youtubeAgentList.innerHTML = `<div class="youtube-detail-empty">这个 Profile 还没有绑定 Agent。请先在节点表的 Profile 下拉里分配。</div>`;
        refs.youtubeNodeInput.value = "当前 Profile 暂无 Agent";
        return;
      }
      refs.youtubeAgentList.innerHTML = matches.map((node) => {
        const streaming = nodeStreaming(node);
        const active = String(node.id) === String(selectedNodeId);
        return `<button type="button" class="youtube-agent-card ${active ? "active" : ""} ${streaming ? "streaming" : ""}" data-youtube-agent-id="${escapeHtml(node.id)}" aria-pressed="${active ? "true" : "false"}" ${streaming ? "disabled" : ""}>
          <strong>${escapeHtml(node.name || node.id)}</strong>
          <small>${escapeHtml(node.youtube_profile_name || profileName(nodeProfileId(node)))}${streaming ? " · 已开播" : ""}</small>
        </button>`;
      }).join("");
      const node = matches.find((item) => String(item.id) === String(selectedNodeId));
      refs.youtubeNodeInput.value = node ? `${node.name || node.id} (${node.id})` : "先选择 Agent";
    }

    function compactValue(value) {
      if (value === true) return "on";
      if (value === false) return "off";
      if (value === null || value === undefined || value === "") return "--";
      return String(value);
    }

    function shortDateTime(value) {
      if (!value) return "--";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleString();
    }

    function renderDefinitionRows(rows = []) {
      return rows.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(compactValue(value))}</dd>`).join("");
    }

    function renderYouTubeResourceDetails(data = {}) {
      if (!refs.youtubeResourceDetails) return;
      const streams = data.streams || [];
      const broadcasts = data.broadcasts || [];
      const streamById = new Map(streams.map((item) => [item.id, item]));
      const streamCards = streams.slice(0, 4).map((item) => `
        <div class="youtube-detail-card">
          <strong>${escapeHtml(item.title || item.id)}</strong>
          <dl>${renderDefinitionRows([
            ["Status", item.stream_status || "--"],
            ["Health", item.health_status || "--"],
            ["Resolution", item.resolution || "--"],
            ["Frame rate", item.frame_rate || "--"],
            ["Reusable", item.is_reusable],
            ["Issues", (item.configuration_issues || []).length],
          ])}</dl>
        </div>
      `).join("");
      const broadcastCards = broadcasts.slice(0, 6).map((item) => {
        const stream = streamById.get(item.bound_stream_id) || {};
        const watch = item.watch_url ? `<dt>Watch</dt><dd><a href="${escapeHtml(item.watch_url)}" target="_blank" rel="noopener">${escapeHtml(item.id)}</a></dd>` : "";
        return `
          <div class="youtube-detail-card">
            <strong>${escapeHtml(item.title || item.id)}</strong>
            <dl>
              ${renderDefinitionRows([
                ["Lifecycle", item.life_cycle_status || "--"],
                ["Privacy", item.privacy_status || "--"],
                ["Recording", item.recording_status || "--"],
                ["Scheduled", shortDateTime(item.scheduled_start_time)],
                ["Actual start", shortDateTime(item.actual_start_time)],
                ["End", shortDateTime(item.actual_end_time || item.scheduled_end_time)],
                ["DVR", item.enable_dvr],
                ["Record", item.record_from_start],
                ["Embed", item.enable_embed],
                ["Auto start", item.enable_auto_start],
                ["Auto stop", item.enable_auto_stop],
                ["Captions", item.enable_closed_captions],
                ["Latency", item.latency_preference || "--"],
                ["Monitor delay", item.broadcast_stream_delay_ms],
                ["Made for kids", item.made_for_kids ?? item.self_declared_made_for_kids],
                ["Live chat", item.live_chat_id ? "available" : "--"],
                ["Ads", item.ads_monetization_status || item.eligible_for_ads_monetization || "--"],
                ["Bound stream", item.bound_stream_id || "--"],
                ["Stream health", stream.health_status || "--"],
              ])}
              ${watch}
            </dl>
          </div>
        `;
      }).join("");
      refs.youtubeResourceDetails.innerHTML = `
        <div class="youtube-details-head">
          <strong>YouTube Studio Details</strong>
          <span>${escapeHtml(data.channel?.title || "--")} · ${streams.length} streams · ${broadcasts.length} broadcasts</span>
        </div>
        ${streamCards ? `<div class="youtube-details-grid">${streamCards}</div>` : `<div class="youtube-detail-empty">No reusable live streams returned.</div>`}
        ${broadcastCards ? `<div class="youtube-details-grid">${broadcastCards}</div>` : `<div class="youtube-detail-empty">No broadcasts returned.</div>`}
      `;
    }

    function clearYouTubeResourceDetails(message = "No YouTube resources loaded yet.") {
      if (!refs.youtubeResourceDetails) return;
      refs.youtubeResourceDetails.innerHTML = `
        <div class="youtube-details-head">
          <strong>YouTube Studio Details</strong>
          <span>Click refresh to load broadcast and stream settings.</span>
        </div>
        <div class="youtube-detail-empty">${escapeHtml(message)}</div>
      `;
    }

    function renderYouTubeResourceSummary(data = {}) {
      const streams = data.streams || [];
      const broadcasts = data.broadcasts || [];
      const streamById = new Map(streams.map((item) => [item.id, item]));
      const lines = [
        `Channel: ${data.channel?.title || "--"}`,
        `Streams: ${streams.length}`,
        `Broadcasts: ${broadcasts.length}`,
      ];
      if (streams.length) {
        lines.push("");
        lines.push("Streams");
        streams.slice(0, 5).forEach((item) => {
          lines.push(`- ${item.title || item.id}`);
          lines.push(`  status=${item.stream_status || "--"} health=${item.health_status || "--"} ${item.resolution || "--"}/${item.frame_rate || "--"} reusable=${compactValue(item.is_reusable)}`);
          const issues = item.configuration_issues || [];
          if (issues.length) lines.push(`  issues=${issues.length}: ${issues.slice(0, 2).map((issue) => issue.reason || issue.type || issue.description || String(issue)).join("; ")}`);
        });
      }
      if (broadcasts.length) {
        lines.push("");
        lines.push("Broadcasts");
        broadcasts.slice(0, 5).forEach((item) => {
          const stream = streamById.get(item.bound_stream_id) || {};
          lines.push(`- ${item.title || item.id}`);
          lines.push(`  status=${item.life_cycle_status || "--"} privacy=${item.privacy_status || "--"} recording=${item.recording_status || "--"}`);
          lines.push(`  scheduled=${shortDateTime(item.scheduled_start_time)} actual=${shortDateTime(item.actual_start_time)} end=${shortDateTime(item.actual_end_time || item.scheduled_end_time)}`);
          lines.push(`  dvr=${compactValue(item.enable_dvr)} record=${compactValue(item.record_from_start)} embed=${compactValue(item.enable_embed)} autoStart=${compactValue(item.enable_auto_start)} autoStop=${compactValue(item.enable_auto_stop)}`);
          lines.push(`  captions=${compactValue(item.enable_closed_captions)} latency=${compactValue(item.latency_preference)} monitorDelayMs=${compactValue(item.broadcast_stream_delay_ms)} madeForKids=${compactValue(item.made_for_kids ?? item.self_declared_made_for_kids)}`);
          lines.push(`  chat=${item.live_chat_id ? "available" : "--"} ads=${compactValue(item.ads_monetization_status || item.eligible_for_ads_monetization)} stream=${item.bound_stream_id || "--"} health=${stream.health_status || "--"}`);
          if (item.watch_url) lines.push(`  ${item.watch_url}`);
        });
      }
      return lines.join("\n");
    }

    async function refreshYouTubeResources() {
      const matches = nodesForActiveProfile();
      let node = matches.find((item) => String(item.id) === String(selectedNodeId));
      if (!node && matches.length) {
        node = ensureSelectedNodeForProfile();
      }
      if (!node) {
        renderYouTubeStreams([]);
        renderYouTubeAgentList();
        renderYouTubeResourceDetails({});
        refs.youtubeWizardLog.textContent = "当前 Profile 还没有可用 Agent。请先在节点列表给 Agent 选择这个 Profile。";
        return null;
      }
      refs.youtubeRefreshBtn.disabled = true;
      refs.youtubeNodeInput.value = `${node.name || node.id} (${node.id})`;
      renderYouTubeAgentList();
      refs.youtubeWizardLog.textContent = "正在由 Hub 读取 YouTube 授权和直播资源...";
      try {
        const data = await postNodeAction("/api/nodes/youtube/resources", { node_id: node.id, profile_id: selectedYouTubeProfileId() });
        if (!data.ok && data.configured === undefined) {
          renderYouTubeStreams([]);
          refs.youtubeWizardLog.textContent = data.message || "YouTube API 读取失败";
          return data;
        }
        if (!data.configured) {
          renderYouTubeStreams([]);
          refs.youtubeWizardLog.textContent = "当前 Hub 尚未配置 YouTube OAuth。请先上传 client_secret_*.json 并保存 API 配置，再连接 YouTube。";
          return data;
        }
        if (!data.authorized) {
          renderYouTubeStreams([]);
          refs.youtubeWizardLog.textContent = "Client ID 已配置，频道尚未授权。点击“连接 YouTube”获取设备验证码。";
          return data;
        }
        if (!data.ok) {
          refs.youtubeWizardLog.textContent = data.message || "YouTube API 读取失败";
          return data;
        }
        const selectedId = node.health?.stream_config?.youtube_stream_id || "";
        renderYouTubeStreams(data.streams || [], selectedId);
        renderYouTubeResourceDetails(data);
        if (data.profile) {
          youtubeProfiles = youtubeProfiles.map((profile) => profile.id === data.profile.id ? data.profile : profile);
          applyYouTubeProfileToForm(data.profile);
        }
        const lines = [
          `频道：${data.channel?.title || "--"}`,
          `直播流：${(data.streams || []).length} 个`,
          `直播活动：${(data.broadcasts || []).length} 个`,
        ];
        (data.broadcasts || []).slice(0, 5).forEach((item) => {
          lines.push(`${item.title || item.id} / ${item.life_cycle_status || "--"} / ${item.privacy_status || "--"}`);
        });
        refs.youtubeWizardLog.textContent = renderYouTubeResourceSummary(data);
        return data;
      } catch (error) {
        refs.youtubeWizardLog.textContent = friendlyError(error, "YouTube API 读取失败");
        return null;
      } finally {
        refs.youtubeRefreshBtn.disabled = false;
      }
    }

    async function pollYouTubeAuthorization(delaySeconds = 5) {
      if (!youtubeOauthSession) return;
      if (youtubeOauthPollTimer) clearTimeout(youtubeOauthPollTimer);
      youtubeOauthPollTimer = setTimeout(async () => {
        try {
          const node = selectedYouTubeAgent();
          if (!node) {
            youtubeOauthSession = "";
            youtubeOauthPollTimer = null;
            refs.youtubeWizardLog.textContent = "当前 Profile 没有可用 Agent，已停止授权检查。";
            return;
          }
          const data = await postNodeAction("/api/nodes/youtube/oauth/poll", {
            node_id: node.id,
            profile_id: selectedYouTubeProfileId(),
            session_id: youtubeOauthSession,
          });
          if (data.ok && data.authorized) {
            youtubeOauthSession = "";
            youtubeOauthPollTimer = null;
            refs.youtubeWizardLog.textContent = "YouTube 授权成功，正在读取频道资源...";
            await refreshYouTubeResources();
            return;
          }
          if (data.ok && data.pending) {
            pollYouTubeAuthorization(Number(data.retry_after || 5));
            return;
          }
          youtubeOauthSession = "";
          youtubeOauthPollTimer = null;
          refs.youtubeWizardLog.textContent = data.message || "YouTube 授权失败";
        } catch (error) {
          youtubeOauthSession = "";
          youtubeOauthPollTimer = null;
          refs.youtubeWizardLog.textContent = friendlyError(error, "YouTube 授权状态读取失败");
        }
      }, Math.max(1, Number(delaySeconds || 5)) * 1000);
    }

    async function startYouTubeAuthorization() {
      const node = ensureSelectedNodeForProfile();
      if (!node) {
        refs.youtubeWizardLog.textContent = "当前 Profile 还没有可用 Agent。";
        return;
      }
      refs.youtubeAuthorizeBtn.disabled = true;
      try {
        if (refs.youtubeClientIdInput.value.trim() || refs.youtubeJsonInput.value.trim()) {
          const saved = await saveYouTubeConfig({ refreshAfter: false, keepSecret: true, quiet: true });
          if (!saved?.ok) return;
        }
        const data = await postNodeAction("/api/nodes/youtube/oauth/start", { node_id: node.id, profile_id: selectedYouTubeProfileId() });
        if (!data.ok) {
          refs.youtubeWizardLog.textContent = data.message || "无法启动 YouTube 授权";
          return;
        }
        youtubeOauthSession = data.session_id;
        refs.youtubeWizardLog.innerHTML = `
          <div class="wizard-status-line done"><strong>设备验证码：${escapeHtml(data.user_code)}</strong></div>
          <div class="wizard-status-line"><a href="${escapeHtml(data.verification_url)}" target="_blank" rel="noopener">打开 Google 设备授权页面</a></div>
          <div class="wizard-status-line">完成授权后本页会自动刷新。</div>
        `;
        window.open(data.verification_url, "_blank", "noopener");
        pollYouTubeAuthorization(Number(data.interval || 5));
      } catch (error) {
        refs.youtubeWizardLog.textContent = friendlyError(error, "无法启动 YouTube 授权");
      } finally {
        refs.youtubeAuthorizeBtn.disabled = false;
      }
    }

    function extractYouTubeOAuthClient(payload) {
      const source = payload?.installed || payload?.web || payload || {};
      const clientId = String(source.client_id || "").trim();
      const clientSecret = String(source.client_secret || "").trim();
      const looksValid = clientId.endsWith(".apps.googleusercontent.com");
      if (!clientId || !looksValid) {
        throw new Error("没有在 JSON 里找到有效的 client_id。请确认下载的是 OAuth Client JSON，不是 API key 或 service account JSON。");
      }
      if (String(payload?.type || "").toLowerCase() === "service_account" || source.private_key) {
        throw new Error("这是 Service Account JSON，不能用于 YouTube 频道授权。请创建 OAuth Client：TVs and Limited Input devices。");
      }
      return { clientId, clientSecret };
    }

    function applyYouTubeOAuthJsonText(text, sourceLabel = "JSON") {
      const raw = String(text || "").trim();
      if (!raw) return false;
      let payload;
      try {
        payload = JSON.parse(raw);
      } catch (_) {
        refs.youtubeWizardLog.textContent = `${sourceLabel} 不是有效 JSON，请检查是否完整复制。`;
        return false;
      }
      try {
        const parsed = extractYouTubeOAuthClient(payload);
        refs.youtubeClientIdInput.value = parsed.clientId;
        refs.youtubeClientSecretInput.value = parsed.clientSecret;
        refs.youtubeWizardLog.textContent = parsed.clientSecret
          ? `${sourceLabel} 已读取：Client ID 和 Client Secret 已自动填入。下一步点“保存 API 配置”。`
          : `${sourceLabel} 已读取：Client ID 已自动填入；这个 OAuth 客户端没有 Client Secret，可以直接保存。`;
        return true;
      } catch (error) {
        refs.youtubeWizardLog.textContent = friendlyError(error, `${sourceLabel} 读取失败`);
        return false;
      }
    }

    async function loadYouTubeOAuthJsonFile() {
      const file = refs.youtubeJsonFileInput.files?.[0];
      if (!file) return;
      if (!file.name.toLowerCase().endsWith(".json")) {
        refs.youtubeWizardLog.textContent = "请选择 Google 下载的 .json 文件。";
        return;
      }
      try {
        const text = await file.text();
        refs.youtubeJsonInput.value = text;
        applyYouTubeOAuthJsonText(text, file.name);
      } catch (error) {
        refs.youtubeWizardLog.textContent = friendlyError(error, "OAuth JSON 文件读取失败");
      }
    }

    async function saveYouTubeConfig(options = {}) {
      const node = ensureSelectedNodeForProfile();
      if (!node) {
        refs.youtubeWizardLog.textContent = "当前 Profile 还没有可用 Agent。";
        return { ok: false };
      }
      if (!refs.youtubeClientIdInput.value.trim() && refs.youtubeJsonInput.value.trim()) {
        applyYouTubeOAuthJsonText(refs.youtubeJsonInput.value, "粘贴的 JSON");
      }
      const clientId = refs.youtubeClientIdInput.value.trim();
      const clientSecret = refs.youtubeClientSecretInput.value.trim();
      if (!clientId) {
        refs.youtubeWizardLog.textContent = "请先上传 Google 下载的 client_secret_*.json。";
        return { ok: false };
      }
      refs.youtubeSaveConfigBtn.disabled = true;
      if (!options.quiet) refs.youtubeWizardLog.textContent = "正在把 YouTube API 配置保存到当前 Hub...";
      try {
        const data = await postNodeAction("/api/nodes/youtube/config", {
          node_id: node.id,
          ...youtubeProfilePayload(),
          client_id: clientId,
          client_secret: clientSecret,
        });
        if (!data.ok) {
          refs.youtubeWizardLog.textContent = data.message || "YouTube API 配置保存失败";
          return data;
        }
        if (!options.keepSecret) refs.youtubeClientSecretInput.value = "";
        if (data.profile) {
          youtubeProfiles = youtubeProfiles.map((profile) => profile.id === data.profile.id ? data.profile : profile);
          if (!youtubeProfiles.some((profile) => profile.id === data.profile.id)) youtubeProfiles.push(data.profile);
          renderYouTubeProfiles(youtubeProfiles, data.profile.id);
        }
        if (!options.quiet) refs.youtubeWizardLog.textContent = "配置已保存。下一步点击“连接 YouTube”完成频道授权。";
        if (options.refreshAfter !== false) await refreshYouTubeResources();
        return data;
      } catch (error) {
        refs.youtubeWizardLog.textContent = friendlyError(error, "YouTube API 配置保存失败");
        return { ok: false, message: String(error?.message || error || "") };
      } finally {
        refs.youtubeSaveConfigBtn.disabled = false;
      }
    }

    async function prepareYouTubeBroadcast() {
      const title = refs.youtubeTitleInput.value.trim();
      const node = ensureSelectedNodeForProfile();
      if (!node || !title) {
        refs.youtubeWizardLog.textContent = "请选择 Agent 并填写直播标题。";
        return;
      }
      refs.youtubePrepareBtn.disabled = true;
      try {
        const resolutionMatch = refs.resolutionInput.value.match(/x(\d+)$/i);
        const scheduled = refs.youtubeScheduleInput.value
          ? new Date(refs.youtubeScheduleInput.value).toISOString()
          : "";
        const data = await postNodeAction("/api/nodes/youtube/prepare", {
          node_id: node.id,
          profile_id: selectedYouTubeProfileId(),
          title,
          privacy_status: refs.youtubePrivacyInput.value,
          scheduled_start_time: scheduled,
          stream_id: refs.youtubePrepareStreamSelect.value,
          resolution: resolutionMatch ? `${resolutionMatch[1]}p` : "720p",
          frame_rate: Number(refs.fpsInput.value || 30) >= 50 ? "60fps" : "30fps",
          enable_auto_start: true,
          enable_auto_stop: true,
        });
        if (!data.ok) {
          refs.youtubeWizardLog.textContent = data.message || "创建 YouTube 直播失败";
          return;
        }
        refs.youtubeWizardLog.textContent = `直播已创建并绑定。\n${data.result?.title || title}\n${data.result?.watch_url || ""}`;
        await refreshYouTubeResources();
        refs.youtubeStreamSelect.value = data.result?.stream_id || "";
        refs.streamOutputModeInput.value = "youtube_api";
        syncStreamOutputMode();
      } catch (error) {
        refs.youtubeWizardLog.textContent = friendlyError(error, "创建 YouTube 直播失败");
      } finally {
        refs.youtubePrepareBtn.disabled = false;
      }
    }

    async function readYouTubeHealth() {
      const streamId = refs.youtubeStreamSelect.value || refs.youtubePrepareStreamSelect.value;
      const node = ensureSelectedNodeForProfile();
      if (!node || !streamId) {
        refs.youtubeWizardLog.textContent = "请先选择 Agent 和 YouTube 直播流。";
        return;
      }
      refs.youtubeHealthBtn.disabled = true;
      refs.youtubeWizardLog.textContent = "正在由 Hub 读取 YouTube 直播健康反馈...";
      try {
        const data = await postNodeAction("/api/nodes/youtube/health", {
          ...streamPayload({ includeKey: false }),
          node_id: node.id,
          profile_id: selectedYouTubeProfileId(),
          youtube_stream_id: streamId,
        });
        if (!data.ok) {
          refs.youtubeWizardLog.textContent = data.message || "YouTube 健康反馈读取失败";
          return;
        }
        if (data.profile) applyYouTubeProfileToForm(data.profile);
        applyTuneRecommendation(data);
        lastTuneRecommendation = data;
        const health = data.health || {};
        const analysis = data.analysis || {};
        const reasons = [...(analysis.reasons || []), ...(analysis.warnings || [])];
        refs.youtubeWizardLog.textContent = [
          "YouTube 健康反馈已读取，推荐参数已应用到 Smart Start 面板。",
          `健康状态：${health.health_status || "--"} / 推流状态：${health.stream_status || "--"}`,
          `YouTube 流：${health.title || streamId}`,
          "",
          ...(reasons.length ? reasons : ["当前没有明显风险，维持现有参数。"]),
        ].join("\n");
        if (refs.commandAdvanced) refs.commandAdvanced.open = true;
        renderTuneRecommendation(data);
      } catch (error) {
        refs.youtubeWizardLog.textContent = friendlyError(error, "YouTube 健康反馈读取失败");
      } finally {
        refs.youtubeHealthBtn.disabled = false;
      }
    }

    async function revokeYouTubeAuthorization() {
      const node = ensureSelectedNodeForProfile();
      if (!node || !window.confirm("确认断开当前 Agent 的 YouTube 授权？")) return;
      try {
        const data = await postNodeAction("/api/nodes/youtube/oauth/revoke", { node_id: node.id, profile_id: selectedYouTubeProfileId() });
        refs.youtubeWizardLog.textContent = data.ok ? "YouTube 授权已断开。" : (data.message || "断开授权失败");
        if (data.ok) renderYouTubeStreams([]);
      } catch (error) {
        refs.youtubeWizardLog.textContent = friendlyError(error, "断开授权失败");
      }
    }

    function renderStreamControls() {
      const node = selectedNode();
      const h = node?.health || {};
      const previousLibraryName = refs.streamVideoSelect.selectedOptions[0]?.dataset.libraryName || "";
      const groups = mediaLibrary.groups || [];
      const groupName = (id) => groups.find((item) => item.id === id)?.name || "未分组";
      const videos = mediaLibrary.resources || [];
      refs.streamNodeInput.value = node ? `${node.name || node.id} (${node.id})` : "选择右侧 VPS 节点";
      refs.streamNodeHint.textContent = node
        ? `${h.ok ? "在线" : "离线"} / ${h.agent?.mode || "旧客户端"} / ${h.stream?.running ? "推流中" : "未推流"}`
        : "等待选择节点";
      refs.streamVideoSelect.innerHTML = videos.length ? videos.map((item) => {
        const localCopy = (item.copies || []).find((copy) => String(copy.node_id) === String(node?.id || ""));
        const sourceCopy = localCopy || (item.copies || [])[0] || {};
        const value = localCopy?.video_path || item.name;
        const copyHint = localCopy ? "本机已有" : "开播前自动复制";
        return `<option value="${escapeHtml(value)}" data-library-name="${escapeHtml(item.name)}" data-media-local="${localCopy ? "1" : "0"}" data-source-node-id="${escapeHtml(sourceCopy.node_id || "")}">[${escapeHtml(groupName(item.group_id))}] ${escapeHtml(item.name)} · ${copyHint} (${escapeHtml(fmtBytes(item.size || 0))})</option>`;
      }).join("") : `<option value="">媒体库暂无视频，请先上传</option>`;
      if (previousLibraryName) {
        const option = [...refs.streamVideoSelect.options].find((item) => item.dataset.libraryName === previousLibraryName);
        if (option) option.selected = true;
      }
      const config = h.stream_config || {};
      if (config.stream_url && !refs.streamUrlInput.dataset.userEdited) refs.streamUrlInput.value = config.stream_url;
      if (config.stream_output_mode) refs.streamOutputModeInput.value = config.stream_output_mode;
      if (config.adaptive_mode) refs.adaptiveModeInput.value = config.adaptive_mode;
      if (config.resolution) refs.resolutionInput.value = config.resolution;
      if (config.fps) refs.fpsInput.value = config.fps;
      if (config.video_bitrate) refs.videoBitrateInput.value = config.video_bitrate;
      if (config.audio_bitrate) refs.audioBitrateInput.value = config.audio_bitrate;
      if (config.preset && config.preset !== "copy") refs.presetInput.value = config.preset;
      if (config.keyframe_seconds) refs.keyframeInput.value = config.keyframe_seconds;
      if (config.youtube_stream_id) refs.youtubeStreamSelect.value = config.youtube_stream_id;
      syncStreamOutputMode();
    }

    function streamPayload({ includeKey = true } = {}) {
      const selectedMediaOption = refs.streamVideoSelect.selectedOptions[0];
      const payload = {
        node_id: selectedNodeId,
        youtube_profile_id: selectedYouTubeProfileId(),
        stream_url: refs.streamUrlInput.value.trim(),
        stream_key: includeKey ? refs.streamKeyInput.value.trim() : "",
        youtube_stream_id: refs.youtubeStreamSelect.value,
        video_path: refs.streamVideoSelect.value,
        library_media_name: selectedMediaOption?.dataset.libraryName || "",
        media_local: selectedMediaOption?.dataset.mediaLocal === "1",
        source_node_id: selectedMediaOption?.dataset.sourceNodeId || "",
        copy_mode: refs.tuneBox.dataset.copyMode === "1",
        adaptive_mode: refs.adaptiveModeInput.value || "auto",
        stream_output_mode: refs.streamOutputModeInput.value || "direct",
        preset: refs.presetInput.value.trim() || "veryfast",
        video_bitrate: Number(refs.videoBitrateInput.value || 4500),
        audio_bitrate: Number(refs.audioBitrateInput.value || 192),
        fps: Number(refs.fpsInput.value || 30),
        resolution: refs.resolutionInput.value.trim() || "1280x720",
        keyframe_seconds: Number(refs.keyframeInput.value || 2),
      };
      if (payload.copy_mode) payload.preset = "copy";
      return payload;
    }

    function applyTuneRecommendation(data) {
      const recommendation = data?.recommendation || {};
      lastTuneRecommendation = data;
      if (typeof recommendation.copy_mode === "boolean") {
        // Copy mode is safe to pass through the backend even though this UI keeps controls simple.
        refs.tuneBox.dataset.copyMode = recommendation.copy_mode ? "1" : "0";
      }
      if (recommendation.preset && recommendation.preset !== "copy") refs.presetInput.value = recommendation.preset;
      if (recommendation.video_bitrate) refs.videoBitrateInput.value = recommendation.video_bitrate;
      if (recommendation.audio_bitrate) refs.audioBitrateInput.value = recommendation.audio_bitrate;
      if (recommendation.fps) refs.fpsInput.value = recommendation.fps;
      if (recommendation.resolution) refs.resolutionInput.value = recommendation.resolution;
      if (recommendation.keyframe_seconds) refs.keyframeInput.value = recommendation.keyframe_seconds;
    }

    function renderTuneRecommendation(data) {
      const recommendation = data?.recommendation || {};
      const bounds = data?.quality_bounds || {};
      const maxQuality = bounds.max_quality || recommendation || {};
      const minQuality = bounds.min_quality || {};
      const analysis = data?.analysis || {};
      const source = analysis.source || {};
      const reasons = [...(analysis.reasons || []), ...(analysis.warnings || [])];
      const fmtTarget = (target) => (
        target && Object.keys(target).length
          ? `${target.resolution || "--"} / ${target.fps || "--"}fps / ${target.video_bitrate || "--"}k / ${target.preset || "--"} / 关键帧 ${target.keyframe_seconds || "--"} 秒`
          : "--"
      );
      refs.tuneBox.textContent = [
        `智能评分：${analysis.score || "--"}/100`,
        `策略：${recommendation.strategy === "copy" ? "Copy passthrough" : "Transcode"}`,
        `最高稳定质量：${fmtTarget(maxQuality)}`,
        `最低保底质量：${fmtTarget(minQuality)}`,
        `当前启动建议：${fmtTarget(recommendation)}`,
        `环境：CPU ${analysis.cpu_percent?.toFixed ? analysis.cpu_percent.toFixed(0) : "--"}% / ${analysis.cpu_count || "--"} 核，内存可用 ${analysis.memory_available_mb || "--"} MB`,
        `运行：speed ${analysis.ffmpeg_speed ? analysis.ffmpeg_speed.toFixed(2) + "x" : "未知"}，当前码率 ${analysis.current_stream_bitrate_kbps ? analysis.current_stream_bitrate_kbps.toFixed(0) + " kbps" : "未知"}，网络预算 ${analysis.network_budget_kbps ? analysis.network_budget_kbps + " kbps" : "待开播后校正"}`,
        `源视频：${source.width || "--"}x${source.height || "--"} / ${source.fps ? source.fps.toFixed(0) + "fps" : "未知"}`,
        "",
        ...(reasons.length ? reasons : ["当前环境没有明显风险，推荐值偏保守。"]),
      ].join("\n");
    }

    async function refreshAll() {
      refs.refreshBtn.disabled = true;
      try {
        const [nodeResp, libraryResp] = await Promise.all([fetch("/api/nodes"), fetch("/api/media-library")]);
        nodes = await nodeResp.json();
        mediaLibrary = await libraryResp.json();
        renderNodes();
        renderMedia();
        renderStreamControls();
        renderYouTubeAgentList();
        renderTailscaleNodeOptions();
        log("状态已刷新");
      } finally {
        refs.refreshBtn.disabled = false;
      }
    }

    async function uploadMedia() {
      const node = selectedNode();
      const file = refs.mediaInput.files[0];
      if (!node?.id) {
        renderTransfer({ status: "failed", badge: "失败", title: "上传未开始", message: "请先在右侧选择一个目标 Agent。" });
        return;
      }
      if (!file) {
        renderTransfer({ status: "failed", badge: "失败", title: "上传未开始", message: "请先选择一个视频文件。" });
        return;
      }
      refs.uploadBtn.dataset.busy = "1";
      uiMessage("正在准备上传线路，请稍等。");
      refs.uploadBtn.disabled = true;
      refs.cancelUploadBtn.disabled = false;
      const uploadId = `browser_${Date.now()}_${Math.random().toString(16).slice(2)}`;
      let target = null;
      let uploadRoute = null;
      const uploadState = {
        uploadId,
        target: null,
        route: null,
        xhr: null,
        canceled: false,
        cancelSent: false,
        targetLabel: node.name || node.id,
        doneBytes: 0,
        totalBytes: file.size,
        percent: 0,
      };
      activeUpload = uploadState;
      try {
        const targetResp = await fetch("/api/nodes/upload-target", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ node_id: node.id, upload_id: uploadId, filename: file.name, total_size: file.size }),
        });
        target = await targetResp.json();
        uploadState.target = target;
        if (!target.ok) {
          renderTransfer({
            status: "failed",
            badge: "失败",
            title: "无法获取上传目标",
            message: friendlyError(target.message || "Hub 未返回可用 Agent 上传地址"),
          });
          return;
        }
        if (uploadState.canceled) throw new Error("上传已取消");
        renderTransfer({
          status: "running",
          badge: "测速中",
          title: `选择上传线路`,
          target: node.name || node.id,
          totalBytes: file.size,
          message: "正在测速公网线路；上传仅使用公网，不会回退 Tailscale 内网。",
        });
        const routeChoice = await chooseUploadRoute(target);
        if (uploadState.canceled) throw new Error("上传已取消");
        uploadRoute = routeChoice.selected;
        uploadState.route = uploadRoute;
        uploadState.targetLabel = `${node.name || node.id} / ${uploadRoute.label}`;
        const uploadFilename = target.filename || file.name;
        const savedNameNote = uploadFilename !== file.name ? `，保存名：${uploadFilename}` : "";
        const chunkSize = Number(target.chunk_bytes || 16 * 1024 * 1024);
        const totalChunks = Math.ceil(file.size / chunkSize);
        const startedAt = performance.now();
        let lastPayload = {};
        let lastPaintAt = 0;
        renderTransfer({
          status: "running",
          badge: "上传中",
          title: `上传到 ${node.name || node.id}`,
          target: `${node.name || node.id} / ${uploadRoute.label}`,
          totalBytes: file.size,
          currentBps: uploadRoute.bps || 0,
          message: `已选择 ${uploadRoute.label}，测速 ${fmtRate(uploadRoute.bps || 0)}，准备上传：${file.name}${savedNameNote}`,
        });
        for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex += 1) {
          if (uploadState.canceled) throw new Error("上传已取消");
          const offset = chunkIndex * chunkSize;
          const blob = file.slice(offset, Math.min(file.size, offset + chunkSize));
          const form = new FormData();
          form.append("upload_id", uploadId);
          form.append("filename", uploadFilename);
          form.append("chunk_index", String(chunkIndex));
          form.append("total_chunks", String(totalChunks));
          form.append("offset", String(offset));
          form.append("total_size", String(file.size));
          form.append("chunk_size", String(chunkSize));
          form.append("chunk", blob, uploadFilename);
          const chunkStartedAt = performance.now();
          lastPayload = await sendUploadChunkWithRetry({
            route: uploadRoute,
            target,
            form,
            uploadState,
            chunkIndex,
            totalChunks,
            onProgress: (loaded) => {
            const now = performance.now();
            if (now - lastPaintAt < 250 && loaded < blob.size) return;
            lastPaintAt = now;
            const chunkSeconds = Math.max(0.001, (now - chunkStartedAt) / 1000);
            const totalSeconds = Math.max(0.001, (now - startedAt) / 1000);
            const uploaded = Math.min(file.size, offset + loaded);
            const averageBps = uploaded / totalSeconds;
            uploadState.doneBytes = uploaded;
            uploadState.percent = file.size ? (uploaded / file.size) * 100 : 0;
            renderTransfer({
              status: "running",
              badge: "上传中",
              title: `上传到 ${node.name || node.id}`,
              target: `${node.name || node.id} / ${uploadRoute.label}`,
              percent: file.size ? (uploaded / file.size) * 100 : 0,
              doneBytes: uploaded,
              totalBytes: file.size,
              currentBps: loaded / chunkSeconds,
              averageBps,
              etaSeconds: averageBps > 0 ? (file.size - uploaded) / averageBps : 0,
              message: `正在通过 ${uploadRoute.label} 上传 ${file.name}${savedNameNote}，第 ${chunkIndex + 1}/${totalChunks} 块。`,
            });
            },
          });
          const chunkSeconds = Math.max(0.001, (performance.now() - chunkStartedAt) / 1000);
          const totalSeconds = Math.max(0.001, (performance.now() - startedAt) / 1000);
          const uploaded = Math.min(file.size, offset + blob.size);
          const averageBps = uploaded / totalSeconds;
          uploadState.doneBytes = uploaded;
          uploadState.percent = file.size ? (uploaded / file.size) * 100 : 0;
          renderTransfer({
            status: "running",
            badge: "上传中",
            title: `上传到 ${node.name || node.id}`,
            target: `${node.name || node.id} / ${uploadRoute.label}`,
            percent: file.size ? (uploaded / file.size) * 100 : 0,
            doneBytes: uploaded,
            totalBytes: file.size,
            currentBps: blob.size / chunkSeconds,
            averageBps,
            etaSeconds: averageBps > 0 ? (file.size - uploaded) / averageBps : 0,
            message: `正在通过 ${uploadRoute.label} 上传 ${file.name}${savedNameNote}，第 ${chunkIndex + 1}/${totalChunks} 块。`,
          });
        }
        const elapsed = Math.max(0.001, (performance.now() - startedAt) / 1000);
        renderTransfer({
          status: "done",
          badge: "完成",
          title: `上传完成`,
          target: `${node.name || node.id} / ${uploadRoute?.label || "默认线路"}`,
          percent: 100,
          doneBytes: file.size,
          totalBytes: file.size,
          currentBps: 0,
          averageBps: file.size / elapsed,
          etaSeconds: 0,
          message: `${file.name} 已通过 ${uploadRoute?.label || "默认线路"} 上传到 ${node.name || node.id}${savedNameNote}。`,
        });
        await postJson("/api/media-library/assign", {
          filename: uploadFilename,
          group_id: refs.uploadGroupInput.value || "",
        });
        await refreshAll();
      } catch (error) {
        await cancelUploadState(uploadState);
        if (uploadState.canceled) {
          renderTransfer({
            status: "failed",
            badge: "已取消",
            title: "上传已取消",
            target: `${node.name || node.id}${uploadRoute?.label ? " / " + uploadRoute.label : ""}`,
            percent: uploadState.percent || 0,
            doneBytes: uploadState.doneBytes || 0,
            totalBytes: file.size,
            message: "已取消上传，Agent 上的临时分片已经清理。",
          });
          return;
        }
        renderTransfer({
          status: "failed",
          badge: "失败",
          title: "上传失败",
          target: `${node.name || node.id}${uploadRoute?.label ? " / " + uploadRoute.label : ""}`,
          totalBytes: file.size,
          message: String(error?.message || "").includes("Failed to fetch")
            ? `公网连接失败：浏览器无法访问 ${uploadRoute?.url || target?.upload_url || "Agent 公网上传地址"}。请检查云防火墙和 VPS 防火墙是否放行 TCP 8787。`
            : friendlyError(error, "上传失败"),
        });
      } finally {
        delete refs.uploadBtn.dataset.busy;
        refs.uploadBtn.disabled = false;
        refs.cancelUploadBtn.disabled = true;
        if (activeUpload === uploadState) activeUpload = null;
        updatePrimaryActionStates();
      }
    }

    async function checkUpdates({ silent = false } = {}) {
      if (!silent) refs.updateBox.textContent = "正在检查 GitHub...";
      const resp = await fetch("/api/github/check", { method: "POST", headers: authHeaders() });
      const data = await resp.json();
      if (!silent) refs.updateBox.textContent = renderGithubUpdateSummary(data);
      return data;
    }

    function renderGithubUpdateSummary(data) {
      if (!data?.ok) {
        return [
          "GitHub 更新检查失败",
          data?.message || data?.fetch?.stderr || data?.fetch?.stdout || "请检查 VPS 到 GitHub 的网络连接。",
        ].join("\n\n");
      }
      return [
        data.has_updates ? "发现 GitHub 新版本" : "当前已经是最新版本",
        "",
        `当前版本：${data.local_label || data.local || "--"}`,
        `最新版本：${data.remote_label || data.remote || "--"}`,
        `落后提交：${data.behind_count ?? "--"}`,
        `本地超前：${data.ahead_count ?? "--"}`,
        data.diff_stat ? `\n更新内容：\n${data.diff_stat}` : "",
      ].filter(Boolean).join("\n");
    }

    async function promptHubUpgrade(data, { manual = false } = {}) {
      refs.updateBox.textContent = renderGithubUpdateSummary(data);
      if (!data?.ok) {
        if (manual) {
          await showChoiceDialog({
            title: "GitHub 更新检查失败",
            subtitle: "VPS 无法完成版本检查",
            icon: "!",
            message: data?.message || data?.fetch?.stderr || "请检查 VPS 到 GitHub 的网络连接。",
            choices: [{ label: "显示安装命令", value: "commands" }],
          }).then((choice) => {
            if (choice === "commands") return showInstallCommands();
          });
        }
        return;
      }
      if (!data.has_updates) {
        uiMessage("当前 Hub 已经是 GitHub 最新版本。");
        if (manual) {
          await showChoiceDialog({
            title: "已经是最新版本",
            subtitle: data.local_label || "main 已同步",
            icon: "✓",
            message: "当前 Hub 与 GitHub main 一致，不需要升级。",
            choices: [{ label: "知道了", value: "ok", className: "primary" }],
          });
        }
        return;
      }
      const choice = await showChoiceDialog({
        title: "发现 GitHub 新版本",
        subtitle: data.remote_label || "main 有更新",
        icon: "↻",
        message: [
          `当前版本：${data.local_label || data.local || "--"}`,
          `最新版本：${data.remote_label || data.remote || "--"}`,
          data.behind_count ? `落后提交：${data.behind_count}` : "",
          "",
          "是否现在升级当前 Hub？VPS 会在后台从 GitHub main 拉取最新代码并重启 Hub 服务；不需要你登录 VPS。",
        ].filter(Boolean).join("\n"),
        choices: [
          { label: "确认升级到最新版", value: "upgrade", className: "primary" },
          { label: "显示安装命令", value: "commands" },
          { label: "稍后再说", value: "skip" },
        ],
      });
      if (choice === "upgrade") await upgradeCurrentHubFromPrompt(data);
      if (choice === "commands") await showInstallCommands();
    }

    async function checkUpdatesAndPrompt() {
      refs.checkUpdatesBtn.dataset.busy = "1";
      refs.checkUpdatesBtn.disabled = true;
      uiMessage("VPS 正在向 GitHub 检查最新版本。");
      try {
        const data = await checkUpdates({ silent: false });
        await promptHubUpgrade(data, { manual: true });
      } catch (error) {
        refs.updateBox.textContent = friendlyError(error, "GitHub 更新检查失败");
      } finally {
        delete refs.checkUpdatesBtn.dataset.busy;
        refs.checkUpdatesBtn.disabled = false;
      }
    }

    async function latestInstallCommands() {
      const resp = await fetch(`/api/install-commands?_=${Date.now()}`, { cache: "no-store" });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.message || "无法读取安装命令");
      return data;
    }

    function renderInstallCommands(data) {
      refs.updateBox.textContent = [
        `来源：${data.source || "--"}`,
        data.source_url ? `清单：${data.source_url}` : "",
        "",
        "Hub 一键安装/升级：",
        data.hub || "--",
        "",
        "Agent 一键安装/升级：",
        data.agent || "--",
        "",
        data.unified ? `统一安装入口：\n${data.unified}` : "",
      ].filter(Boolean).join("\n");
    }

    async function showInstallCommands() {
      refs.updateBox.textContent = "正在从 GitHub 读取一键安装/升级命令...";
      try {
        const data = await latestInstallCommands();
        renderInstallCommands(data);
        return data;
      } catch (error) {
        refs.updateBox.textContent = friendlyError(error, "安装命令读取失败");
        return null;
      }
    }

    async function copyInstallCommand(kind) {
      const data = await showInstallCommands();
      const command = data?.[kind] || "";
      if (!command) return;
      const label = kind === "hub" ? "Hub" : "Agent";
      try {
        await navigator.clipboard.writeText(command);
        log(`${label} GitHub 一键安装/升级命令已复制`);
      } catch (_) {
        refs.updateBox.textContent = `${label} 命令如下，请手动复制：\n\n${command}`;
      }
    }

    async function upgradeCurrentHubFromPrompt(checkData) {
      refs.updateBox.textContent = "正在提交后台升级任务...";
      uiMessage("正在让 VPS 后台升级 Hub，请不要重复点击。");
      const resp = await fetch("/api/upgrade", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({}) });
      const data = await resp.json();
      refs.updateBox.textContent = data.ok
        ? [
            "升级任务已提交",
            "",
            `当前版本：${checkData?.local_label || checkData?.local || "--"}`,
            `目标版本：${checkData?.remote_label || checkData?.remote || "--"}`,
            `后台任务：${data.result?.unit || "--"}`,
            "",
            "VPS 会自动从 GitHub main 拉取最新代码并重启 Hub。页面可能会短暂断开，稍后刷新即可。",
          ].join("\n")
        : [
            "升级任务提交失败",
            "",
            data.message || resp.statusText || "请检查当前 Hub 是否为 systemd + Git 安装。",
          ].join("\n");
      uiMessage(data.ok ? "升级任务已提交，Hub 重启后刷新页面即可。" : "升级任务提交失败，请查看更新模块详情。");
      log(data.ok ? "当前 Hub GitHub 升级任务已提交" : `当前 Hub 升级失败：${data.message || resp.statusText}`);
    }

    async function checkDailyGithubUpdates() {
      const key = "streamHubLastGithubAutoCheckAt";
      const now = Date.now();
      const last = Number(localStorage.getItem(key) || 0);
      if (last && now - last < 24 * 60 * 60 * 1000) return;
      let data = null;
      try {
        data = await checkUpdates({ silent: true });
        localStorage.setItem(key, String(now));
      } catch (error) {
        log(friendlyError(error, "每日 GitHub 更新检查失败"));
        return;
      }
      if (!data?.ok || !data.has_updates) return;
      await promptHubUpgrade(data);
    }

    async function showPolicy() {
      refs.updateBox.textContent = "Loading upload policy...";
      const resp = await fetch("/api/policy");
      const data = await resp.json();
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
    }

    async function showAudit() {
      refs.updateBox.textContent = "Loading push audit...";
      const resp = await fetch("/api/push-audit?limit=20");
      const data = await resp.json();
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
    }

    async function showTailscaleStatus() {
      setTailscaleWizardOpen(true);
      setTailscaleStep("verify", "running");
      setTailscaleLog("正在读取 Tailscale 状态...");
      const resp = await fetch("/api/tailscale/status");
      const data = await resp.json();
      setTailscaleStep("verify", data.ok ? "done" : "fail");
      setTailscaleLog(data);
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
    }

    function setTailscaleWizardOpen(open) {
      refs.tailscaleWizardModal.classList.toggle("open", open);
      refs.tailscaleWizardModal.setAttribute("aria-hidden", open ? "false" : "true");
      if (open) loadLatestAgentInstallCommand();
    }

    async function loadLatestAgentInstallCommand() {
      refs.agentInstallCommand.textContent = "正在从 GitHub 获取最新一键安装命令...";
      try {
        const resp = await fetch(`/api/install-commands?_=${Date.now()}`, { cache: "no-store" });
        const data = await resp.json();
        if (!resp.ok || !data.agent) throw new Error(data.message || "命令清单无效");
        refs.agentInstallCommand.textContent = data.agent;
        refs.agentInstallCommand.title = data.source === "github" ? "已与 GitHub 同步" : "GitHub 不可用，当前显示本地备用命令";
      } catch (error) {
        refs.agentInstallCommand.textContent = "无法读取安装命令，请检查 Hub 到 GitHub 的网络连接。";
      }
    }

    function tailscaleStatusLines(data, fallback = "Tailscale 状态已更新") {
      if (typeof data === "string") return [{ text: data }];
      const ok = Boolean(data?.ok);
      const lines = [{ text: ok ? (data.message || fallback) : (data?.message || "操作失败"), tone: ok ? "done" : "fail" }];
      if (data?.installed === false) lines.push({ text: "当前机器还没有安装 Tailscale。", tone: "fail" });
      if (data?.backend_state) lines.push({ label: "运行状态", text: data.backend_state });
      const self = data?.self || data?.status?.self || {};
      const tailscaleIps = self.tailscale_ips || data?.tailscale_ips || data?.status?.tailscale_ips || [];
      if (tailscaleIps.length) lines.push({ label: "本机 Tailscale IP", text: tailscaleIps.join(" / "), tone: "done" });
      if (self.dns_name) lines.push({ label: "Tailnet 名称", text: self.dns_name });
      const peers = data?.peers ?? data?.status?.peers ?? null;
      if (Array.isArray(peers)) {
        const onlinePeers = peers.filter((peer) => peer?.online === true).length;
        const offlinePeers = peers.length - onlinePeers;
        lines.push({ label: "Tailnet 设备", text: `${peers.length} 台（在线 ${onlinePeers} / 离线 ${offlinePeers}）`, tone: onlinePeers ? "done" : "" });
      }
      if (data?.node_id && data?.base_url) lines.push({ label: "Agent 连接", text: `${data.node_id} -> ${data.base_url}`, tone: "done" });
      if (data?.previous_base_url) lines.push({ label: "原地址已保留", text: data.previous_base_url });
      const detail = data?.error || data?.result?.stderr || data?.result?.message || data?.precheck?.message || "";
      if (!ok && detail) lines.push({ label: "失败原因", text: String(detail).slice(0, 260), tone: "fail" });
      if (ok && data?.node_id && data?.base_url) {
        lines.push({ label: "连接完成", text: "Agent 已在线并保存，节点列表已刷新；现在可以关闭此窗口。", tone: "done" });
      } else {
        lines.push({ label: "下一步", text: ok ? "请输入 Agent 的 100.x 地址进行连接。" : "请按提示修复后重试。" });
      }
      return lines;
    }

    function setTailscaleLog(value, fallback = "Tailscale 状态已更新") {
      const lines = tailscaleStatusLines(value, fallback);
      refs.tailscaleWizardLog.innerHTML = lines.map((line) => {
        const cls = line.tone === "fail" ? " fail" : line.tone === "done" ? " done" : "";
        const label = line.label ? `<strong>${escapeHtml(line.label)}：</strong>` : "";
        return `<div class="wizard-status-line${cls}">${label}${escapeHtml(line.text || "")}</div>`;
      }).join("");
    }

    function setTailscaleStep(step, state) {
      const el = refs.tailscaleWizardModal.querySelector(`[data-tailscale-step="${step}"]`);
      if (!el) return;
      el.classList.remove("done", "fail");
      if (state === "done" || state === "fail") el.classList.add(state);
    }

    function setTailscaleBusy(busy) {
      [refs.tailscaleUseExistingIpBtn]
        .forEach((button) => { button.disabled = busy; });
    }

    function renderTailscaleNodeOptions() {
      return;
    }

    async function runTailscaleStep(step, label, action) {
      setTailscaleWizardOpen(true);
      setTailscaleBusy(true);
      setTailscaleStep(step, "running");
      setTailscaleLog(`${label}...`);
      try {
        const data = await action();
        setTailscaleStep(step, data.ok ? "done" : "fail");
        setTailscaleLog(data);
        refs.updateBox.textContent = JSON.stringify(data, null, 2);
        return data;
      } catch (error) {
        const data = { ok: false, message: friendlyError(error, `${label}失败`) };
        setTailscaleStep(step, "fail");
        setTailscaleLog(data);
        refs.updateBox.textContent = JSON.stringify(data, null, 2);
        return data;
      } finally {
        setTailscaleBusy(false);
      }
    }

    async function precheckTailscale() {
      return runTailscaleStep("precheck", "正在检查 Tailscale 安装环境", async () => {
        const resp = await fetch("/api/tailscale/precheck");
        return resp.json();
      });
    }

    async function installTailscale() {
      return runTailscaleStep("install", "正在安装或修复 Tailscale", async () => {
        const resp = await fetch("/api/tailscale/install", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({}),
        });
        return resp.json();
      });
    }

    async function connectTailscale() {
      const auth_key = refs.tailscaleAuthInput.value.trim();
      const hostname = refs.tailscaleHostInput.value.trim() || "stream-control-hub";
      if (!auth_key) {
        setTailscaleWizardOpen(true);
        setTailscaleLog("请输入一次性 Tailscale auth key。");
        return;
      }
      const data = await runTailscaleStep("connect", "正在使用 auth key 登录 Tailscale", async () => {
        const resp = await fetch("/api/tailscale/connect", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ auth_key, hostname, ssh: false, accept_routes: true }),
        });
        return resp.json();
      });
      if (data.ok) {
        refs.tailscaleAuthInput.value = "";
        setTailscaleStep("verify", data.status?.ok ? "done" : "fail");
      }
    }

    async function verifyTailscale() {
      return showTailscaleStatus();
    }

    async function connectExistingTailscaleIp() {
      const tailscale_ip = refs.tailscaleExistingIpInput.value.trim();
      if (!tailscale_ip) {
        setTailscaleWizardOpen(true);
        setTailscaleLog("请输入已有的 Tailscale IP，例如 100.x.x.x。");
        return;
      }
      const data = await runTailscaleStep("verify", "正在检查同一 Tailnet、Agent 服务并自动配对", async () => {
        const resp = await fetch("/api/tailscale/connect-existing-ip", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ tailscale_ip }),
        });
        return resp.json();
      });
      if (data.ok) {
        if (data.hub_only) {
          if (data.node_id) rememberSelectedNode(data.node_id);
          log(`Hub-only 节点：${data.node_id || data.hub_url || tailscale_ip}，Agent 当前关闭`);
          await refreshAll();
          setTailscaleLog(`检测结果：Hub 在线，Agent 已关闭。\n\n${data.message || ""}\n\nHub 地址：${data.hub_url || "--"}`);
          return;
        }
        rememberSelectedNode(data.node_id || selectedNodeId || "");
        log(`已连接 ${data.node_id} 到 ${data.base_url}`);
        await refreshAll();
        alert("连接成功，请转控制台进行推流设置。");
        setTailscaleWizardOpen(false);
        refs.smartStartBtn.scrollIntoView({ behavior: "smooth", block: "center" });
        refs.smartStartBtn.focus({ preventScroll: true });
      }
    }

    async function copyAgentInstallCommand() {
      const command = refs.agentInstallCommand.textContent.trim();
      try {
        await navigator.clipboard.writeText(command);
        refs.copyAgentInstallBtn.textContent = "已复制";
      } catch (_) {
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(refs.agentInstallCommand);
        selection.removeAllRanges();
        selection.addRange(range);
        refs.copyAgentInstallBtn.textContent = "请按 Ctrl+C 复制";
      }
      window.setTimeout(() => { refs.copyAgentInstallBtn.textContent = "复制一键安装命令"; }, 1800);
    }

    async function pushSelectedMedia(explicitTargetNodeIds = null) {
      const sourceNode = nodes.find((item) => String(item.id) === String(selectedMediaNodeId())) || selectedNode();
      const requestedTargets = Array.isArray(explicitTargetNodeIds) ? explicitTargetNodeIds : selectedNodeIds();
      const target_node_ids = requestedTargets.filter((id) => String(id) !== String(sourceNode?.id || ""));
      const media = selectedMediaPath() || selectedMediaName();
      const targetLabel = target_node_ids
        .map((id) => nodes.find((item) => String(item.id) === String(id))?.name || id)
        .join(", ");
      const sourceLabel = sourceNode?.name || sourceNode?.id || "--";
      if (!sourceNode?.id || !target_node_ids.length || !media) {
        renderTransfer({
          status: "failed",
          badge: "失败",
          title: "共享未开始",
          message: "请选择源 Agent 的一个服务器视频，并勾选至少一个其他 Agent。",
        });
        return;
      }
      if (refs.pushSelectedBtn) refs.pushSelectedBtn.disabled = true;
      renderTransfer({
        status: "running",
        badge: "共享中",
        title: `共享到 ${targetLabel}`,
        source: sourceLabel,
        target: targetLabel,
        message: `正在创建共享任务：${media}`,
      });
      try {
        const resp = await fetch("/api/media/share", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ source_node_id: sourceNode.id, target_node_ids, media }),
        });
        const first = await resp.json().catch(() => ({ ok: false, message: resp.statusText }));
        if (!resp.ok || !first.ok || !first.task_id) {
          renderTransfer({
            status: "failed",
            badge: "失败",
            title: "共享启动失败",
            source: sourceLabel,
            target: targetLabel,
            message: friendlyError(first.message || first.error || "Hub 未能创建共享任务"),
          });
          return false;
        }
        let last = first;
        let completed = false;
        while (true) {
          renderTransfer({
            status: last.status === "done" ? "done" : last.status === "failed" ? "failed" : "running",
            badge: last.status === "done" ? "完成" : last.status === "failed" ? "失败" : "共享中",
            title: last.status === "done" ? "共享完成" : last.status === "failed" ? "共享失败" : `共享到 ${targetLabel}`,
            source: sourceLabel,
            target: targetLabel,
            routeLabel: last.route_label || "公网直连（禁止内网回退）",
            percent: last.percent || 0,
            doneBytes: last.done_bytes || 0,
            totalBytes: last.total_bytes || 0,
            currentBps: last.current_bps || 0,
            averageBps: last.average_bps || 0,
            etaSeconds: last.eta_seconds || 0,
            message: last.status === "failed"
              ? friendlyError(last.error || last.message || "共享失败")
              : last.status === "done"
                ? `${media} 已共享到 ${targetLabel}。`
                : (last.message || "正在共享，请稍候。"),
          });
          if (last.status === "done") {
            completed = true;
            await refreshAll();
            break;
          }
          if (last.status === "failed") {
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, 1000));
          const statusResp = await fetch(`/api/media/share/status/${encodeURIComponent(first.task_id)}`);
          last = await statusResp.json().catch(() => ({ ok: false, status: "failed", message: statusResp.statusText }));
          if (!statusResp.ok && last.status !== "failed") {
            last = { ...last, status: "failed", message: last.message || "无法读取共享进度" };
          }
        }
        return completed;
      } catch (error) {
        renderTransfer({
          status: "failed",
          badge: "失败",
          title: "共享失败",
          source: sourceLabel,
          target: targetLabel,
          message: friendlyError(error, "共享失败"),
        });
        return false;
      } finally {
        if (refs.pushSelectedBtn) refs.pushSelectedBtn.disabled = false;
      }
    }

    async function previewTune() {
      if (refs.commandAdvanced) refs.commandAdvanced.open = true;
      const payload = streamPayload({ includeKey: false });
      if (!payload.node_id || !payload.video_path) {
        refs.tuneBox.textContent = "请先选择右侧节点，并选择该节点服务器视频。";
        return;
      }
      refs.previewTuneBtn.dataset.busy = "1";
      refs.previewTuneBtn.disabled = true;
      uiMessage("正在分析节点和视频，生成推荐参数。");
      refs.tuneBox.textContent = "正在让节点分析 CPU / 内存 / 网络 / 视频源...";
      try {
        const data = await postNodeAction("/api/nodes/stream/recommend", payload);
        lastTuneRecommendation = data;
        if (data.ok) {
          renderTuneRecommendation(data);
        } else {
          refs.tuneBox.textContent = data.message || "智能调优失败";
        }
      } finally {
        delete refs.previewTuneBtn.dataset.busy;
        refs.previewTuneBtn.disabled = false;
        updatePrimaryActionStates();
      }
    }

    function applyLastTune() {
      if (refs.commandAdvanced) refs.commandAdvanced.open = true;
      if (!lastTuneRecommendation?.ok) {
        refs.tuneBox.textContent = "还没有可应用的推荐参数，请先点“预览智能调优”。";
        return;
      }
      applyTuneRecommendation(lastTuneRecommendation);
      renderTuneRecommendation(lastTuneRecommendation);
      log("已应用智能调优推荐参数到开播表单");
    }

    async function ensureSmartStartMedia(payload) {
      if (!payload.library_media_name || payload.media_local) return { ok: true, copied: false };
      if (!payload.source_node_id) throw new Error(`媒体库没有可用源副本：${payload.library_media_name}`);
      const copySourceLabel = nodes.find((item) => String(item.id) === String(payload.source_node_id))?.name || payload.source_node_id;
      refs.tuneBox.textContent = `目标节点没有 ${payload.library_media_name}，正在创建自动复制任务...`;
      const task = await postJson("/api/media/share", {
        source_node_id: payload.source_node_id,
        target_node_ids: [payload.node_id],
        media: payload.library_media_name,
      });
      if (!task.ok || !task.task_id) throw new Error(task.message || "自动复制任务创建失败");
      const deadline = Date.now() + 30 * 60 * 1000;
      while (Date.now() < deadline) {
        const response = await fetch(`/api/media/share/status/${encodeURIComponent(task.task_id)}`);
        const status = await response.json();
        const percent = Math.max(0, Math.min(100, Number(status.percent || 0)));
        refs.tuneBox.textContent = [
          `正在把 ${payload.library_media_name} 复制到 ${selectedNode()?.name || payload.node_id}`,
          `进度：${percent.toFixed(1)}%`,
          status.average_bps ? `平均速度：${fmtBytes(status.average_bps)}/s` : "正在建立传输...",
          status.message || "请稍候，复制完成后会自动启动推流。",
        ].join("\n");
        renderTransfer({
          status: status.status === "failed" ? "failed" : "uploading",
          badge: status.status === "failed" ? "失败" : "自动复制",
          title: `开播前复制 · ${payload.library_media_name}`,
          source: copySourceLabel,
          target: selectedNode()?.name || payload.node_id,
          percent,
          doneBytes: status.done_bytes || 0,
          totalBytes: status.total_bytes || 0,
          currentBps: status.current_bps || 0,
          averageBps: status.average_bps || 0,
          etaSeconds: status.eta_seconds || 0,
          message: status.message || "复制完成后自动开播",
        });
        if (status.status === "done") return { ok: true, copied: true, task_id: task.task_id };
        if (status.status === "failed" || status.ok === false) throw new Error(status.message || "自动复制失败");
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
      throw new Error("自动复制等待超时，请检查节点网络后重试");
    }

    async function smartStart() {
      const payload = streamPayload({ includeKey: true });
      const relayMode = payload.stream_output_mode === "local_relay";
      const youtubeApiMode = payload.stream_output_mode === "youtube_api";
      const targetMissing = (!relayMode && !youtubeApiMode && !payload.stream_key)
        || (youtubeApiMode && !payload.youtube_stream_id);
      if (!payload.node_id || !payload.video_path || targetMissing) {
        const missing = [];
        if (!payload.node_id) missing.push("目标 Agent");
        if (!payload.video_path) missing.push("服务器视频");
        if (!relayMode && !youtubeApiMode && !payload.stream_key) missing.push("直播码");
        if (youtubeApiMode && !payload.youtube_stream_id) missing.push("YouTube 直播流");
        refs.tuneBox.textContent = `无法启动：还缺少 ${missing.join("、")}。`;
        return;
      }
      refs.smartStartBtn.dataset.busy = "1";
      refs.smartStartBtn.disabled = true;
      uiMessage("Smart Start 正在启动，会先确认视频和推流参数。");
      refs.tuneBox.textContent = "正在启动 Smart Start：会在选中节点停止重复推流，并启动一个干净 FFmpeg。";
      try {
        const mediaResult = await ensureSmartStartMedia(payload);
        if (mediaResult.copied) {
          refs.tuneBox.textContent = "媒体复制完成，正在刷新节点并启动 FFmpeg...";
          await refreshAll();
        }
        if (!lastTuneRecommendation?.ok) {
          const tune = await postNodeAction("/api/nodes/stream/recommend", { ...payload, stream_key: "" });
          if (tune.ok) {
            applyTuneRecommendation(tune);
            renderTuneRecommendation(tune);
          }
        }
        const startPayload = streamPayload({ includeKey: true });
        if (lastTuneRecommendation?.recommendation) {
          Object.assign(startPayload, lastTuneRecommendation.recommendation);
        }
        const data = await postNodeAction("/api/nodes/stream/start", startPayload);
        if (data.ok) refs.streamKeyInput.value = "";
        refs.tuneBox.textContent = JSON.stringify({
          ok: data.ok,
          node_id: data.node_id,
          message: data.message,
          started_pid: data.result?.started_pid,
          duplicate_processes: data.result?.duplicate_processes,
        }, null, 2);
        await refreshAll();
      } catch (error) {
        const message = friendlyError(error, "Smart Start 启动失败");
        refs.tuneBox.textContent = `Smart Start 失败：${message}`;
        renderTransfer({ status: "failed", badge: "失败", title: "Smart Start 未启动", message });
        log(`Smart Start 失败：${message}`);
      } finally {
        delete refs.smartStartBtn.dataset.busy;
        refs.smartStartBtn.disabled = false;
        updatePrimaryActionStates();
      }
    }

    async function postNodeAction(path, payload) {
      const resp = await fetch(path, {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
      return data;
    }

    async function postJson(path, payload) {
      const resp = await fetch(path, {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      const data = await resp.json().catch(() => ({ ok: false, message: resp.statusText }));
      if (!resp.ok && data.ok !== false) data.ok = false;
      return data;
    }

    async function handleMediaAction(action, row) {
      const nodeId = row.dataset.nodeId || selectedNodeId;
      const mediaName = row.dataset.mediaName || "";
      const videoPath = row.dataset.videoPath || mediaName;
      const node = nodes.find((item) => String(item.id) === String(nodeId));
      const nodeLabel = node?.name || nodeId;
      if (action === "property") {
        showMediaProperties(row);
        return;
      }
      if (action === "inspect") {
        const input = row.querySelector("[data-media-check]");
        if (input) input.checked = true;
        renderTransfer({
          status: "done",
          badge: "详情",
          title: "Agent 文件详情",
          target: nodeLabel,
          percent: 100,
          doneBytes: Number(row.dataset.size || 0),
          totalBytes: Number(row.dataset.size || 0),
          message: `${mediaName} | ${row.dataset.modifiedLabel || "--"} | ${videoPath}`,
        });
        return;
      }
      if (action === "use") {
        const input = row.querySelector("[data-media-check]");
        if (input) input.checked = true;
        const targetNode = selectedNode();
        renderTransfer({
          status: "done",
          badge: "已选用",
          title: "已从媒体库选用视频",
          target: targetNode?.name || targetNode?.id || "尚未选择开播节点",
          percent: 100,
          message: `${mediaName} 已放入开播选择；若当前节点没有副本，Smart Start 会先从其他节点公网复制。`,
        });
        renderMedia();
        renderStreamControls();
        const option = [...refs.streamVideoSelect.options].find((item) => item.dataset.libraryName === mediaName);
        if (option) option.selected = true;
        updatePrimaryActionStates();
        return;
      }
      if (action === "rename") {
        const nextName = prompt("输入新的文件名，保留 .mp4/.mov/.mkv/.m4v/.webm 后缀：", mediaName);
        if (!nextName || nextName === mediaName) return;
        const data = await postJson("/api/nodes/media/rename", {
          node_id: nodeId,
          media: videoPath,
          new_name: nextName,
        });
        renderTransfer({
          status: data.ok ? "done" : "failed",
          badge: data.ok ? "完成" : "失败",
          title: data.ok ? "重命名完成" : "重命名失败",
          target: nodeLabel,
          percent: data.ok ? 100 : 0,
          message: data.ok ? `${mediaName} 已改名为 ${data.name || nextName}。` : friendlyError(data.message || data.error || "重命名失败"),
        });
        await refreshAll();
        return;
      }
      if (action === "delete") {
        if (!confirm(`确认删除 ${mediaName}？\n\n只删除当前 Agent 上的这个视频，不会影响其他 Agent。`)) return;
        const data = await postJson("/api/nodes/media/delete", {
          node_id: nodeId,
          media: videoPath,
        });
        renderTransfer({
          status: data.ok ? "done" : "failed",
          badge: data.ok ? "完成" : "失败",
          title: data.ok ? "删除完成" : "删除失败",
          target: nodeLabel,
          percent: data.ok ? 100 : 0,
          message: data.ok ? `${data.name || mediaName} 已从 ${nodeLabel} 删除。` : friendlyError(data.message || data.error || "删除失败"),
        });
        await refreshAll();
      }
    }

    function showMediaProperties(row) {
      const mediaName = row.dataset.mediaName || "";
      const resource = mediaResourceByName(mediaName);
      const groupName = mediaGroupName(resource?.group_id || row.dataset.groupId || "");
      const copies = resource?.copies || [];
      const copyLines = copies.length
        ? copies.map((copy) => `- ${copy.node_name || copy.node_id}: ${copy.video_path || mediaName}（${copy.last_used_label || "从未开播"}）`).join("\n")
        : `- ${row.dataset.copyNames || row.dataset.nodeId || "未知节点"}`;
      alert([
        `文件：${mediaName}`,
        `分组：${groupName}`,
        `大小：${fmtBytes(Number(row.dataset.size || resource?.size || 0))}`,
        `上传时间：${row.dataset.modifiedLabel || resource?.modified_label || "--"}`,
        `副本数：${copies.length || row.dataset.copyCount || 1}`,
        "",
        "所在节点 / 路径：",
        copyLines,
      ].join("\n"));
    }

    async function moveMediaToGroup(row, groupId) {
      const filename = row.dataset.mediaName || "";
      const targetGroupId = groupId === "__ungrouped__" ? "" : groupId;
      if (!filename) return;
      const targetName = targetGroupId ? mediaGroupName(targetGroupId) : "未分组";
      const data = await postJson("/api/media-library/assign", { filename, group_id: targetGroupId });
      renderTransfer({
        status: data.ok ? "done" : "failed",
        badge: data.ok ? "完成" : "失败",
        title: data.ok ? "分组移动完成" : "分组移动失败",
        target: targetName,
        percent: data.ok ? 100 : 0,
        message: data.ok ? `${filename} 已移动到分组：${targetName}` : friendlyError(data.message || "移动分组失败"),
      });
      if (data.ok) await refreshAll();
    }

    function selectMediaRow(row) {
      document.querySelectorAll("[data-media-row].selected").forEach((item) => item.classList.remove("selected"));
      row.classList.add("selected");
      const input = row.querySelector("[data-media-check]");
      if (input) input.checked = true;
      updatePrimaryActionStates();
    }

    function hideMediaMenu() {
      refs.mediaContextMenu.classList.remove("open");
      refs.mediaContextMenu.style.left = "";
      refs.mediaContextMenu.style.top = "";
      contextMediaRow = null;
    }

    function showMediaMenu(event, row) {
      event.preventDefault();
      selectMediaRow(row);
      contextMediaRow = row;
      const sourceNodeId = String(row.dataset.nodeId || "");
      const targets = nodes.filter((node) => node.roles?.agent?.enabled && String(node.id) !== sourceNodeId);
      const currentGroupId = row.dataset.groupId || "";
      const groupTargets = [
        { id: "__ungrouped__", name: "未分组" },
        ...(mediaLibrary.groups || []),
      ].filter((group) => String(group.id === "__ungrouped__" ? "" : group.id) !== String(currentGroupId));
      const targetButtons = (action) => targets.length
        ? targets.map((node) => `<button data-media-menu-action="${action}" data-target-node-id="${escapeHtml(node.id)}">${escapeHtml(node.name || node.id)}</button>`).join("")
        : `<button disabled>没有其他在线节点</button>`;
      refs.mediaGroupTargets.innerHTML = groupTargets.length
        ? groupTargets.map((group) => `<button data-media-menu-action="move-group" data-target-group-id="${escapeHtml(group.id)}">${escapeHtml(group.name)}</button>`).join("")
        : `<button disabled>已经在唯一分组</button>`;
      refs.mediaSendTargets.innerHTML = targetButtons("send-node");
      refs.mediaMoveTargets.innerHTML = targetButtons("move-node");
      refs.mediaContextMenu.classList.add("open");
      const menuWidth = refs.mediaContextMenu.offsetWidth || 160;
      const menuHeight = refs.mediaContextMenu.offsetHeight || 170;
      const left = Math.min(event.clientX, window.innerWidth - menuWidth - 8);
      const top = Math.min(event.clientY, window.innerHeight - menuHeight - 8);
      refs.mediaContextMenu.style.left = `${Math.max(8, left)}px`;
      refs.mediaContextMenu.style.top = `${Math.max(8, top)}px`;
    }

    async function handleNodeAction(action, nodeId) {
      const node = nodes.find((item) => String(item.id) === String(nodeId));
      const nodeName = node?.name || nodeId;
      if (action === "stop-stream") {
        if (!confirm(`确认停止 ${nodeName} 的推流？`)) {
          return;
        }
        log(`请求停止推流：${nodeName}`);
        const data = await postNodeAction("/api/nodes/stop-stream", { node_id: nodeId });
        log(data.ok ? `推流已停止：${nodeName}` : `停止推流失败：${data.message || nodeName}`);
        await refreshAll();
        return;
      }
      if (action === "restart-stream") {
        if (!confirm(`确认请求重启 ${nodeName} 的推流？\n\n保护规则：不会清空直播码；如果节点没有安全重启接口，总控台会拒绝执行。`)) {
          return;
        }
        log(`请求重启推流：${nodeName}`);
        const data = await postNodeAction("/api/nodes/restart-stream", { node_id: nodeId });
        log(data.ok ? `推流重启已执行：${nodeName}` : `推流重启被保护规则拦截：${data.message || nodeName}`);
        await refreshAll();
        return;
      }
      if (action === "reboot-vps") {
        const confirmText = `REBOOT ${nodeId}`;
        const typed = prompt(`重启 VPS 是危险操作。\n请输入 ${confirmText} 才会继续：`);
        if (typed !== confirmText) {
          log(`已取消重启 VPS：${nodeName}`);
          return;
        }
        log(`请求重启 VPS：${nodeName}`);
        const data = await postNodeAction("/api/nodes/reboot", { node_id: nodeId, confirm_text: typed });
        log(data.ok ? `VPS 重启已提交：${nodeName}` : `VPS 重启被保护规则拦截：${data.message || nodeName}`);
        await refreshAll();
        return;
      }
    }

    let roleSettingsNodeId = "";

    function setRoleSettingsOpen(open, nodeId = "") {
      roleSettingsNodeId = open ? String(nodeId) : "";
      refs.roleSettingsModal.classList.toggle("open", open);
      refs.roleSettingsModal.setAttribute("aria-hidden", open ? "false" : "true");
      if (!open) return;
      const node = nodes.find((item) => String(item.id) === roleSettingsNodeId);
      if (!node) return setRoleSettingsOpen(false);
      refs.roleSettingsTitle.textContent = `${node.name || node.id} · 角色设置`;
      refs.roleSettingsNameInput.value = node.name || node.id;
      refs.roleSettingsSummary.textContent = "角色维护功能不会直接执行；选择后还需通过保护确认。取消角色默认保留数据。";
      refs.roleSettingsActions.innerHTML = ["agent", "hub"].map((role) => {
        const info = node.roles?.[role] || {};
        const enabled = Boolean(info.enabled);
        const label = role === "hub" ? "Hub" : "Agent";
        return `<div class="role-settings-item">
          <span><strong>${label}</strong><small>当前状态：${enabled ? `已激活 · 版本 ${escapeHtml(info.version || "未识别")}` : "未激活"}</small></span>
          <span class="actions">
            <button class="${enabled ? "" : "primary"}" data-settings-role="${role}" data-role-action="${enabled ? "upgrade-role" : "activate-role"}">${enabled ? `GitHub 升级 ${label}` : `激活 ${label}`}</button>
            ${enabled ? `<button class="danger" data-settings-role="${role}" data-role-action="deactivate-role">取消 ${label}</button>` : ""}
          </span>
        </div>`;
      }).join("");
    }

    function setResourceToolsOpen(open) {
      refs.resourceToolsModal.classList.toggle("open", open);
      refs.resourceToolsModal.setAttribute("aria-hidden", open ? "false" : "true");
    }

    function showChoiceDialog({ title, subtitle = "请选择处理方式。", message = "", icon = "!", choices = [] }) {
      return new Promise((resolve) => {
        refs.choiceTitle.textContent = title || "确认操作";
        refs.choiceSubtitle.textContent = subtitle;
        refs.choiceMessage.textContent = message;
        refs.choiceIcon.textContent = icon;
        refs.choiceActions.innerHTML = choices.map((choice, index) => (
          `<button class="${escapeHtml(choice.className || "")}" data-choice-index="${index}">${escapeHtml(choice.label)}</button>`
        )).join("") + `<button data-choice-cancel>取消</button>`;
        const close = (value) => {
          refs.choiceModal.classList.remove("open");
          refs.choiceModal.setAttribute("aria-hidden", "true");
          refs.choiceActions.onclick = null;
          resolve(value);
        };
        refs.choiceActions.onclick = (event) => {
          const cancel = event.target.closest("[data-choice-cancel]");
          if (cancel) return close(null);
          const button = event.target.closest("[data-choice-index]");
          if (!button) return;
          const choice = choices[Number(button.dataset.choiceIndex)];
          close(choice?.value ?? null);
        };
        refs.choiceModal.classList.add("open");
        refs.choiceModal.setAttribute("aria-hidden", "false");
      });
    }

    async function handleRoleAction(action, role, nodeId, sourceButton) {
      const node = nodes.find((item) => String(item.id) === String(nodeId));
      const nodeName = node?.name || nodeId;
      const roleLabel = role === "hub" ? "Hub" : "Agent";
      if (action === "switch-hub") {
        await switchHubWithFallback(nodeId);
        return;
      }
      const activating = action === "activate-role";
      const deactivating = action === "deactivate-role";
      const roleInfo = node?.roles?.[role] || {};
      const currentStatus = roleInfo.enabled ? `已激活，当前版本 ${roleInfo.version || "未识别"}` : "未激活";
      let migrateBeforeDeactivate = false;
      let directDeactivate = false;
      if (deactivating) {
        const choice = await showChoiceDialog({
          title: `取消 ${roleLabel} 功能`,
          subtitle: nodeName,
          icon: "⚠",
          message: role === "agent"
            ? `${roleLabel} 当前状态：${currentStatus}\n\n请选择处理方式：\n- 先迁移资源再关闭：会把该 Agent 独有视频同步到容量最大的其它 Agent，成功后再关闭 Agent。\n- 直接关闭：只停止 Agent 功能，不迁移资源。`
            : `${roleLabel} 当前状态：${currentStatus}\n\nHub 不保存视频资源。你可以直接关闭 Hub 功能，Agent 不受影响。`,
          choices: role === "agent"
            ? [
                { label: "先迁移资源，再关闭 Agent", value: "migrate", className: "primary" },
                { label: "直接关闭 Agent", value: "direct", className: "danger-soft" },
              ]
            : [
                { label: "直接关闭 Hub", value: "direct", className: "danger-soft" },
              ],
        });
        if (!choice) return;
        migrateBeforeDeactivate = choice === "migrate";
        directDeactivate = choice === "direct";
      }
      const warning = deactivating
        ? `${nodeName} 的 ${roleLabel} 当前状态：${currentStatus}。\n\n是否确认取消 ${roleLabel} 功能？\n\n系统会停止并卸载该角色服务，默认保留配置/视频/节点数据；另一个角色不会被停止。`
        : activating
        ? `${nodeName} 的 ${roleLabel} 当前状态：${currentStatus}。\n\n是否确认激活 ${roleLabel}？\n\n安全提示：将新增并启用独立 systemd 服务，开放 Tailscale ${role === "hub" ? "8788" : "8787"} 端口。现有 ${role === "hub" ? "Agent" : "Hub"} 会继续运行，配置与视频不会删除。`
        : `${nodeName} 的 ${roleLabel} 当前状态：${currentStatus}。\n\n是否确认升级 ${roleLabel}？\n\n系统会从 GitHub main 拉取最新版，只重启该角色，不停止另一个角色。`;
      if (!deactivating && !confirm(warning)) return;
      setRoleSettingsOpen(false);
      if (sourceButton) sourceButton.disabled = true;
      if (migrateBeforeDeactivate) {
        const migrated = await migrateNodeResourcesBeforeAction(nodeId, nodeName);
        if (!migrated) {
          if (sourceButton) sourceButton.disabled = false;
          return;
        }
      }
      const path = deactivating ? `/api/nodes/roles/${role}/deactivate` : activating ? `/api/nodes/roles/${role}/activate` : `/api/nodes/roles/${role}/upgrade`;
      const data = await postNodeAction(path, { node_id: nodeId });
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
      log(data.ok ? `${roleLabel} 任务已提交：${nodeName}` : `${roleLabel} 操作失败：${data.message || nodeName}`);
      if (data.ok) {
        await new Promise((resolve) => setTimeout(resolve, 8000));
        await refreshAll();
      } else if (sourceButton) {
        sourceButton.disabled = false;
      }
    }

    async function saveRoleSettingsName() {
      const name = refs.roleSettingsNameInput.value.replace(/\s+/g, " ").trim().slice(0, 80);
      if (!roleSettingsNodeId || !name) return;
      refs.roleSettingsSaveNameBtn.disabled = true;
      try {
        const data = await postJson("/api/nodes/name", { node_id: roleSettingsNodeId, name });
        if (!data.ok) throw new Error(data.message || "名称保存失败");
        await refreshAll();
        setRoleSettingsOpen(true, roleSettingsNodeId);
        log(`节点名称已更新：${name}`);
      } catch (error) {
        log(friendlyError(error, "节点名称保存失败"));
      } finally {
        refs.roleSettingsSaveNameBtn.disabled = false;
      }
    }

    async function switchHubWithFallback(nodeId) {
      const node = nodes.find((item) => String(item.id) === String(nodeId));
      const fallbackUrl = node?.roles?.hub?.url || "";
      try {
        const data = await postJson("/api/hubs/switch-target", { node_id: nodeId });
        refs.updateBox.textContent = JSON.stringify(data, null, 2);
        if (data.ok && data.url) {
          if (data.fallback) log(`目标 Hub 无响应，自动切换到可用 Hub：${data.node_name || data.node_id || data.url}`);
          window.location.href = data.url;
          return;
        }
        throw new Error(data.message || "没有可用 Hub");
      } catch (error) {
        if (fallbackUrl && !sameOriginUrl(fallbackUrl)) {
          log(friendlyError(error, "Hub 探测失败，尝试直接打开目标地址"));
          window.location.href = fallbackUrl;
          return;
        }
        alert(friendlyError(error, "没有可用 Hub 可切换"));
      }
    }

    async function deleteNodeRecord(nodeId) {
      const node = nodes.find((item) => String(item.id) === String(nodeId));
      const label = node?.name || nodeId;
      if (!nodeId) return;
      const choice = await showChoiceDialog({
        title: "删除节点",
        subtitle: label,
        icon: "⚠",
        message: "请选择处理方式：\n- 先迁移资源再删除：在线节点会把独有视频同步到容量最大的其它 Agent，成功后删除节点记录。\n- 直接删除记录：只从当前 Hub 移除节点，不迁移资源，不删除 VPS 上文件。",
        choices: [
          { label: "先迁移资源，再删除节点", value: "migrate", className: "primary" },
          { label: "直接删除节点记录", value: "direct", className: "danger-soft" },
        ],
      });
      if (!choice) return;
      const data = await postJson("/api/nodes/delete", { node_id: nodeId, migrate_resources: choice === "migrate" });
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
      if (!data.ok) return alert(data.message || "节点删除失败");
      if (data.task_id) {
        log(`节点删除前资源迁移已开始：${label}`);
        renderTransfer({
          status: "running",
          badge: "迁移中",
          title: `正在迁移 ${label} 的资源`,
          source: label,
          target: "容量最大的可用 Agent",
          percent: data.percent || 0,
          message: data.message || "正在迁移资源，完成后会自动删除节点记录。",
        });
        while (true) {
          await new Promise((resolve) => setTimeout(resolve, 2000));
          const statusResp = await fetch(`/api/media/share/status/${encodeURIComponent(data.task_id)}`);
          const status = await statusResp.json();
          refs.updateBox.textContent = JSON.stringify(status, null, 2);
          renderTransfer({
            status: status.status,
            badge: status.status === "done" ? "已删除" : status.status === "failed" ? "失败" : "迁移中",
            title: status.status === "done" ? `节点已删除：${label}` : `正在迁移 ${label} 的资源`,
            source: label,
            target: (status.target_node_ids || []).join("、") || "容量最大的可用 Agent",
            percent: status.percent || 0,
            message: status.message || "",
          });
          if (status.status === "done") break;
          if (status.status === "failed") {
            alert(status.error || status.message || "资源迁移失败，节点记录已保留");
            return;
          }
        }
      }
      if (String(selectedNodeId) === String(nodeId)) rememberSelectedNode("");
      if (String(openResourceNodeId) === String(nodeId)) openResourceNodeId = "";
      log(`节点记录已删除：${label}`);
      await refreshAll();
    }

    async function migrateNodeResourcesBeforeAction(nodeId, label) {
      const data = await postJson("/api/nodes/resources/migrate", { node_id: nodeId });
      refs.updateBox.textContent = JSON.stringify(data, null, 2);
      if (!data.ok) {
        alert(data.message || "资源迁移失败");
        return false;
      }
      if (!data.task_id) {
        log(`无需迁移资源：${label}`);
        return true;
      }
      renderTransfer({
        status: "running",
        badge: "迁移中",
        title: `正在迁移 ${label} 的资源`,
        source: label,
        target: "容量最大的可用 Agent",
        percent: data.percent || 0,
        message: data.message || "正在迁移资源，完成后继续执行操作。",
      });
      while (true) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const statusResp = await fetch(`/api/media/share/status/${encodeURIComponent(data.task_id)}`);
        const status = await statusResp.json();
        refs.updateBox.textContent = JSON.stringify(status, null, 2);
        renderTransfer({
          status: status.status,
          badge: status.status === "done" ? "迁移完成" : status.status === "failed" ? "失败" : "迁移中",
          title: status.status === "done" ? `资源迁移完成：${label}` : `正在迁移 ${label} 的资源`,
          source: label,
          target: (status.target_node_ids || []).join("、") || "容量最大的可用 Agent",
          percent: status.percent || 0,
          message: status.message || "",
        });
        if (status.status === "done") return true;
        if (status.status === "failed") {
          alert(status.error || status.message || "资源迁移失败，操作已取消");
          return false;
        }
      }
    }

    async function transferHubNodes() {
      const target = refs.transferHubUrlInput.value.trim();
      const token = refs.transferHubTokenInput.value.trim();
      if (!target) return alert("请输入新 Hub 地址。");
      if (!confirm(`把当前 Hub 的节点信息转移到：\n${target}\n\n新 Hub 会合并这些节点信息，用来接管控制。是否继续？`)) return;
      refs.transferHubNodesBtn.disabled = true;
      try {
        const data = await postJson("/api/hub-transfer/nodes", { target_hub_url: target, target_token: token });
        refs.updateBox.textContent = JSON.stringify(data, null, 2);
        if (!data.ok) throw new Error(data.message || "控制转移失败");
        log(`Hub 控制转移已完成：${data.imported_count || 0} 个节点 -> ${target}`);
        alert(`转移完成：${data.imported_count || 0} 个节点已导入新 Hub。`);
      } catch (error) {
        const message = friendlyError(error, "Hub 控制转移失败");
        log(message);
        alert(message);
      } finally {
        refs.transferHubNodesBtn.disabled = false;
      }
    }

    async function syncAllHubs() {
      if (!confirm("把当前节点信息同步到所有已激活 Hub？\n\n用于无缝切换控制台；不会删除 VPS 上的任何文件或服务。")) return;
      refs.syncAllHubsBtn.disabled = true;
      try {
        const data = await postJson("/api/hubs/sync", {});
        refs.updateBox.textContent = JSON.stringify(data, null, 2);
        if (!data.ok) throw new Error(data.message || "Hub 同步失败");
        log(`Hub 节点信息同步完成：${data.ok_count || 0}/${data.target_count || 0}`);
        alert(`同步完成：${data.ok_count || 0}/${data.target_count || 0} 个 Hub 已更新。`);
      } catch (error) {
        const message = friendlyError(error, "Hub 同步失败");
        log(message);
        alert(message);
      } finally {
        refs.syncAllHubsBtn.disabled = false;
      }
    }

    async function manageMediaGroup(action) {
      const selected = refs.mediaGroupFilter.value;
      const current = (mediaLibrary.groups || []).find((item) => item.id === selected);
      if (action !== "create" && !current) return alert("请先选择一个分组。");
      if (action === "delete" && !confirm(`删除分组“${current.name}”？视频文件不会被删除，将回到未分组。`)) return;
      const name = action === "delete" ? "" : prompt(action === "create" ? "新分组名称：" : "修改分组名称：", current?.name || "");
      if (action !== "delete" && !name?.trim()) return;
      const data = await postJson("/api/media-groups", { action, group_id: current?.id || "", name: name?.trim() || "" });
      if (!data.ok) return alert(data.message || "分组操作失败");
      await refreshAll();
      if (data.group_id) refs.mediaGroupFilter.value = data.group_id;
      renderMedia();
    }

    function setQuickGroupManageOpen(open) {
      refs.quickGroupManageMenu.hidden = !open;
    }

    async function deleteQuickGroup() {
      const groups = mediaLibrary.groups || [];
      const selected = refs.mediaGroupFilter.value;
      const current = groups.find((item) => item.id === selected) || groups[groups.length - 1];
      if (!current) return alert("当前没有可减少的分组。");
      if (!confirm(`删除分组“${current.name}”？视频文件不会被删除，将回到未分组。`)) return;
      const data = await postJson("/api/media-groups", { action: "delete", group_id: current.id, name: "" });
      if (!data.ok) return alert(data.message || "分组删除失败");
      refs.mediaGroupFilter.value = "";
      resourceTableFilters.group = "";
      await refreshAll();
    }

    async function assignSelectedMediaGroup() {
      const filename = selectedMediaName();
      if (!filename) return alert("请先选择一个视频。");
      const selected = refs.mediaGroupFilter.value;
      const groupId = selected && selected !== "__ungrouped__" ? selected : "";
      const data = await postJson("/api/media-library/assign", { filename, group_id: groupId });
      if (!data.ok) return alert(data.message || "移动分组失败");
      await refreshAll();
    }

    async function cleanupDuplicateMedia() {
      const createdBeforeDays = Number(refs.mediaCleanupAge.value || 30);
      const usageMode = refs.mediaCleanupUsage.value || "any";
      const criteria = {
        created_before_days: createdBeforeDays,
        unused_days: createdBeforeDays,
        usage_mode: usageMode,
      };
      refs.mediaCleanupBtn.disabled = true;
      try {
        const preview = await postJson("/api/media-library/cleanup", { ...criteria, execute: false });
        if (!preview.ok) return alert(preview.message || "清理预览失败");
        if (!preview.candidate_count) {
          return alert("没有符合条件且已通过 SHA-256 验证、保留满 72 小时的重复旧副本。唯一文件不会被系统删除。");
        }
        const details = (preview.candidates || []).slice(0, 10).map((item) => `• ${item.filename} @ ${item.node_name}`).join("\n");
        if (!confirm(`将删除 ${preview.candidate_count} 个重复旧副本，释放约 ${fmtBytes(preview.candidate_bytes)}。\n\n${details}\n\n系统会在删除前再次确认另一份完整副本及 SHA-256；唯一文件绝不删除。是否继续？`)) return;
        const result = await postJson("/api/media-library/cleanup", { ...criteria, execute: true });
        alert(`清理完成：删除 ${result.deleted_count || 0} 个重复旧副本。`);
        await refreshAll();
      } finally {
        refs.mediaCleanupBtn.disabled = false;
      }
    }

    async function renameQuickGroup(groupId, currentName) {
      const nextName = prompt("修改快捷分组名称：", currentName || "");
      if (!nextName || !nextName.trim() || nextName.trim() === currentName) return;
      const data = await postJson("/api/media-groups", {
        action: "rename",
        group_id: groupId,
        name: nextName.trim(),
      });
      if (!data.ok) return alert(data.message || "分组改名失败");
      await refreshAll();
      refs.mediaGroupFilter.value = groupId;
      resourceTableFilters.group = groupId;
      renderMedia();
    }

    async function saveNodeYouTubeProfile(selectEl) {
      const nodeId = selectEl?.dataset?.nodeId || "";
      const profileId = selectEl?.value || "";
      const node = nodes.find((item) => String(item.id) === String(nodeId));
      const previousProfileId = nodeProfileId(node);
      if (!nodeId || !profileId || profileId === previousProfileId) return;
      selectEl.disabled = true;
      try {
        const data = await postJson("/api/nodes/youtube-profile", { node_id: nodeId, profile_id: profileId });
        if (!data.ok) throw new Error(data.message || "Profile 保存失败");
        nodes = nodes.map((item) => {
          if (String(item.id) !== String(nodeId)) return item;
          return {
            ...item,
            youtube_profile_id: data.profile_id || profileId,
            youtube_profile_name: data.profile?.name || profileName(data.profile_id || profileId),
          };
        });
        if (String(selectedNodeId) === String(nodeId) && nodeProfileId(nodes.find((item) => String(item.id) === String(nodeId))) !== selectedYouTubeProfileId()) {
          ensureSelectedNodeForProfile();
        }
        renderNodes();
        renderMedia();
        renderStreamControls();
        renderYouTubeAgentList();
      } catch (error) {
        selectEl.value = previousProfileId;
        alert(error.message || "Profile 保存失败");
      } finally {
        selectEl.disabled = false;
      }
    }

    refs.nodeList.addEventListener("click", (event) => {
      if (event.target.closest("[data-node-profile-select]")) {
        event.stopPropagation();
        return;
      }
      const noteButton = event.target.closest("[data-node-note]");
      if (noteButton) {
        event.preventDefault();
        event.stopPropagation();
        const node = nodes.find((item) => String(item.id) === String(noteButton.dataset.nodeId));
        if (!node) return;
        const current = String(node.note || "");
        const next = prompt(`节点：${node.name || node.id}\n\n完整备注：${current || "（暂无）"}\n\n可在下方编辑备注：`, current);
        if (next === null || next === current) return;
        postJson("/api/nodes/note", { node_id: node.id, note: next }).then(async (data) => {
          if (!data.ok) {
            log(`备注保存失败：${data.message || node.id}`);
            return;
          }
          await refreshAll();
        });
        return;
      }
      const settingsButton = event.target.closest("[data-role-settings]");
      if (settingsButton) {
        event.preventDefault();
        event.stopPropagation();
        setRoleSettingsOpen(true, settingsButton.dataset.nodeId);
        return false;
      }
      const roleButton = event.target.closest("[data-role-action]");
      if (roleButton) {
        event.preventDefault();
        event.stopPropagation();
        handleRoleAction(roleButton.dataset.roleAction, roleButton.dataset.role, roleButton.dataset.nodeId, roleButton);
        return;
      }
      const actionButton = event.target.closest("[data-node-action]");
      if (actionButton) {
        event.preventDefault();
        event.stopPropagation();
        handleNodeAction(actionButton.dataset.nodeAction, actionButton.dataset.nodeId);
        return;
      }
      if (event.target.closest("[data-node-check]")) {
        return;
      }
      const row = event.target.closest("[data-node-row]");
      if (!row) return;
      rememberSelectedNode(row.dataset.nodeId);
      renderNodes();
      renderMedia();
      renderStreamControls();
      renderYouTubeAgentList();
    });
    refs.nodeList.addEventListener("change", (event) => {
      const selectEl = event.target.closest("[data-node-profile-select]");
      if (selectEl) saveNodeYouTubeProfile(selectEl);
    });
    refs.hubNodeList.addEventListener("click", (event) => {
      if (event.target.closest("[data-node-profile-select]")) {
        event.stopPropagation();
        return;
      }
      const settingsButton = event.target.closest("[data-role-settings]");
      if (settingsButton) {
        event.preventDefault();
        event.stopPropagation();
        setRoleSettingsOpen(true, settingsButton.dataset.nodeId);
        return;
      }
      const roleButton = event.target.closest("[data-role-action]");
      if (roleButton) {
        event.preventDefault();
        event.stopPropagation();
        handleRoleAction(roleButton.dataset.roleAction, roleButton.dataset.role || "hub", roleButton.dataset.nodeId, roleButton);
        return;
      }
      const row = event.target.closest("[data-hub-row]");
      if (!row) return;
      switchHubWithFallback(row.dataset.nodeId);
    });
    refs.hubNodeList.addEventListener("change", (event) => {
      const selectEl = event.target.closest("[data-node-profile-select]");
      if (selectEl) saveNodeYouTubeProfile(selectEl);
    });
    refs.roleSettingsClose.addEventListener("click", () => setRoleSettingsOpen(false));
    refs.roleSettingsModal.addEventListener("click", (event) => {
      if (event.target === refs.roleSettingsModal) {
        setRoleSettingsOpen(false);
        return;
      }
      const button = event.target.closest("[data-settings-role]");
      if (!button) return;
      handleRoleAction(button.dataset.roleAction, button.dataset.settingsRole, roleSettingsNodeId, button);
    });
    refs.roleSettingsSaveNameBtn.addEventListener("click", saveRoleSettingsName);
    refs.roleSettingsDeleteNodeBtn.addEventListener("click", () => deleteNodeRecord(roleSettingsNodeId));
    refs.transferHubNodesBtn.addEventListener("click", transferHubNodes);
    refs.syncAllHubsBtn.addEventListener("click", syncAllHubs);
    refs.roleSettingsNameInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") saveRoleSettingsName();
    });
    refs.mediaDiskList.addEventListener("dblclick", (event) => {
      const card = event.target.closest("[data-resource-node-id]");
      if (!card) return;
      openResourceNodeId = card.dataset.resourceNodeId || "";
      renderMedia();
    });
    function openNodeResources(nodeId) {
      openResourceNodeId = String(nodeId || "");
      renderMedia();
      document.querySelector(".resource-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
      refs.mediaList.querySelector('[data-resource-filter="ownerNode"]')?.focus({ preventScroll: true });
    }
    refs.nodeSpaceRings.addEventListener("dblclick", (event) => {
      const card = event.target.closest("[data-space-node-id]");
      if (card) openNodeResources(card.dataset.spaceNodeId);
    });
    refs.nodeSpaceRings.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const card = event.target.closest("[data-space-node-id]");
      if (!card) return;
      event.preventDefault();
      openNodeResources(card.dataset.spaceNodeId);
    });
    refs.mediaDiskList.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const card = event.target.closest("[data-resource-node-id]");
      if (!card) return;
      event.preventDefault();
      openResourceNodeId = card.dataset.resourceNodeId || "";
      renderMedia();
    });
    refs.resourceFilterChip.addEventListener("click", (event) => {
      if (!event.target.closest("[data-clear-resource-node]")) return;
      openResourceNodeId = "";
      renderMedia();
    });
    refs.quickGroupBar.addEventListener("click", (event) => {
      const button = event.target.closest("[data-quick-group-index]");
      if (!button) return;
      const groupId = button.dataset.quickGroupId || "";
      refs.mediaGroupFilter.value = groupId;
      resourceTableFilters.group = groupId;
      renderMedia();
    });
    refs.quickGroupBar.addEventListener("contextmenu", (event) => {
      const button = event.target.closest("[data-quick-group-index]");
      if (!button) return;
      event.preventDefault();
      const groupId = button.dataset.quickGroupId || "";
      const currentName = button.textContent.trim();
      renameQuickGroup(groupId, currentName);
    });
    refs.mediaList.addEventListener("click", (event) => {
      hideMediaMenu();
      if (event.target.closest("[data-clear-resource-filters]")) {
        clearResourceFilters();
        return;
      }
      const row = event.target.closest("[data-media-row]");
      if (!row) return;
      selectMediaRow(row);
    });
    refs.mediaList.addEventListener("input", (event) => {
      const field = event.target?.dataset?.resourceFilter;
      if (!field) return;
      resourceTableFilters[field] = event.target.value || "";
      if (field === "group") refs.mediaGroupFilter.value = resourceTableFilters.group;
      if (field === "name") {
        clearTimeout(resourceNameFilterTimer);
        const caret = event.target.selectionStart ?? event.target.value.length;
        resourceNameFilterTimer = setTimeout(() => {
          renderMedia();
          const input = refs.mediaList.querySelector('[data-resource-filter="name"]');
          if (input) {
            input.focus();
            input.setSelectionRange(caret, caret);
          }
        }, 180);
        return;
      }
      renderMedia();
    });
    refs.mediaList.addEventListener("change", (event) => {
      const field = event.target?.dataset?.resourceFilter;
      if (!field) return;
      resourceTableFilters[field] = event.target.value || "";
      if (field === "group") refs.mediaGroupFilter.value = resourceTableFilters.group;
      renderMedia();
    });
    refs.mediaList.addEventListener("dblclick", (event) => {
      const row = event.target.closest("[data-media-row]");
      if (row) handleMediaAction("inspect", row);
    });
    refs.mediaList.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const row = event.target.closest("[data-media-row]");
      if (!row) return;
      event.preventDefault();
      selectMediaRow(row);
      handleMediaAction("inspect", row);
    });
    refs.mediaList.addEventListener("contextmenu", (event) => {
      const row = event.target.closest("[data-media-row]");
      if (!row) return;
      showMediaMenu(event, row);
    });
    refs.mediaContextMenu.addEventListener("click", (event) => {
      const button = event.target.closest("[data-media-menu-action]");
      if (!button || !contextMediaRow) return;
      const row = contextMediaRow;
      const action = button.dataset.mediaMenuAction;
      const targetNodeId = button.dataset.targetNodeId || "";
      const targetGroupId = button.dataset.targetGroupId || "";
      hideMediaMenu();
      if (action === "send-node") {
        selectMediaRow(row);
        pushSelectedMedia([targetNodeId]);
      } else if (action === "move-group") {
        selectMediaRow(row);
        moveMediaToGroup(row, targetGroupId);
      } else if (action === "move-node") {
        const sourceLabel = nodes.find((node) => String(node.id) === String(row.dataset.nodeId))?.name || row.dataset.nodeId;
        const targetLabel = nodes.find((node) => String(node.id) === String(targetNodeId))?.name || targetNodeId;
        if (!confirm(`确认把 ${row.dataset.mediaName} 从 ${sourceLabel} 移动到 ${targetLabel}？\n\n系统会先完整传输并确认成功，然后才删除源文件。`)) return;
        selectMediaRow(row);
        pushSelectedMedia([targetNodeId]).then(async (completed) => {
          if (!completed) return;
          const data = await postJson("/api/nodes/media/delete", {
            node_id: row.dataset.nodeId,
            media: row.dataset.videoPath || row.dataset.mediaName,
          });
          renderTransfer({
            status: data.ok ? "done" : "failed",
            badge: data.ok ? "移动完成" : "源文件保留",
            title: data.ok ? "资源移动完成" : "传输成功，但删除源文件失败",
            source: sourceLabel,
            target: targetLabel,
            percent: 100,
            message: data.ok ? `${row.dataset.mediaName} 已移动到 ${targetLabel}。` : friendlyError(data.message || "源文件删除失败"),
          });
          await refreshAll();
        });
      } else {
        handleMediaAction(action, row);
      }
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest("#mediaContextMenu")) hideMediaMenu();
      if (!event.target.closest(".quick-group-manage")) setQuickGroupManageOpen(false);
      if (refs.youtubeMoreActions?.open && !event.target.closest("#youtubeMoreActions")) refs.youtubeMoreActions.open = false;
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        hideMediaMenu();
        if (refs.youtubeMoreActions) refs.youtubeMoreActions.open = false;
      }
    });
    const THEME_STORAGE_KEY = "streamHubTheme";
    const TITLE_STORAGE_KEY = "streamHubCustomTitle";

    function applyTheme(theme) {
      const allowed = new Set(["forest", "midnight", "violet", "light"]);
      const selected = allowed.has(theme) ? theme : "forest";
      document.documentElement.dataset.theme = selected;
      refs.themeSelect.value = selected;
      localStorage.setItem(THEME_STORAGE_KEY, selected);
    }

    async function saveCustomTitle() {
      const title = refs.editableHubTitle.textContent.replace(/\s+/g, " ").trim().slice(0, 80) || "Stream Control Hub";
      refs.editableHubTitle.textContent = title;
      document.title = title;
      localStorage.setItem(TITLE_STORAGE_KEY, title);
      try {
        await postJson("/api/settings", { hub_name: title });
      } catch (error) {
        log(friendlyError(error, "Hub 名称保存失败"));
      }
    }

    async function loadHubSettings() {
      try {
        const resp = await fetch("/api/settings");
        const data = await resp.json();
        const title = String(data.hub_name || "").trim();
        if (title) {
          refs.editableHubTitle.textContent = title;
          document.title = title;
          localStorage.setItem(TITLE_STORAGE_KEY, title);
        }
      } catch (_) {}
    }

    applyTheme(localStorage.getItem(THEME_STORAGE_KEY) || "forest");
    refs.editableHubTitle.textContent = localStorage.getItem(TITLE_STORAGE_KEY) || "Stream Control Hub";
    document.title = refs.editableHubTitle.textContent;
    loadHubSettings();
    refs.themeSelect.addEventListener("change", () => applyTheme(refs.themeSelect.value));
    refs.editableHubTitle.addEventListener("blur", saveCustomTitle);
    refs.editableHubTitle.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        refs.editableHubTitle.blur();
      }
    });
    refs.editableHubTitle.addEventListener("paste", (event) => {
      event.preventDefault();
      document.execCommand("insertText", false, event.clipboardData.getData("text/plain"));
    });
    document.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (button) flashButton(button);
      const connectButton = event.target.closest("[data-open-connect]");
      if (connectButton) {
        event.preventDefault();
        setTailscaleWizardOpen(true);
        uiMessage("正在打开 Agent 快速连接。输入目标服务器的 Tailscale 100.x 地址即可检测并接入。");
        return;
      }
      const scrollButton = event.target.closest("[data-scroll-target]");
      if (scrollButton) {
        event.preventDefault();
        scrollToSelector(scrollButton.dataset.scrollTarget);
        uiMessage("已跳到对应操作区域。");
      }
    });
    refs.refreshBtn.addEventListener("click", refreshAll);
    refs.mediaGroupFilter.addEventListener("change", () => {
      resourceTableFilters.group = refs.mediaGroupFilter.value || "";
      renderMedia();
    });
    refs.quickGroupManageBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      setQuickGroupManageOpen(refs.quickGroupManageMenu.hidden);
    });
    refs.quickGroupCreateBtn.addEventListener("click", () => {
      setQuickGroupManageOpen(false);
      manageMediaGroup("create");
    });
    refs.quickGroupDeleteBtn.addEventListener("click", () => {
      setQuickGroupManageOpen(false);
      deleteQuickGroup();
    });
    refs.resourceMoreBtn.addEventListener("click", () => setResourceToolsOpen(true));
    refs.resourceToolsClose.addEventListener("click", () => setResourceToolsOpen(false));
    refs.resourceToolsModal.addEventListener("click", (event) => {
      if (event.target === refs.resourceToolsModal) setResourceToolsOpen(false);
    });
    if (refs.mediaGroupAddBtn) refs.mediaGroupAddBtn.addEventListener("click", () => manageMediaGroup("create"));
    if (refs.mediaGroupRenameBtn) refs.mediaGroupRenameBtn.addEventListener("click", () => manageMediaGroup("rename"));
    if (refs.mediaGroupDeleteBtn) refs.mediaGroupDeleteBtn.addEventListener("click", () => manageMediaGroup("delete"));
    if (refs.mediaAssignGroupBtn) refs.mediaAssignGroupBtn.addEventListener("click", assignSelectedMediaGroup);
    refs.mediaCleanupBtn.addEventListener("click", cleanupDuplicateMedia);
    refs.uploadBtn.addEventListener("click", uploadMedia);
    refs.mediaInput.addEventListener("change", updatePrimaryActionStates);
    refs.cancelUploadBtn.addEventListener("click", cancelActiveUpload);
    refs.checkUpdatesBtn.addEventListener("click", checkUpdatesAndPrompt);
    refs.showInstallCommandsBtn.addEventListener("click", showInstallCommands);
    refs.copyHubInstallBtn.addEventListener("click", () => copyInstallCommand("hub"));
    refs.copyAgentInstallQuickBtn.addEventListener("click", () => copyInstallCommand("agent"));
    refs.policyBtn.addEventListener("click", showPolicy);
    refs.auditBtn.addEventListener("click", showAudit);
    refs.tailscaleWizardBtn.addEventListener("click", () => setTailscaleWizardOpen(true));
    refs.tailscaleWizardClose.addEventListener("click", () => setTailscaleWizardOpen(false));
    refs.tailscaleWizardModal.addEventListener("click", (event) => {
      if (event.target === refs.tailscaleWizardModal) setTailscaleWizardOpen(false);
    });
    refs.youtubeWizardBtn.addEventListener("click", () => setYouTubeModalOpen(true));
    refs.youtubeImportJsonBtn.addEventListener("click", openYouTubeJsonImport);
    refs.youtubeWizardClose.addEventListener("click", () => setYouTubeModalOpen(false));
    refs.youtubeWizardModal.addEventListener("click", (event) => {
      if (event.target === refs.youtubeWizardModal) event.preventDefault();
    });
    refs.youtubeJsonPickBtn.addEventListener("click", () => refs.youtubeJsonFileInput.click());
    refs.youtubeJsonFileInput.addEventListener("change", loadYouTubeOAuthJsonFile);
    refs.youtubeJsonInput.addEventListener("paste", () => {
      window.setTimeout(() => applyYouTubeOAuthJsonText(refs.youtubeJsonInput.value, "粘贴的 JSON"), 0);
    });
    refs.youtubeJsonInput.addEventListener("blur", () => applyYouTubeOAuthJsonText(refs.youtubeJsonInput.value, "粘贴的 JSON"));
    refs.youtubeProfileSelect.addEventListener("change", selectYouTubeProfile);
    refs.youtubeProfileQuickBar.addEventListener("click", (event) => {
      if (event.target.closest("[data-youtube-profile-edit]")) return;
      const chip = event.target.closest("[data-youtube-profile-chip]");
      if (!chip) return;
      refs.youtubeProfileSelect.value = chip.dataset.youtubeProfileChip || "default";
      selectYouTubeProfile();
    });
    refs.youtubeProfileQuickBar.addEventListener("dblclick", (event) => {
      const chip = event.target.closest("[data-youtube-profile-chip]");
      if (!chip) return;
      refs.youtubeProfileSelect.value = chip.dataset.youtubeProfileChip || "default";
      applyYouTubeProfileToForm(currentYouTubeProfile() || {});
      setYouTubeProfileNameEditing(chip.dataset.youtubeProfileChip || "default");
    });
    refs.youtubeProfileQuickBar.addEventListener("keydown", (event) => {
      const input = event.target.closest("[data-youtube-profile-edit]");
      if (!input) return;
      if (event.key === "Enter") {
        event.preventDefault();
        saveYouTubeProfileName(input.value, input.dataset.youtubeProfileEdit || "");
      }
      if (event.key === "Escape") {
        event.preventDefault();
        cancelYouTubeProfileNameEdit();
      }
    });
    refs.youtubeProfileQuickBar.addEventListener("focusout", (event) => {
      const input = event.target.closest("[data-youtube-profile-edit]");
      if (input) saveYouTubeProfileName(input.value, input.dataset.youtubeProfileEdit || "");
    });
    refs.youtubeAgentList.addEventListener("click", (event) => {
      const card = event.target.closest("[data-youtube-agent-id]");
      if (!card || card.disabled) return;
      rememberSelectedNode(card.dataset.youtubeAgentId || "");
      renderNodes();
      renderMedia();
      renderStreamControls();
      renderYouTubeAgentList();
      refreshYouTubeResources();
    });
    refs.youtubeProfileAddBtn.addEventListener("click", createYouTubeProfile);
    refs.youtubeProfileDeleteBtn.addEventListener("click", deleteYouTubeProfile);
    refs.youtubeRefreshBtn.addEventListener("click", refreshYouTubeResources);
    refs.youtubeSaveConfigBtn.addEventListener("click", saveYouTubeConfig);
    refs.youtubeAuthorizeBtn.addEventListener("click", startYouTubeAuthorization);
    refs.youtubePrepareBtn.addEventListener("click", prepareYouTubeBroadcast);
    refs.youtubeHealthBtn.addEventListener("click", readYouTubeHealth);
    refs.youtubeRevokeBtn.addEventListener("click", revokeYouTubeAuthorization);
    refs.youtubeMoreActions.addEventListener("click", (event) => {
      const button = event.target.closest(".youtube-more-menu button");
      if (button) window.setTimeout(() => { refs.youtubeMoreActions.open = false; }, 0);
    });
    refs.tailscaleUseExistingIpBtn.addEventListener("click", connectExistingTailscaleIp);
    refs.copyAgentInstallBtn.addEventListener("click", copyAgentInstallCommand);
    if (refs.pushSelectedBtn) refs.pushSelectedBtn.addEventListener("click", pushSelectedMedia);
    refs.previewTuneBtn.addEventListener("click", previewTune);
    refs.applyTuneBtn.addEventListener("click", applyLastTune);
    refs.smartStartBtn.addEventListener("click", smartStart);
    refs.streamOutputModeInput.addEventListener("change", syncStreamOutputMode);
    refs.streamVideoSelect.addEventListener("change", updatePrimaryActionStates);
    refs.streamKeyInput.addEventListener("input", updatePrimaryActionStates);
    refs.youtubeStreamSelect.addEventListener("change", updatePrimaryActionStates);
    refs.streamUrlInput.addEventListener("input", () => { refs.streamUrlInput.dataset.userEdited = "1"; });
    [refs.presetInput, refs.videoBitrateInput, refs.audioBitrateInput, refs.fpsInput, refs.resolutionInput, refs.keyframeInput].forEach((el) => {
      el.addEventListener("input", () => { refs.tuneBox.dataset.copyMode = "0"; });
    });
    initNodeRoleSplitter();
    refreshAll();
    checkDailyGithubUpdates();
    window.setInterval(checkDailyGithubUpdates, 60 * 60 * 1000);
  </script>
</body>
</html>
"""


@APP.get("/")
def index():
    version = local_git_version()
    etag = hashlib.sha256(f"{version}:{len(HTML)}".encode("utf-8")).hexdigest()[:24]
    if request.headers.get("If-None-Match", "").strip('"') == etag:
        response = make_response("", 304)
    else:
        response = make_response(HTML)
    response.headers["ETag"] = f'"{etag}"'
    response.headers["X-Stream-Hub-Version"] = version
    response.headers["Cache-Control"] = "private, no-cache, max-age=0, must-revalidate"
    return response


@APP.get("/api/app-version")
def api_app_version():
    response = jsonify({"ok": True, "version": local_git_version()})
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def is_private_or_loopback_host(hostname: str) -> bool:
    if not hostname:
        return False
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        return hostname.endswith(".local") or hostname.endswith(".ts.net") or hostname.endswith(".beta.tailscale.net")
    return ip.is_loopback or ip.is_private or ip in TAILSCALE_CGNAT


def request_is_local() -> bool:
    remote_addr = request.remote_addr or ""
    try:
        remote_ip = ipaddress.ip_address(remote_addr.split("%", 1)[0])
    except ValueError:
        return False
    return remote_ip.is_loopback


def request_control_token() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return request.headers.get("X-Control-Token", "").strip()


def has_valid_control_token() -> bool:
    return bool(CONTROL_TOKEN and hmac.compare_digest(request_control_token(), CONTROL_TOKEN))


def write_request_allowed() -> bool:
    if request_is_local() or has_valid_control_token():
        return True
    return TRUSTED_REMOTE_WRITES


def dangerous_local_action_allowed() -> bool:
    return request_is_local() or has_valid_control_token() or TRUSTED_REMOTE_WRITES


def reject_forbidden(message: str = "control token or localhost access required"):
    return jsonify({"ok": False, "message": message}), 403


@APP.before_request
def protect_write_requests():
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    if not write_request_allowed():
        return reject_forbidden()
    origin = request.headers.get("Origin")
    if origin:
        parsed = urlparse(origin)
        host = parsed.hostname or ""
        if not is_private_or_loopback_host(host) and not has_valid_control_token():
            return reject_forbidden("cross-origin write requests require STREAM_HUB_CONTROL_TOKEN")
    return None


def redact_secret(value: str, *secrets: str) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def run_command(args: list[str], timeout: int = 60, secrets: list[str] | None = None) -> dict[str, Any]:
    if not args:
        return {"ok": False, "message": "missing command"}
    if not shutil.which(args[0]):
        return {"ok": False, "message": f"{args[0]} is not installed"}
    try:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    secret_values = secrets or []
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": redact_secret(proc.stdout.strip(), *secret_values),
        "stderr": redact_secret(proc.stderr.strip(), *secret_values),
    }


def run_helper_script(
    script: Path,
    args: list[str],
    timeout: int = 60,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
) -> dict[str, Any]:
    if not script.exists():
        return {"ok": False, "message": f"helper script missing: {script}"}
    if not shutil.which("sh"):
        return {"ok": False, "message": "sh is not available"}
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    try:
        proc = subprocess.run(
            ["sh", str(script), *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            env=proc_env,
        )
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    secret_values = secrets or []
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": redact_secret(proc.stdout.strip(), *secret_values),
        "stderr": redact_secret(proc.stderr.strip(), *secret_values),
    }


def tailscale_status() -> dict[str, Any]:
    if TAILSCALE_HELPER.exists():
        helper = run_helper_script(TAILSCALE_HELPER, ["status"], timeout=15)
        if helper.get("ok"):
            try:
                data = json.loads(helper.get("stdout") or "{}")
            except json.JSONDecodeError:
                data = {}
            if data:
                return tailscale_status_from_json(data)
        if "not installed" in str(helper.get("stdout") or helper.get("stderr") or helper.get("message") or "").lower():
            return {"ok": False, "installed": False, "message": "tailscale is not installed"}
    result = run_command(["tailscale", "status", "--json"], timeout=15)
    if not result.get("ok"):
        return result
    try:
        data = json.loads(result.get("stdout") or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "message": "tailscale returned invalid json"}
    return tailscale_status_from_json(data)


def tailscale_status_from_json(data: dict[str, Any]) -> dict[str, Any]:
    self_info = data.get("Self") or {}
    return {
        "ok": True,
        "installed": True,
        "backend_state": data.get("BackendState"),
        "self": {
            "host_name": self_info.get("HostName"),
            "dns_name": self_info.get("DNSName"),
            "tailscale_ips": self_info.get("TailscaleIPs") or [],
            "online": self_info.get("Online"),
        },
        "peers": [
            {
                "host_name": peer.get("HostName"),
                "dns_name": peer.get("DNSName"),
                "tailscale_ips": peer.get("TailscaleIPs") or [],
                "online": peer.get("Online"),
                "last_seen": peer.get("LastSeen"),
            }
            for peer in (data.get("Peer") or {}).values()
        ],
    }


def online_tailscale_peer_for_ip(ip: str) -> dict[str, Any] | None:
    status = tailscale_status()
    if not status.get("ok"):
        return None
    for peer in status.get("peers") or []:
        if ip in (peer.get("tailscale_ips") or []) and peer.get("online") is True:
            return peer
    return None


def pair_tailscale_agent(base_url: str, *, timeout: int = 12) -> dict[str, Any]:
    try:
        response = requests.post(f"{base_url.rstrip('/')}/pair", json={"client": "stream-control-hub"}, timeout=timeout)
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:500]}
        payload["ok"] = response.ok and bool(payload.get("ok", True))
        payload.setdefault("status_code", response.status_code)
        return payload
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def request_hub_status_url(hub_url: str, *, timeout: int = 5) -> dict[str, Any]:
    try:
        response = requests.get(f"{hub_url.rstrip('/')}/api/role-status", timeout=timeout)
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:500]}
        payload["ok"] = response.ok and bool(payload.get("ok", False))
        payload.setdefault("status_code", response.status_code)
        return payload
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def normalize_install_commands(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, str] = {}
    for key in ("unified", "hub", "agent"):
        value = str(payload.get(key) or "").strip()
        if value and len(value) <= 2000 and "scripts/install.sh" in value:
            result[key] = value
    return result


def latest_install_commands() -> dict[str, Any]:
    if INSTALL_COMMANDS_URL:
        try:
            response = requests.get(
                INSTALL_COMMANDS_URL,
                headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
                params={"ts": int(time.time())},
                timeout=8,
            )
            response.raise_for_status()
            commands = normalize_install_commands(response.json())
            if commands.get("agent"):
                return {"ok": True, "source": "github", "source_url": INSTALL_COMMANDS_URL, **commands}
        except Exception:
            pass
    try:
        commands = normalize_install_commands(json.loads(INSTALL_COMMANDS_FILE.read_text(encoding="utf-8")))
    except Exception:
        commands = {}
    if commands.get("agent"):
        return {"ok": True, "source": "local-fallback", **commands}
    return {"ok": False, "message": "GitHub and local install command manifests are unavailable"}


def tailscale_precheck() -> dict[str, Any]:
    result = run_helper_script(TAILSCALE_HELPER, ["precheck"], timeout=60)
    payload: dict[str, Any] = {"ok": False, "message": result.get("message") or "Tailscale precheck failed"}
    with suppress(json.JSONDecodeError):
        payload = json.loads(result.get("stdout") or "{}")
    payload["result"] = result
    return payload


def tailscale_install() -> dict[str, Any]:
    precheck = tailscale_precheck()
    result = run_helper_script(TAILSCALE_HELPER, ["install"], timeout=600)
    return {
        "ok": bool(result.get("ok")),
        "message": "Tailscale install/fix complete" if result.get("ok") else "Tailscale install/fix failed",
        "precheck": precheck,
        "result": result,
        "status": tailscale_status() if result.get("ok") else None,
    }


def tailscale_connect(auth_key: str, hostname: str, *, accept_routes: bool = True, ssh: bool = False) -> dict[str, Any]:
    env = {
        "TAILSCALE_AUTH_KEY": auth_key,
        "TAILSCALE_HOSTNAME": hostname,
        "TAILSCALE_ACCEPT_ROUTES": "1" if accept_routes else "0",
        "TAILSCALE_SSH": "1" if ssh else "0",
    }
    precheck = tailscale_precheck()
    result = run_helper_script(TAILSCALE_HELPER, ["connect"], timeout=600, env=env, secrets=[auth_key])
    if not result.get("ok") and "sh is not available" in str(result.get("message") or ""):
        args = [
            "tailscale",
            "up",
            "--auth-key",
            auth_key,
            "--hostname",
            hostname,
            "--accept-dns=false",
        ]
        if accept_routes:
            args.append("--accept-routes")
        if ssh:
            args.append("--ssh")
        result = run_command(args, timeout=90, secrets=[auth_key])
    status = tailscale_status() if result.get("ok") else None
    return {
        "ok": bool(result.get("ok")),
        "message": "Tailscale connected" if result.get("ok") else "Tailscale connect failed",
        "precheck": precheck,
        "result": result,
        "status": status,
    }


def ensure_dirs() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not NODES_FILE.exists():
        example = CONFIG_DIR / "nodes.example.json"
        if example.exists():
            shutil.copyfile(example, NODES_FILE)


def load_nodes() -> list[dict[str, Any]]:
    ensure_dirs()
    try:
        data = json.loads(NODES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [node for node in data if isinstance(node, dict)]


def save_nodes(nodes: list[dict[str, Any]]) -> None:
    ensure_dirs()
    NODES_FILE.write_text(json.dumps(nodes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_hub_settings() -> dict[str, Any]:
    ensure_dirs()
    try:
        payload = json.loads(HUB_SETTINGS_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def save_hub_settings(settings: dict[str, Any]) -> None:
    ensure_dirs()
    HUB_SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def node_youtube_profile_map() -> dict[str, str]:
    settings = load_hub_settings()
    mapping = settings.get("node_youtube_profiles")
    return {str(key): safe_youtube_profile_id(str(value)) for key, value in (mapping or {}).items()} if isinstance(mapping, dict) else {}


def set_node_youtube_profile(node_id: str, profile_id: str) -> str:
    profile_id = safe_youtube_profile_id(profile_id or active_youtube_profile_id())
    config = load_youtube_profiles_config()
    valid_profiles = {profile["id"] for profile in config["profiles"]}
    if profile_id not in valid_profiles:
        raise YouTubeAPIError("YouTube profile not found", status_code=404, reason="profile_not_found")
    settings = load_hub_settings()
    mapping = settings.get("node_youtube_profiles")
    if not isinstance(mapping, dict):
        mapping = {}
    mapping[str(node_id)] = profile_id
    settings["node_youtube_profiles"] = mapping
    save_hub_settings(settings)
    return profile_id


def load_media_groups() -> dict[str, Any]:
    ensure_dirs()
    try:
        payload = json.loads(MEDIA_GROUPS_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload.setdefault("groups", [])
            payload.setdefault("assignments", {})
            payload.setdefault("duplicate_retention", [])
            return payload
    except Exception:
        pass
    return {"groups": [], "assignments": {}, "duplicate_retention": []}


def save_media_groups(payload: dict[str, Any]) -> None:
    ensure_dirs()
    MEDIA_GROUPS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cleanup_verified_duplicates(
    metadata: dict[str, Any],
    *,
    execute: bool,
    created_before_days: int = 0,
    usage_mode: str = "any",
    unused_days: int = 0,
) -> dict[str, Any]:
    now = time.time()
    nodes = {str(node.get("id") or ""): node for node in load_nodes()}
    records = list(metadata.get("duplicate_retention") or [])
    remaining: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    for record in records:
        if now < float(record.get("delete_after") or 0):
            remaining.append(record)
            continue
        old_node = nodes.get(str(record.get("old_node_id") or ""))
        keep_node = nodes.get(str(record.get("keep_node_id") or ""))
        filename = str(record.get("filename") or "")
        expected_hash = str(record.get("sha256") or "")
        if not old_node or not keep_node or not filename or not expected_hash:
            record["status"] = "blocked-missing-node-metadata"
            remaining.append(record)
            continue
        old_hash = request_node_media_hash(old_node, filename)
        keep_hash = request_node_media_hash(keep_node, filename)
        # Never delete unless two complete copies still exist and both hashes match.
        if not (
            old_hash.get("ok") and keep_hash.get("ok")
            and old_hash.get("sha256") == expected_hash == keep_hash.get("sha256")
            and int(old_hash.get("size") or 0) == int(keep_hash.get("size") or 0)
        ):
            record["status"] = "blocked-hash-or-copy-check"
            remaining.append(record)
            continue
        old_status = request_node_json(old_node, "/api/status", timeout=12)
        old_video = next((item for item in old_status.get("videos") or [] if str(item.get("name") or "") == filename), {})
        created_at = float(old_video.get("created_at") or old_video.get("modified") or 0)
        last_used_at = float(old_video.get("last_used_at") or 0)
        if created_before_days and (not created_at or now - created_at < created_before_days * 86400):
            remaining.append(record)
            continue
        if usage_mode == "never" and last_used_at:
            remaining.append(record)
            continue
        if usage_mode == "unused" and last_used_at and now - last_used_at < max(1, unused_days) * 86400:
            remaining.append(record)
            continue
        candidate = {
            "record_id": record.get("id"),
            "filename": filename,
            "node_id": str(old_node.get("id") or ""),
            "node_name": str(old_node.get("name") or old_node.get("id") or "Agent"),
            "size": int(old_hash.get("size") or 0),
            "created_at": created_at,
            "last_used_at": last_used_at,
            "sha256": expected_hash,
        }
        candidates.append(candidate)
        if execute:
            result = post_node_json(old_node, "/api/media/delete", {"media": filename}, timeout=60)
            if result.get("ok"):
                deleted.append(candidate)
                continue
            record["status"] = "delete-failed"
            record["last_error"] = result.get("message") or "delete failed"
        remaining.append(record)
    if execute:
        metadata["duplicate_retention"] = remaining
        save_media_groups(metadata)
    return {
        "ok": True,
        "candidates": candidates,
        "deleted": deleted,
        "candidate_count": len(candidates),
        "deleted_count": len(deleted),
        "candidate_bytes": sum(int(item.get("size") or 0) for item in candidates),
        "safety": "verified-duplicate-only; never delete the last copy",
    }


def media_library_payload() -> dict[str, Any]:
    metadata = load_media_groups()
    cleanup_verified_duplicates(metadata, execute=True)
    metadata = load_media_groups()
    resources: dict[str, dict[str, Any]] = {}
    node_disks: list[dict[str, Any]] = []
    for node in load_nodes():
        if not node.get("enabled", True):
            continue
        status = request_node_json(node, "/api/status", timeout=10)
        disk = status.get("disk") or {}
        node_disks.append({
            "node_id": str(node.get("id") or ""),
            "node_name": str(node.get("name") or node.get("id") or "Agent"),
            "online": bool(status.get("ok")),
            "total": int(disk.get("total") or 0),
            "used": int(disk.get("used") or 0),
            "free": int(disk.get("free") or 0),
            "percent": float(disk.get("percent") or 0),
        })
        for video in status.get("videos") or []:
            name = str(video.get("name") or Path(str(video.get("video_path") or "")).name).strip()
            if not name:
                continue
            item = resources.setdefault(name, {
                "name": name,
                "size": int(video.get("size") or 0),
                "modified": float(video.get("modified") or 0),
                "modified_label": str(video.get("modified_label") or "--"),
                "created_at": float(video.get("created_at") or video.get("modified") or 0),
                "last_used_at": float(video.get("last_used_at") or 0),
                "group_id": str((metadata.get("assignments") or {}).get(name) or ""),
                "copies": [],
            })
            if float(video.get("modified") or 0) > float(item.get("modified") or 0):
                item["modified"] = float(video.get("modified") or 0)
                item["modified_label"] = str(video.get("modified_label") or "--")
            item["size"] = max(int(item.get("size") or 0), int(video.get("size") or 0))
            item["copies"].append({
                "node_id": str(node.get("id") or ""),
                "node_name": str(node.get("name") or node.get("id") or "Agent"),
                "video_path": str(video.get("video_path") or video.get("path") or name),
                "created_at": float(video.get("created_at") or video.get("modified") or 0),
                "last_used_at": float(video.get("last_used_at") or 0),
                "last_used_label": str(video.get("last_used_label") or "从未开播"),
            })
            item["last_used_at"] = max(float(item.get("last_used_at") or 0), float(video.get("last_used_at") or 0))
    return {
        "ok": True,
        "groups": metadata.get("groups") or [],
        "resources": sorted(resources.values(), key=lambda item: float(item.get("modified") or 0), reverse=True),
        "nodes": node_disks,
        "duplicate_retention": metadata.get("duplicate_retention") or [],
    }


def node_by_id(node_id: str) -> dict[str, Any] | None:
    for node in load_nodes():
        if str(node.get("id")) == node_id:
            return node
    return None


def update_env_file_values(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or "=" not in raw_line:
                existing.append((raw_line, None))
                continue
            key, _ = raw_line.split("=", 1)
            key = key.strip()
            if key in updates:
                existing.append((f"{key}={updates[key]}", key))
                seen.add(key)
            else:
                existing.append((raw_line, key))
    for key, value in updates.items():
        if key not in seen:
            existing.append((f"{key}={value}", key))
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text("\n".join(line for line, _ in existing) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def reload_hub_youtube_client(*, client_id: str, client_secret: str) -> None:
    global YOUTUBE_CLIENT
    os.environ["YOUTUBE_CLIENT_ID"] = client_id
    os.environ["YOUTUBE_CLIENT_SECRET"] = client_secret
    YOUTUBE_CLIENT = YouTubeAPIClient(
        client_id=client_id,
        client_secret=client_secret,
        credential_path=YOUTUBE_CREDENTIAL_FILE,
        quota_recorder=lambda method, resource, units: record_youtube_api_usage(YOUTUBE_DEFAULT_PROFILE_ID, method, resource, units),
    )


def save_youtube_profile_config(profile_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    profile_id = safe_youtube_profile_id(profile_id or active_youtube_profile_id())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with YOUTUBE_PROFILE_LOCK:
        config = load_youtube_profiles_config()
        profiles = list(config["profiles"])
        found = False
        for profile in profiles:
            if profile["id"] == profile_id:
                found = True
                profile.update(updates)
                profile["id"] = profile_id
                profile["updated_at"] = now
                if not profile.get("credential_file"):
                    profile["credential_file"] = str(YOUTUBE_PROFILE_CREDENTIALS_DIR / f"{profile_id}.json")
                break
        if not found:
            profile = {
                **default_youtube_profile(),
                **updates,
                "id": profile_id,
                "created_at": now,
                "updated_at": now,
                "credential_file": str(YOUTUBE_PROFILE_CREDENTIALS_DIR / f"{profile_id}.json"),
            }
            profiles.append(profile)
        config["profiles"] = profiles
        config["active_profile_id"] = profile_id
        save_youtube_profiles_config(config)
        YOUTUBE_CLIENT_CACHE.pop(profile_id, None)
    if profile_id == YOUTUBE_DEFAULT_PROFILE_ID:
        update_env_file_values(
            HUB_ENV_FILE,
            {
                "YOUTUBE_CLIENT_ID": str(updates.get("client_id") or ""),
                "YOUTUBE_CLIENT_SECRET": str(updates.get("client_secret") or ""),
                "YOUTUBE_CREDENTIAL_FILE": str(YOUTUBE_CREDENTIAL_FILE),
            },
        )
        reload_hub_youtube_client(
            client_id=str(updates.get("client_id") or ""),
            client_secret=str(updates.get("client_secret") or ""),
        )
    return youtube_profile_by_id(profile_id)


def youtube_error_response(exc: Exception):
    if isinstance(exc, YouTubeAPIError):
        return jsonify({"ok": False, "message": str(exc), "reason": exc.reason}), exc.status_code
    return jsonify({"ok": False, "message": str(exc)}), 502


def request_node_json(node: dict[str, Any], path: str, *, timeout: int = 6) -> dict[str, Any]:
    base_url = node_base_url(node)
    if not base_url:
        return {"ok": False, "message": "missing node base_url"}
    try:
        resp = requests.get(f"{base_url}{path}", headers=node_headers(node), timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            data = {"message": resp.text[:500]}
        data["ok"] = resp.ok and bool(data.get("ok", True))
        data.setdefault("status_code", resp.status_code)
        return data
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def node_base_url(node: dict[str, Any]) -> str:
    return str(node.get("base_url") or "").rstrip("/")


def node_role_urls(node: dict[str, Any]) -> dict[str, str]:
    base_url = node_base_url(node)
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    if not host:
        return {"agent": base_url, "hub": ""}
    host_label = f"[{host}]" if ":" in host else host
    return {
        "agent": base_url or f"http://{host_label}:8787",
        "hub": str(node.get("hub_url") or f"http://{host_label}:8788").rstrip("/"),
    }


def request_hub_role_status(node: dict[str, Any]) -> dict[str, Any]:
    hub_url = node_role_urls(node)["hub"]
    if not hub_url:
        return {"ok": False, "enabled": False, "message": "missing Hub URL"}
    try:
        response = requests.get(f"{hub_url}/api/role-status", timeout=3)
        data = response.json()
        hub = (data.get("roles") or {}).get("hub") or {}
        return {"ok": response.ok, "enabled": response.ok and bool(hub.get("enabled", True)), "url": hub_url, **hub}
    except Exception as exc:
        return {"ok": False, "enabled": False, "url": hub_url, "message": str(exc)}


def schedule_agent_role_activation(control_hub_url: str) -> dict[str, Any]:
    if not shutil.which("systemd-run"):
        raise RuntimeError("systemd-run is required to activate the Agent role")
    unit = f"stream-control-agent-activate-{int(time.time())}"
    root = shlex.quote(str(ROOT))
    control_hub = shlex.quote(control_hub_url)
    script = f"set -eu; sleep 2; env STREAM_AGENT_CONTROL_HUB={control_hub} CHOICE=1 sh {root}/scripts/install-agent.sh"
    result = subprocess.run(
        ["systemd-run", "--unit", unit, "--collect", "--no-block", "/bin/sh", "-c", script],
        text=True,
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "failed to schedule Agent activation").strip())
    return {"unit": unit, "role": "agent", "control_hub": control_hub_url}


def schedule_hub_upgrade() -> dict[str, Any]:
    if not shutil.which("systemd-run") or not (ROOT / ".git").exists():
        raise RuntimeError("Hub must be a Git-managed systemd installation")
    unit = f"stream-control-hub-upgrade-{int(time.time())}"
    root = shlex.quote(str(ROOT))
    script = (
        "set -eu; sleep 2; "
        f"git -C {root} fetch origin main; git -C {root} checkout main; "
        f"git -C {root} pull --ff-only origin main; env BRANCH=main CHOICE=1 "
        f"STREAM_HUB_SUPPRESS_TOKEN_OUTPUT=1 sh {root}/scripts/install-hub.sh"
    )
    result = subprocess.run(
        ["systemd-run", "--unit", unit, "--collect", "--no-block", "/bin/sh", "-c", script],
        text=True,
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "failed to schedule Hub upgrade").strip())
    return {"unit": unit, "role": "hub", "from_version": local_git_version(), "target_branch": "main"}


def schedule_hub_deactivation() -> dict[str, Any]:
    if not shutil.which("systemd-run") or not (ROOT / "scripts" / "install-hub.sh").exists():
        raise RuntimeError("Hub must be a systemd installation to deactivate from the panel")
    unit = f"stream-control-hub-deactivate-{int(time.time())}"
    root = shlex.quote(str(ROOT))
    script = f"set -eu; sleep 2; ACTION=uninstall REMOVE_DATA=0 sh {root}/scripts/install-hub.sh"
    result = subprocess.run(
        ["systemd-run", "--unit", unit, "--collect", "--no-block", "/bin/sh", "-c", script],
        text=True,
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "failed to schedule Hub deactivation").strip())
    return {"unit": unit, "role": "hub", "remove_data": False}


def is_public_upload_url(value: str) -> bool:
    try:
        host = urlparse(value).hostname or ""
        ip = ipaddress.ip_address(host.split("%", 1)[0])
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip in TAILSCALE_CGNAT)
    except ValueError:
        return bool(host and not host.endswith((".local", ".ts.net", ".beta.tailscale.net")))


def node_upload_base_urls(node: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("upload_base_url", "public_base_url"):
        value = str(node.get(key) or "").strip().rstrip("/")
        if value and is_public_upload_url(value):
            values.append(value)
    for value in node.get("upload_base_urls") or []:
        value = str(value or "").strip().rstrip("/")
        if value and is_public_upload_url(value):
            values.append(value)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def safe_media_filename(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    raw = unicodedata.normalize("NFC", raw)
    suffix = Path(raw).suffix.lower()
    if suffix not in ALLOWED_MEDIA_EXTENSIONS:
        raise ValueError("unsupported media extension")
    stem = raw[:-len(suffix)]
    stem = "".join(char for char in stem if ord(char) >= 32 and char not in '<>:"/\\|?*')
    stem = stem.strip().strip(".").strip()
    if not stem:
        stem = f"视频-{uuid.uuid4().hex[:10]}"
    if len(stem) > 180:
        stem = stem[:180].rstrip()
    name = f"{stem}{suffix}"
    return name


def upload_route_label(url: str, base_url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        ip = ipaddress.ip_address(host.split("%", 1)[0])
        if ip in TAILSCALE_CGNAT:
            return "Tailscale 兜底"
        if ip.is_private or ip.is_loopback:
            return "内网直连"
    except ValueError:
        if host.endswith(".ts.net") or host.endswith(".beta.tailscale.net"):
            return "Tailscale 兜底"
    return "公网直连" if url != base_url else "默认线路"


def node_headers(node: dict[str, Any]) -> dict[str, str]:
    token = str(node.get("token") or node.get("control_token") or "").strip()
    return {"X-Control-Token": token} if token else {}


def post_node_json(node: dict[str, Any], path: str, payload: dict[str, Any], *, timeout: int = 15) -> dict[str, Any]:
    base_url = node_base_url(node)
    if not base_url:
        return {"ok": False, "message": "missing node base_url"}
    try:
        resp = requests.post(f"{base_url}{path}", json=payload, headers=node_headers(node), timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            data = {"message": resp.text[:500]}
        data["ok"] = resp.ok and bool(data.get("ok", False))
        data.setdefault("status_code", resp.status_code)
        return data
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def post_url_json(url: str, payload: dict[str, Any], *, timeout: int = 30, headers: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        response = requests.post(url, json=payload, headers=headers or {}, timeout=timeout)
        try:
            data = response.json()
        except ValueError:
            data = {"message": response.text[:500]}
        data["ok"] = response.ok and bool(data.get("ok", False))
        data.setdefault("status_code", response.status_code)
        return data
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def request_node_upload_ticket(node: dict[str, Any], *, upload_id: str, filename: str, total_size: int) -> dict[str, Any]:
    return post_node_json(
        node,
        "/api/upload-ticket",
        {"upload_id": upload_id, "filename": filename, "total_size": total_size},
        timeout=10,
    )


def request_node_media_info(node: dict[str, Any], media: str) -> dict[str, Any]:
    status = request_node_json(node, "/api/status", timeout=10)
    if not status.get("ok"):
        return {"ok": False, "message": status.get("message") or "source node status unavailable"}
    media_name = Path(media).name
    for item in status.get("videos") or []:
        values = {str(item.get("name") or ""), str(item.get("video_path") or ""), str(item.get("path") or "")}
        if media in values or media_name in values:
            return {"ok": True, **item}
    return {"ok": False, "message": "media not found on source node"}


def share_task_snapshot(task_id: str) -> dict[str, Any] | None:
    with SHARE_TASKS_LOCK:
        task = SHARE_TASKS.get(task_id)
        return dict(task) if task else None


def update_share_task(task_id: str, **updates: Any) -> None:
    with SHARE_TASKS_LOCK:
        task = SHARE_TASKS.get(task_id)
        if not task:
            return
        task.update(updates)
        task["updated_at"] = time.time()


def share_task_payload(task: dict[str, Any]) -> dict[str, Any]:
    total = int(task.get("total_bytes") or 0)
    done = int(task.get("done_bytes") or 0)
    average_bps = int(task.get("average_bps") or 0)
    eta = int((total - done) / average_bps) if total and average_bps > 0 and done < total else 0
    return {
        "ok": task.get("status") != "failed",
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "message": task.get("message") or "",
        "source_node_id": task.get("source_node_id"),
        "target_node_ids": task.get("target_node_ids") or [],
        "media": task.get("media"),
        "done_bytes": done,
        "total_bytes": total,
        "percent": round((done / total) * 100, 2) if total else 0,
        "current_bps": int(task.get("current_bps") or 0),
        "average_bps": average_bps,
        "eta_seconds": eta,
        "results": task.get("results") or [],
        "error": task.get("error") or "",
        "deleted_node_id": task.get("deleted_node_id") or "",
        "migration_total_files": int(task.get("migration_total_files") or 0),
        "migration_done_files": int(task.get("migration_done_files") or 0),
        "route_label": task.get("route_label") or "",
        "transfer_route": task.get("transfer_route") or "",
    }


def request_node_media_hash(node: dict[str, Any], media: str) -> dict[str, Any]:
    return post_node_json(node, "/api/media/hash", {"media": media}, timeout=1800)


def register_verified_duplicate(source_node: dict[str, Any], target_node: dict[str, Any], filename: str, sha256: str) -> None:
    metadata = load_media_groups()
    records = list(metadata.get("duplicate_retention") or [])
    source_id = str(source_node.get("id") or "")
    records = [item for item in records if not (
        str(item.get("old_node_id") or "") == source_id and str(item.get("filename") or "") == filename
    )]
    verified_at = time.time()
    records.append({
        "id": f"duplicate-{uuid.uuid4().hex[:12]}",
        "filename": filename,
        "sha256": sha256,
        "old_node_id": source_id,
        "keep_node_id": str(target_node.get("id") or ""),
        "verified_at": verified_at,
        "delete_after": verified_at + 3 * 24 * 60 * 60,
        "status": "waiting-72h",
    })
    metadata["duplicate_retention"] = records
    save_media_groups(metadata)


def verify_and_register_copy(
    source_node: dict[str, Any],
    target_node: dict[str, Any],
    filename: str,
    source_hash: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_hash = source_hash or request_node_media_hash(source_node, filename)
    target_hash = request_node_media_hash(target_node, filename)
    if not source_hash.get("ok"):
        post_node_json(target_node, "/api/media/delete", {"media": filename}, timeout=60)
        return {"ok": False, "message": "源 Agent 不支持媒体哈希校验，请先升级源 Agent"}
    if not target_hash.get("ok"):
        post_node_json(target_node, "/api/media/delete", {"media": filename}, timeout=60)
        return {"ok": False, "message": "目标 Agent 无法读取复制文件，已删除不完整新副本"}
    verified = bool(
        source_hash.get("ok") and target_hash.get("ok") and source_hash.get("sha256")
        and source_hash.get("sha256") == target_hash.get("sha256")
        and int(source_hash.get("size") or 0) == int(target_hash.get("size") or 0)
    )
    if not verified:
        post_node_json(target_node, "/api/media/delete", {"media": filename}, timeout=60)
        return {"ok": False, "message": "复制文件 SHA-256 或大小校验失败，已删除不完整新副本"}
    register_verified_duplicate(source_node, target_node, filename, str(source_hash["sha256"]))
    return {"ok": True, "sha256": source_hash["sha256"], "delete_old_after_hours": 72}


def run_share_task(
    task_id: str,
    source_node: dict[str, Any],
    target_nodes: list[dict[str, Any]],
    media: str,
    progress_url: str,
) -> None:
    started_at = time.time()
    results: list[dict[str, Any]] = []
    try:
        media_info = request_node_media_info(source_node, media)
        source_hash: dict[str, Any] = {}
        if media_info.get("ok"):
            source_hash = request_node_media_hash(source_node, str(media_info.get("name") or Path(media).name))
        if not media_info.get("ok") or not source_hash.get("ok"):
            preferred_source_id = str(source_node.get("id") or "")
            for candidate in load_nodes():
                if str(candidate.get("id") or "") == preferred_source_id or not candidate.get("enabled", True):
                    continue
                candidate_media = request_node_media_info(candidate, media)
                if not candidate_media.get("ok"):
                    continue
                candidate_name = str(candidate_media.get("name") or Path(media).name)
                candidate_hash = request_node_media_hash(candidate, candidate_name)
                if candidate_hash.get("ok"):
                    source_node = candidate
                    media_info = candidate_media
                    source_hash = candidate_hash
                    update_share_task(
                        task_id,
                        source_node_id=str(candidate.get("id") or ""),
                        message=f"首选源节点不可用，已切换到 {candidate.get('name') or candidate.get('id')}",
                    )
                    break
        if not media_info.get("ok"):
            raise RuntimeError(media_info.get("message") or "媒体库没有可用源副本")
        if not source_hash.get("ok"):
            raise RuntimeError("所有在线源 Agent 都不支持媒体哈希校验，请先升级源 Agent")

        for target_index, target_node in enumerate(target_nodes):
            target_node_id = str(target_node.get("id") or "")
            previous_task = share_task_snapshot(task_id) or {}
            previous_total = int(previous_task.get("single_target_total_bytes") or previous_task.get("total_bytes") or 0)
            filename = str(media_info.get("name") or Path(media).name)
            total_size = int(media_info.get("size") or 0)
            if total_size <= 0:
                raise RuntimeError("source media size is unavailable")
            if int(source_hash.get("size") or 0) != total_size:
                raise RuntimeError("源媒体大小在共享前发生变化，请稍后重试")
            upload_id = f"share_{uuid.uuid4().hex}"
            ticket = request_node_upload_ticket(target_node, upload_id=upload_id, filename=filename, total_size=total_size)
            if not ticket.get("ok"):
                raise RuntimeError(ticket.get("message") or f"{target_node_id} did not issue an upload ticket")
            public_status = request_node_json(target_node, "/api/public-upload", timeout=10)
            discovered_public_url = (
                str(public_status.get("public_origin") or "").strip().rstrip("/")
                if public_status.get("ok") and public_status.get("supported")
                else ""
            )
            upload_urls: list[str] = []
            for upload_url in [discovered_public_url, *node_upload_base_urls(target_node)]:
                if upload_url and is_public_upload_url(upload_url) and upload_url not in upload_urls:
                    upload_urls.append(upload_url)
            if not upload_urls:
                raise RuntimeError(f"{target_node_id} 没有可用的公网上传地址；禁止通过 Tailscale 内网共享")
            target_upload_base_urls = upload_urls
            update_share_task(
                task_id,
                status="running",
                message=f"正在通过公网共享到 {target_node.get('name') or target_node_id}",
                transfer_route=target_upload_base_urls[0],
                route_label="公网直连（禁止内网回退）",
                done_bytes=previous_total * target_index if previous_total else int(previous_task.get("done_bytes") or 0),
                total_bytes=previous_total * len(target_nodes) if previous_total else int(previous_task.get("total_bytes") or 0),
            )
            share_payload = {
                "media": media,
                "target_base_url": target_upload_base_urls[0],
                "target_base_urls": target_upload_base_urls,
                "upload_id": upload_id,
                "target_upload_ticket": str(ticket.get("ticket") or ""),
                "progress_url": progress_url,
                "progress_task_id": task_id,
                "progress_target_index": target_index,
                "progress_target_count": len(target_nodes),
                "progress_target_node_id": target_node_id,
            }
            result = post_node_json(source_node, "/api/share-media", share_payload, timeout=1800)
            result["node_id"] = target_node_id
            results.append(result)
            if not result.get("ok"):
                raise RuntimeError(result.get("message") or f"{target_node_id} 共享失败")
            verification = verify_and_register_copy(source_node, target_node, filename, source_hash=source_hash)
            result["verification"] = verification
            if not verification.get("ok"):
                raise RuntimeError(verification.get("message") or "复制完整性校验失败")
        elapsed = max(0.001, time.time() - started_at)
        task = share_task_snapshot(task_id) or {}
        total = int(task.get("total_bytes") or 0)
        update_share_task(
            task_id,
            status="done",
            done_bytes=total or int(task.get("done_bytes") or 0),
            current_bps=0,
            average_bps=int((total or int(task.get("done_bytes") or 0)) / elapsed),
            message="共享完成",
            results=results,
        )
    except Exception as exc:
        update_share_task(
            task_id,
            status="failed",
            message="共享失败",
            error=str(exc),
            results=results,
        )


def remove_node_record(node_id: str) -> dict[str, Any]:
    nodes = load_nodes()
    remaining = [node for node in nodes if str(node.get("id") or "") != node_id]
    if len(remaining) == len(nodes):
        return {"ok": False, "node_id": node_id, "message": "node not found"}
    save_nodes(remaining)
    return {"ok": True, "node_id": node_id, "deleted": True, "remaining_count": len(remaining)}


def node_delete_migration_plan(source_node: dict[str, Any]) -> dict[str, Any]:
    source_id = str(source_node.get("id") or "")
    library = media_library_payload()
    source_disk = next((item for item in library.get("nodes") or [] if str(item.get("node_id") or "") == source_id), {})
    if not source_disk.get("online"):
        return {"ok": True, "online": False, "plan": [], "message": "source node offline; only the Hub record can be deleted"}
    candidates: list[dict[str, Any]] = []
    for disk in library.get("nodes") or []:
        node_id = str(disk.get("node_id") or "")
        if node_id == source_id or not disk.get("online"):
            continue
        node = node_by_id(node_id)
        if node and node.get("enabled", True):
            candidates.append({"node": node, "node_id": node_id, "free": int(disk.get("free") or 0)})
    candidates.sort(key=lambda item: int(item.get("free") or 0), reverse=True)
    if not candidates:
        return {"ok": False, "message": "没有其它在线 Agent 可承接该节点资源"}

    required: list[dict[str, Any]] = []
    for resource in library.get("resources") or []:
        copies = resource.get("copies") or []
        source_copy = next((copy for copy in copies if str(copy.get("node_id") or "") == source_id), None)
        if not source_copy or any(str(copy.get("node_id") or "") != source_id for copy in copies):
            continue
        required.append({
            "name": str(resource.get("name") or source_copy.get("video_path") or "").strip(),
            "video_path": str(source_copy.get("video_path") or resource.get("name") or "").strip(),
            "size": int(resource.get("size") or 0),
        })

    plan: list[dict[str, Any]] = []
    for item in sorted(required, key=lambda value: int(value.get("size") or 0), reverse=True):
        size = int(item.get("size") or 0)
        target = next((candidate for candidate in candidates if int(candidate.get("free") or 0) >= size), None)
        if not target:
            return {
                "ok": False,
                "message": f"没有足够容量迁移：{item.get('name') or item.get('video_path')} ({file_size_label(size)})",
                "required_count": len(required),
            }
        target["free"] = int(target.get("free") or 0) - size
        plan.append({**item, "target_node": target["node"], "target_node_id": target["node_id"]})
    return {"ok": True, "online": True, "plan": plan, "required_count": len(required)}


def run_node_delete_migration_task(
    task_id: str,
    source_node: dict[str, Any],
    plan: list[dict[str, Any]],
    progress_base_url: str,
    *,
    delete_after: bool = True,
) -> None:
    source_id = str(source_node.get("id") or "")
    results: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(plan):
            target_node = item["target_node"]
            target_id = str(target_node.get("id") or "")
            update_share_task(
                task_id,
                status="running",
                source_node_id=source_id,
                target_node_ids=[target_id],
                media=item.get("video_path") or item.get("name"),
                message=f"删除节点前迁移资源 {index + 1}/{len(plan)}：{item.get('name')}",
                migration_total_files=len(plan),
                migration_done_files=index,
                done_bytes=0,
                total_bytes=0,
            )
            run_share_task(
                task_id,
                source_node,
                [target_node],
                str(item.get("video_path") or item.get("name") or ""),
                f"{progress_base_url}/api/media/share/progress/{task_id}",
            )
            snapshot = share_task_snapshot(task_id) or {}
            if snapshot.get("status") == "failed":
                raise RuntimeError(snapshot.get("error") or snapshot.get("message") or "resource migration failed")
            results.append({"ok": True, "media": item.get("name") or item.get("video_path"), "target_node_id": target_id})

        deleted_node_id = ""
        if delete_after:
            removed = remove_node_record(source_id)
            if not removed.get("ok"):
                raise RuntimeError(removed.get("message") or "node record delete failed after migration")
            deleted_node_id = source_id
        update_share_task(
            task_id,
            status="done",
            message=(
                f"资源迁移完成，节点记录已删除：{source_node.get('name') or source_id}"
                if delete_after
                else f"资源迁移完成：{source_node.get('name') or source_id}"
            ),
            migration_total_files=len(plan),
            migration_done_files=len(plan),
            deleted_node_id=deleted_node_id,
            results=results,
        )
    except Exception as exc:
        update_share_task(
            task_id,
            status="failed",
            message="删除节点前资源迁移失败；节点记录已保留",
            error=str(exc),
            migration_total_files=len(plan),
            results=results,
        )


def public_upload_summary(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key != "token"}


def upload_policy() -> dict[str, Any]:
    return {
        "name": UPLOAD_POLICY_NAME,
        "safety": {
            "token_storage": "memory-only",
            "public_window_ttl_seconds": NODE_PUBLIC_UPLOAD_TTL_SECONDS,
            "close_public_window_on_success": True,
            "close_public_window_on_failure": True,
            "cancel_partial_upload_on_failure": True,
            "min_free_after_upload_bytes": MIN_FREE_AFTER_UPLOAD_BYTES,
            "max_hub_upload_bytes": APP.config["MAX_CONTENT_LENGTH"],
        },
        "stability": {
            "chunk_retries": NODE_UPLOAD_RETRIES,
            "chunk_timeout_seconds": NODE_UPLOAD_TIMEOUT_SECONDS,
            "probe_before_public_upload": True,
            "probe_timeout_seconds": NODE_UPLOAD_PROBE_TIMEOUT_SECONDS,
            "public_to_internal_fallback": False,
        },
        "speed": {
            "route_preference": "public-window, public-direct",
            "public_chunk_bytes": NODE_PUBLIC_UPLOAD_CHUNK_BYTES,
            "probe_bytes": NODE_UPLOAD_PROBE_BYTES,
            "min_public_upload_bytes_per_second": MIN_PUBLIC_UPLOAD_BYTES_PER_SECOND,
        },
    }


def policy_brief() -> dict[str, Any]:
    policy = upload_policy()
    return {
        "name": policy["name"],
        "safety": "memory-only-secret/auto-close/cancel-partial/disk-guard",
        "stability": "probe/retry/public-only/no-internal-fallback",
        "speed": "public-only/probe-measured/chunked",
    }


def rotate_push_audit_log() -> None:
    if PUSH_AUDIT_LOG.exists() and PUSH_AUDIT_LOG.stat().st_size > PUSH_AUDIT_LOG_MAX_BYTES:
        PUSH_AUDIT_LOG.replace(PUSH_AUDIT_LOG.with_suffix(".jsonl.1"))


def append_push_audit(event: dict[str, Any]) -> None:
    ensure_dirs()
    rotate_push_audit_log()
    safe_event = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy": policy_brief(),
        **event,
    }
    with PUSH_AUDIT_LOG.open("a", encoding="utf-8") as audit_file:
        audit_file.write(json.dumps(safe_event, ensure_ascii=False, separators=(",", ":")) + "\n")


def recent_push_audit(limit: int = 50) -> list[dict[str, Any]]:
    ensure_dirs()
    if not PUSH_AUDIT_LOG.exists():
        return []
    lines = PUSH_AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 200)):]
    events = []
    for line in lines:
        with suppress(Exception):
            events.append(json.loads(line))
    return events


def probe_upload_route(route: dict[str, Any]) -> dict[str, Any]:
    payload = b"0" * max(1, NODE_UPLOAD_PROBE_BYTES)
    started_at = time.time()
    try:
        resp = requests.post(
            f"{str(route['upload_base_url']).rstrip('/')}/api/upload-probe",
            data=payload,
            headers={**(route.get("headers") or {}), "Content-Type": "application/octet-stream"},
            timeout=NODE_UPLOAD_PROBE_TIMEOUT_SECONDS,
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"message": resp.text[:500]}
        elapsed = max(0.001, time.time() - started_at)
        ok = resp.ok and bool(data.get("ok", False))
        bytes_per_second = int(len(payload) / elapsed)
        if ok and bytes_per_second < MIN_PUBLIC_UPLOAD_BYTES_PER_SECOND:
            ok = False
            data["message"] = (
                f"public probe too slow: {file_size_label(bytes_per_second)}/s "
                f"< {file_size_label(MIN_PUBLIC_UPLOAD_BYTES_PER_SECOND)}/s"
            )
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "elapsed_seconds": round(elapsed, 3),
            "bytes_per_second": bytes_per_second,
            "rate_label": f"{file_size_label(bytes_per_second)}/s",
            "message": data.get("message") or ("probe ok" if ok else "probe failed"),
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def make_public_upload_route(node: dict[str, Any], warnings: list[str] | None = None) -> dict[str, Any]:
    base_url = node_base_url(node)
    return {
        "base_url": base_url,
        "upload_base_url": "",
        "route": "public-pending",
        "route_label": "public only",
        "token": "",
        "headers": node_headers(node),
        "opened_public_window": False,
        "chunk_bytes": NODE_PUBLIC_UPLOAD_CHUNK_BYTES,
        "public_status": {},
        "warnings": list(warnings or []),
        "decision_log": [],
        "last_heartbeat_at": 0.0,
        "probe": {"ok": False, "skipped": True, "message": "public route not selected yet"},
        "fallback_from": "",
    }


def select_node_upload_route(
    node: dict[str, Any],
    *,
    upload_id: str,
    filename: str,
    total_size: int,
) -> dict[str, Any]:
    base_url = node_base_url(node)
    if not base_url:
        raise ValueError("missing node base_url")

    status = request_node_json(node, "/api/public-upload", timeout=10)
    route = make_public_upload_route(node)
    route["public_status"] = public_upload_summary(status)
    if not status.get("ok"):
        raise RuntimeError(status.get("message") or "公网上传状态不可用；已禁止使用 Tailscale 内网上传")

    public_origin = str(status.get("public_origin") or "").rstrip("/")
    restrict_public = bool(status.get("restrict_public_to_upload"))
    supports_window = bool(status.get("window_supported"))
    route["decision_log"].append(
        f"public status ok; supported={supports_window}; restricted={restrict_public}; origin={public_origin or '-'}"
    )

    if supports_window:
        opened = post_node_json(
            node,
            "/api/public-upload/open",
            {
                "ttl_seconds": NODE_PUBLIC_UPLOAD_TTL_SECONDS,
                "mode": "auto",
                "reason": "stream-control-hub-media-push",
            },
            timeout=20,
        )
        if opened.get("ok"):
            token = str(opened.get("token") or "")
            opened_origin = str(opened.get("public_origin") or public_origin).rstrip("/")
            if opened_origin:
                route["decision_log"].append("public window opened; probing public route")
                route.update({
                    "upload_base_url": opened_origin,
                    "route": "public-window",
                    "route_label": "public window",
                    "token": token,
                    "headers": {
                        **node_headers(node),
                        **({"X-Public-Upload-Token": token} if token else {}),
                    },
                    "opened_public_window": True,
                    "chunk_bytes": NODE_PUBLIC_UPLOAD_CHUNK_BYTES,
                    "public_status": public_upload_summary(opened),
                    "last_heartbeat_at": time.time(),
                })
                probe = probe_upload_route(route)
                route["probe"] = probe
                if probe.get("ok"):
                    route["decision_log"].append(
                        f"public probe ok at {probe.get('rate_label')}; using public window route"
                    )
                    return route
                route["warnings"].append(probe.get("message") or "public upload probe failed")
                route["decision_log"].append("public probe failed; internal fallback is disabled")
                close_node_public_upload(node, route, reason="stream-control-hub-public-probe-failed")
                raise RuntimeError(probe.get("message") or "公网线路测速失败；不会回退 Tailscale 内网")
        route["warnings"].append(opened.get("message") or "failed to open public upload window")
        route["decision_log"].append("failed to open public window; considering direct public or internal route")

    if public_origin:
        headers = node_headers(node)
        if restrict_public or bool(status.get("ticket_required")):
            ticket = request_node_upload_ticket(node, upload_id=upload_id, filename=filename, total_size=total_size)
            if not ticket.get("ok"):
                route["warnings"].append(ticket.get("message") or "failed to issue public upload ticket")
                raise RuntimeError(ticket.get("message") or "无法获取公网上传票据；不会回退 Tailscale 内网")
            ticket_value = str(ticket.get("ticket") or "")
            if not ticket_value:
                route["warnings"].append("public upload ticket response did not include a ticket")
                raise RuntimeError("公网上传票据缺失；不会回退 Tailscale 内网")
            headers = {"X-Upload-Ticket": ticket_value}
            route["token"] = ticket_value
            route["decision_log"].append("public upload ticket issued; probing public route")
        route["decision_log"].append(
            "probing discovered public route with upload ticket"
            if restrict_public
            else "public origin is unrestricted; probing direct public route"
        )
        route.update({
            "upload_base_url": public_origin,
            "route": "public-direct",
            "route_label": "public direct",
            "headers": headers,
            "chunk_bytes": NODE_PUBLIC_UPLOAD_CHUNK_BYTES,
        })
        probe = probe_upload_route(route)
        route["probe"] = probe
        if not probe.get("ok"):
            route["warnings"].append(probe.get("message") or "public direct probe failed")
            route["decision_log"].append("direct public probe failed; internal fallback is disabled")
            raise RuntimeError(probe.get("message") or "公网线路测速失败；不会回退 Tailscale 内网")
        route["decision_log"].append(f"direct public probe ok at {probe.get('rate_label')}; using direct public route")
    else:
        raise RuntimeError("节点没有可用的公网上传地址；请配置 upload_base_url 或 Agent 公网来源")
    return route


def route_summary(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "route": route.get("route"),
        "route_label": route.get("route_label"),
        "upload_base_url": route.get("upload_base_url"),
        "opened_public_window": bool(route.get("opened_public_window")),
        "chunk_bytes": route.get("chunk_bytes"),
        "warnings": route.get("warnings") or [],
        "decision_log": route.get("decision_log") or [],
        "probe": route.get("probe") or {},
        "fallback_from": route.get("fallback_from") or "",
    }


def touch_node_public_upload(node: dict[str, Any], route: dict[str, Any]) -> None:
    if not route.get("opened_public_window") or not route.get("token"):
        return
    now = time.time()
    if now - float(route.get("last_heartbeat_at") or 0) < 30:
        return
    result = post_node_json(
        node,
        "/api/public-upload/heartbeat",
        {
            "ttl_seconds": NODE_PUBLIC_UPLOAD_TTL_SECONDS,
            "reason": "stream-control-hub-media-push-heartbeat",
            "token": route.get("token"),
        },
        timeout=15,
    )
    if not result.get("ok"):
        raise RuntimeError(result.get("message") or "failed to refresh public upload window")
    route["last_heartbeat_at"] = now


def close_node_public_upload(node: dict[str, Any], route: dict[str, Any], *, reason: str) -> dict[str, Any]:
    if not route.get("opened_public_window"):
        return {"ok": True, "skipped": True}
    result = post_node_json(
        node,
        "/api/public-upload/close",
        {"release_auto": True, "reason": reason},
        timeout=20,
    )
    return public_upload_summary(result)


def cancel_node_upload(node: dict[str, Any], upload_id: str) -> dict[str, Any]:
    return post_node_json(node, "/api/upload-chunk/cancel", {"upload_id": upload_id}, timeout=30)


def upload_chunk_with_retries(
    media_path: Path,
    route: dict[str, Any],
    *,
    upload_id: str,
    chunk_index: int,
    total_chunks: int,
    offset: int,
    total_size: int,
    chunk_size: int,
) -> dict[str, Any]:
    payload: dict[str, Any] | None = None
    for attempt in range(NODE_UPLOAD_RETRIES + 1):
        payload = upload_chunk_to_node(
            media_path,
            route,
            upload_id=upload_id,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            offset=offset,
            total_size=total_size,
            chunk_size=chunk_size,
        )
        if payload.get("ok"):
            break
        if attempt < NODE_UPLOAD_RETRIES:
            time.sleep(min(5, 0.8 * (attempt + 1)))
    return payload or {}


def upload_chunk_to_node(
    media_path: Path,
    route: dict[str, Any],
    *,
    upload_id: str,
    chunk_index: int,
    total_chunks: int,
    offset: int,
    total_size: int,
    chunk_size: int,
) -> dict[str, Any]:
    with media_path.open("rb") as stream:
        stream.seek(offset)
        chunk_bytes = stream.read(min(chunk_size, total_size - offset))

    data = {
        "upload_id": upload_id,
        "filename": media_path.name,
        "chunk_index": str(chunk_index),
        "total_chunks": str(total_chunks),
        "offset": str(offset),
        "total_size": str(total_size),
        "chunk_size": str(chunk_size),
    }
    files = {"chunk": (media_path.name, chunk_bytes, "application/octet-stream")}
    try:
        resp = requests.post(
            f"{str(route['upload_base_url']).rstrip('/')}/api/upload-chunk",
            data=data,
            files=files,
            headers=route.get("headers") or {},
            timeout=NODE_UPLOAD_TIMEOUT_SECONDS,
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = {"message": resp.text[:500]}
        payload.setdefault("status_code", resp.status_code)
        payload["ok"] = resp.ok and bool(payload.get("ok", False))
        return payload
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def push_media_to_node(node: dict[str, Any], media_path: Path) -> dict[str, Any]:
    node_id = str(node.get("id") or "")
    upload_id = f"hub_{uuid.uuid4().hex}"
    total_size = media_path.stat().st_size
    if total_size <= 0:
        return {"node_id": node_id, "ok": False, "message": "media file is empty"}

    started_at = time.time()
    route: dict[str, Any] | None = None
    last_payload: dict[str, Any] = {}
    received_size = 0
    try:
        route = select_node_upload_route(node, upload_id=upload_id, filename=media_path.name, total_size=total_size)
        chunk_size = int(route.get("chunk_bytes") or NODE_UPLOAD_CHUNK_BYTES)
        total_chunks = (total_size + chunk_size - 1) // chunk_size

        for chunk_index in range(total_chunks):
            offset = chunk_index * chunk_size
            touch_node_public_upload(node, route)
            last_payload = upload_chunk_with_retries(
                media_path,
                route,
                upload_id=upload_id,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                offset=offset,
                total_size=total_size,
                chunk_size=chunk_size,
            )
            if not last_payload.get("ok"):
                close_node_public_upload(node, route, reason="stream-control-hub-public-transfer-failed")
                raise RuntimeError(
                    (last_payload.get("message") or f"公网分片 {chunk_index + 1} 上传失败")
                    + "；已禁止回退 Tailscale 内网"
                )
            received_size = max(received_size, int(last_payload.get("received_size") or 0))

        if not last_payload.get("complete"):
            raise RuntimeError("node did not report upload completion")

        close_result = close_node_public_upload(node, route, reason="stream-control-hub-media-push-complete")
        elapsed = max(0.001, time.time() - started_at)
        audit_event = {
            "node_id": node_id,
            "ok": True,
            "media": media_path.name,
            "size": total_size,
            "received_size": received_size,
            "elapsed_seconds": round(elapsed, 2),
            "average_bytes_per_second": int(total_size / elapsed),
            "route": route_summary(route),
            "video_path": last_payload.get("video_path"),
            "close_public_window": close_result,
        }
        append_push_audit(audit_event)
        return {
            "node_id": node_id,
            "ok": True,
            "message": "media pushed to node",
            "policy": policy_brief(),
            "media": media_path.name,
            "size": total_size,
            "size_label": file_size_label(total_size),
            "received_size": received_size,
            "elapsed_seconds": round(elapsed, 2),
            "average_bytes_per_second": int(total_size / elapsed),
            "average_rate_label": f"{file_size_label(int(total_size / elapsed))}/s",
            "video_path": last_payload.get("video_path"),
            "route": route_summary(route),
            "close_public_window": close_result,
            "audit_recorded": True,
        }
    except Exception as exc:
        cleanup = cancel_node_upload(node, upload_id)
        close_result: dict[str, Any] = {"ok": True, "skipped": True}
        if route:
            with suppress(Exception):
                close_result = close_node_public_upload(node, route, reason="stream-control-hub-media-push-failed")
        failure_event = {
            "node_id": node_id,
            "ok": False,
            "message": str(exc),
            "media": media_path.name,
            "received_size": received_size,
            "last_response": public_upload_summary(last_payload),
            "route": route_summary(route) if route else None,
            "cleanup": public_upload_summary(cleanup),
            "close_public_window": close_result,
        }
        with suppress(Exception):
            append_push_audit(failure_event)
        return {
            "node_id": node_id,
            "ok": False,
            "message": str(exc),
            "policy": policy_brief(),
            "media": media_path.name,
            "received_size": received_size,
            "last_response": public_upload_summary(last_payload),
            "route": route_summary(route) if route else None,
            "cleanup": public_upload_summary(cleanup),
            "close_public_window": close_result,
            "audit_recorded": True,
        }


def media_allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_MEDIA_EXTENSIONS


def file_size_label(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.1f} {units[index]}" if index else f"{int(value)} B"


def ensure_media_disk_space(incoming_size: int) -> None:
    if incoming_size <= 0:
        return
    usage = shutil.disk_usage(MEDIA_DIR)
    required_free = incoming_size + max(MIN_FREE_AFTER_UPLOAD_BYTES, int(incoming_size * 0.1))
    if usage.free < required_free:
        raise RuntimeError(
            f"not enough disk space: need {file_size_label(required_free)}, free {file_size_label(usage.free)}"
        )


def list_media() -> list[dict[str, Any]]:
    ensure_dirs()
    items = []
    for path in sorted(MEDIA_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_file() or path.suffix.lower() not in ALLOWED_MEDIA_EXTENSIONS:
            continue
        stat = path.stat()
        items.append({
            "name": path.name,
            "size": stat.st_size,
            "size_label": file_size_label(stat.st_size),
            "modified": stat.st_mtime,
            "modified_label": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        })
    return items


@APP.get("/api/settings")
def api_hub_settings():
    settings = load_hub_settings()
    return jsonify({"ok": True, "hub_name": str(settings.get("hub_name") or "Stream Control Hub")})


@APP.post("/api/settings")
def api_save_hub_settings():
    payload = request.get_json(silent=True) or {}
    hub_name = " ".join(str(payload.get("hub_name") or "").split()).strip()
    if not hub_name:
        return jsonify({"ok": False, "message": "Hub name is required"}), 400
    if len(hub_name) > 80:
        return jsonify({"ok": False, "message": "Hub name is limited to 80 characters"}), 400
    settings = load_hub_settings()
    settings["hub_name"] = hub_name
    save_hub_settings(settings)
    return jsonify({"ok": True, "hub_name": hub_name})


@APP.get("/api/media-library")
def api_media_library():
    return jsonify(media_library_payload())


@APP.post("/api/media-groups")
def api_media_groups():
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "create").strip().lower()
    group_id = secure_filename(str(payload.get("group_id") or "").strip())
    name = " ".join(str(payload.get("name") or "").split()).strip()
    metadata = load_media_groups()
    groups = list(metadata.get("groups") or [])
    if action == "create":
        if not name or len(name) > 80:
            return jsonify({"ok": False, "message": "group name is required and limited to 80 characters"}), 400
        if len(groups) >= 6:
            return jsonify({"ok": False, "message": "最多支持 6 个分组"}), 400
        group_id = f"group-{uuid.uuid4().hex[:10]}"
        groups.append({"id": group_id, "name": name, "created_at": time.time()})
    elif action == "rename":
        target = next((item for item in groups if str(item.get("id")) == group_id), None)
        if not target:
            return jsonify({"ok": False, "message": "group not found"}), 404
        if not name or len(name) > 80:
            return jsonify({"ok": False, "message": "group name is required and limited to 80 characters"}), 400
        target["name"] = name
    elif action == "delete":
        if not any(str(item.get("id")) == group_id for item in groups):
            return jsonify({"ok": False, "message": "group not found"}), 404
        groups = [item for item in groups if str(item.get("id")) != group_id]
        metadata["assignments"] = {
            key: value for key, value in (metadata.get("assignments") or {}).items() if str(value) != group_id
        }
    else:
        return jsonify({"ok": False, "message": "unsupported group action"}), 400
    metadata["groups"] = groups
    save_media_groups(metadata)
    return jsonify({"ok": True, "group_id": group_id, "groups": groups})


@APP.post("/api/media-library/assign")
def api_media_library_assign():
    payload = request.get_json(silent=True) or {}
    filename = Path(str(payload.get("filename") or "").strip()).name
    group_id = secure_filename(str(payload.get("group_id") or "").strip())
    if not filename:
        return jsonify({"ok": False, "message": "filename is required"}), 400
    metadata = load_media_groups()
    if group_id and not any(str(item.get("id")) == group_id for item in metadata.get("groups") or []):
        return jsonify({"ok": False, "message": "group not found"}), 404
    assignments = dict(metadata.get("assignments") or {})
    if group_id:
        assignments[filename] = group_id
    else:
        assignments.pop(filename, None)
    metadata["assignments"] = assignments
    save_media_groups(metadata)
    return jsonify({"ok": True, "filename": filename, "group_id": group_id})


@APP.post("/api/media-library/cleanup")
def api_media_library_cleanup():
    payload = request.get_json(silent=True) or {}
    try:
        created_before_days = max(0, int(payload.get("created_before_days") or 0))
        unused_days = max(0, int(payload.get("unused_days") or 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "invalid cleanup age"}), 400
    usage_mode = str(payload.get("usage_mode") or "any").strip().lower()
    if usage_mode not in {"any", "never", "unused"}:
        return jsonify({"ok": False, "message": "invalid usage mode"}), 400
    execute = bool(payload.get("execute"))
    metadata = load_media_groups()
    result = cleanup_verified_duplicates(
        metadata,
        execute=execute,
        created_before_days=created_before_days,
        usage_mode=usage_mode,
        unused_days=unused_days,
    )
    return jsonify(result)


@APP.get("/api/nodes")
def api_nodes():
    result = []
    profile_map = node_youtube_profile_map()
    profiles = {profile["id"]: public_youtube_profile(profile) for profile in load_youtube_profiles_config()["profiles"]}
    default_profile = active_youtube_profile_id()
    for node in load_nodes():
        node_view = dict(node)
        node_view.pop("token", None)
        node_view.pop("control_token", None)
        profile_id = profile_map.get(str(node.get("id") or ""), default_profile)
        profile = profiles.get(profile_id) or profiles.get(default_profile) or {}
        node_view["youtube_profile_id"] = profile_id
        node_view["youtube_profile_name"] = str(profile.get("name") or profile_id)
        node_view["health"] = request_node_json(node, "/api/status") if node.get("enabled", True) else {"ok": False}
        urls = node_role_urls(node)
        agent_health = node_view["health"]
        agent_info = agent_health.get("agent") or {}
        node_view["roles"] = {
            "agent": {
                "enabled": bool(agent_health.get("ok")),
                "version": str(agent_info.get("version") or "unrecognized"),
                "url": urls["agent"],
            },
            "hub": request_hub_role_status(node),
        }
        result.append(node_view)
    return jsonify(result)


@APP.post("/api/nodes/youtube-profile")
def api_node_youtube_profile():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    profile_id = safe_youtube_profile_id(str(payload.get("profile_id") or ""))
    if not node_by_id(node_id):
        return jsonify({"ok": False, "message": "node not found"}), 404
    try:
        saved_profile_id = set_node_youtube_profile(node_id, profile_id)
    except Exception as exc:
        return youtube_error_response(exc)
    profile = public_youtube_profile(youtube_profile_by_id(saved_profile_id))
    return jsonify({
        "ok": True,
        "node_id": node_id,
        "profile_id": saved_profile_id,
        "profile": profile,
    })


@APP.post("/api/nodes/note")
def api_node_note():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    note = str(payload.get("note") or "").replace("\r", " ").replace("\n", " ").strip()
    if len(note) > 500:
        return jsonify({"ok": False, "message": "note is limited to 500 characters"}), 400
    nodes = load_nodes()
    for node in nodes:
        if str(node.get("id") or "") == node_id:
            if note:
                node["note"] = note
            else:
                node.pop("note", None)
            save_nodes(nodes)
            return jsonify({"ok": True, "node_id": node_id, "note": note})
    return jsonify({"ok": False, "message": "node not found"}), 404


@APP.post("/api/nodes/name")
def api_node_name():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    name = " ".join(str(payload.get("name") or "").split()).strip()
    if not name:
        return jsonify({"ok": False, "message": "node name is required"}), 400
    if len(name) > 80:
        return jsonify({"ok": False, "message": "node name is limited to 80 characters"}), 400
    nodes = load_nodes()
    for node in nodes:
        if str(node.get("id") or "") == node_id:
            node["name"] = name
            save_nodes(nodes)
            return jsonify({"ok": True, "node_id": node_id, "name": name})
    return jsonify({"ok": False, "message": "node not found"}), 404


@APP.post("/api/nodes/delete")
def api_node_delete():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    if not node_id:
        return jsonify({"ok": False, "message": "node_id is required"}), 400
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "node_id": node_id, "message": "node not found"}), 404
    migrate = payload.get("migrate_resources", True) is not False
    if not migrate:
        result = remove_node_record(node_id)
        return jsonify(result), 200 if result.get("ok") else 404

    plan_result = node_delete_migration_plan(node)
    if not plan_result.get("ok"):
        return jsonify({"ok": False, "node_id": node_id, **plan_result}), 409
    plan = plan_result.get("plan") or []
    if not plan:
        result = remove_node_record(node_id)
        return jsonify({
            **result,
            "migration_required": False,
            "migration_message": plan_result.get("message") or "没有只存在于该节点的资源需要迁移",
        }), 200 if result.get("ok") else 404

    task_id = f"delete_node_{uuid.uuid4().hex}"
    with SHARE_TASKS_LOCK:
        SHARE_TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "source_node_id": node_id,
            "target_node_ids": [str(item.get("target_node_id") or "") for item in plan],
            "media": "node-delete-migration",
            "message": "删除节点前资源迁移任务已创建",
            "done_bytes": 0,
            "total_bytes": 0,
            "current_bps": 0,
            "average_bps": 0,
            "migration_total_files": len(plan),
            "migration_done_files": 0,
            "results": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    worker = threading.Thread(
        target=run_node_delete_migration_task,
        args=(task_id, node, plan, request.host_url.rstrip("/")),
        daemon=True,
    )
    worker.start()
    return jsonify({"ok": True, "accepted": True, "migration_required": True, **share_task_payload(share_task_snapshot(task_id) or {})}), 202


@APP.post("/api/nodes/resources/migrate")
def api_node_resources_migrate():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    if not node_id:
        return jsonify({"ok": False, "message": "node_id is required"}), 400
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "node_id": node_id, "message": "node not found"}), 404
    plan_result = node_delete_migration_plan(node)
    if not plan_result.get("ok"):
        return jsonify({"ok": False, "node_id": node_id, **plan_result}), 409
    plan = plan_result.get("plan") or []
    if not plan:
        return jsonify({
            "ok": True,
            "node_id": node_id,
            "migration_required": False,
            "message": plan_result.get("message") or "没有只存在于该节点的资源需要迁移",
        })
    task_id = f"migrate_node_{uuid.uuid4().hex}"
    with SHARE_TASKS_LOCK:
        SHARE_TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "source_node_id": node_id,
            "target_node_ids": [str(item.get("target_node_id") or "") for item in plan],
            "media": "node-resource-migration",
            "message": "节点资源迁移任务已创建",
            "done_bytes": 0,
            "total_bytes": 0,
            "current_bps": 0,
            "average_bps": 0,
            "migration_total_files": len(plan),
            "migration_done_files": 0,
            "results": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    worker = threading.Thread(
        target=run_node_delete_migration_task,
        args=(task_id, node, plan, request.host_url.rstrip("/")),
        kwargs={"delete_after": False},
        daemon=True,
    )
    worker.start()
    return jsonify({"ok": True, "accepted": True, "migration_required": True, **share_task_payload(share_task_snapshot(task_id) or {})}), 202


@APP.post("/api/nodes/import")
def api_import_nodes():
    payload = request.get_json(silent=True) or {}
    incoming = payload.get("nodes") or []
    if not isinstance(incoming, list):
        return jsonify({"ok": False, "message": "nodes must be a list"}), 400
    current = load_nodes()
    by_id = {str(node.get("id") or ""): dict(node) for node in current if str(node.get("id") or "")}
    imported = 0
    skipped = 0
    for raw in incoming:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        node = dict(raw)
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            skipped += 1
            continue
        node["id"] = node_id
        by_id[node_id] = node
        imported += 1
    save_nodes(list(by_id.values()))
    return jsonify({"ok": True, "imported_count": imported, "skipped_count": skipped, "total_count": len(by_id)})


@APP.post("/api/hub-transfer/nodes")
def api_transfer_nodes_to_hub():
    payload = request.get_json(silent=True) or {}
    target = str(payload.get("target_hub_url") or "").strip().rstrip("/")
    token = str(payload.get("target_token") or "").strip()
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return jsonify({"ok": False, "message": "target_hub_url must be an http(s) Hub address"}), 400
    headers = {"X-Control-Token": token} if token else {}
    result = post_url_json(f"{target}/api/nodes/import", {"nodes": load_nodes(), "source_hub": request.host_url.rstrip("/")}, timeout=30, headers=headers)
    status_code = 200 if result.get("ok") else int(result.get("status_code") or 502)
    return jsonify({"target_hub_url": target, **result}), status_code


@APP.post("/api/hubs/sync")
def api_sync_all_hubs():
    current_nodes = load_nodes()
    source_hub = request.host_url.rstrip("/")
    results: list[dict[str, Any]] = []
    for node in current_nodes:
        hub = request_hub_role_status(node)
        if not hub.get("enabled") or not hub.get("url"):
            continue
        hub_url = str(hub.get("url") or "").rstrip("/")
        result = post_url_json(
            f"{hub_url}/api/nodes/import",
            {"nodes": current_nodes, "source_hub": source_hub},
            timeout=30,
        )
        results.append({
            "node_id": str(node.get("id") or ""),
            "node_name": str(node.get("name") or node.get("id") or ""),
            "hub_url": hub_url,
            **result,
        })
    ok_count = sum(1 for item in results if item.get("ok"))
    return jsonify({
        "ok": ok_count == len(results),
        "target_count": len(results),
        "ok_count": ok_count,
        "results": results,
    })


@APP.post("/api/hubs/switch-target")
def api_hub_switch_target():
    payload = request.get_json(silent=True) or {}
    requested_id = str(payload.get("node_id") or "").strip()
    nodes = load_nodes()

    def usable_hub(node: dict[str, Any]) -> dict[str, Any] | None:
        hub = request_hub_role_status(node)
        if hub.get("enabled") and hub.get("ok") and hub.get("url"):
            return {
                "node_id": str(node.get("id") or ""),
                "node_name": str(node.get("name") or node.get("id") or ""),
                "url": str(hub.get("url") or "").rstrip("/"),
                "hub": hub,
            }
        return None

    requested_node = next((node for node in nodes if str(node.get("id") or "") == requested_id), None)
    requested_result = usable_hub(requested_node) if requested_node else None
    if requested_result:
        return jsonify({"ok": True, "fallback": False, **requested_result})

    for node in nodes:
        if str(node.get("id") or "") == requested_id:
            continue
        result = usable_hub(node)
        if result:
            return jsonify({"ok": True, "fallback": True, "requested_node_id": requested_id, **result})

    return jsonify({"ok": False, "requested_node_id": requested_id, "message": "没有可用 Hub"}), 409


@APP.get("/api/role-status")
def api_hub_role_status():
    host = (request.host.split(":", 1)[0] or "127.0.0.1").strip("[]")
    return jsonify({
        "ok": True,
        "roles": {
            "hub": {"enabled": True, "version": local_git_version(), "url": f"http://{host}:{PORT}"},
            "agent": {"enabled": service_active("stream-control-headless-agent.service"), "url": f"http://{host}:8787"},
        },
    })


@APP.post("/api/roles/agent/activate")
def api_activate_agent_role():
    payload = request.get_json(silent=True) or {}
    control_hub_url = str(payload.get("control_hub_url") or request.host_url.rstrip("/")).strip().rstrip("/")
    parsed = urlparse(control_hub_url)
    if not parsed.hostname or not is_private_or_loopback_host(parsed.hostname):
        return jsonify({"ok": False, "message": "control_hub_url must use a private or Tailscale address"}), 400
    try:
        result = schedule_agent_role_activation(control_hub_url)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409
    return jsonify({"ok": True, "accepted": True, "message": "Agent activation scheduled; Hub remains active", "result": result}), 202


@APP.post("/api/roles/hub/deactivate")
def api_deactivate_hub_role():
    try:
        result = schedule_hub_deactivation()
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409
    return jsonify({"ok": True, "accepted": True, "message": "Hub deactivation scheduled; local data is preserved", "result": result}), 202


@APP.post("/api/roles/agent/deactivate")
def api_deactivate_agent_role_from_hub():
    return jsonify({"ok": False, "message": "Agent role can only be deactivated through the Agent service"}), 409


@APP.post("/api/upgrade")
def api_upgrade_hub():
    try:
        result = schedule_hub_upgrade()
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409
    return jsonify({"ok": True, "accepted": True, "message": "Hub upgrade scheduled; Agent remains active", "result": result}), 202


@APP.get("/api/media")
def api_media():
    return jsonify(list_media())


@APP.post("/api/nodes/upload-target")
def api_node_upload_target():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    upload_id = secure_filename(str(payload.get("upload_id") or "").strip())
    original_filename = str(payload.get("filename") or "").strip()
    try:
        filename = safe_media_filename(original_filename)
        total_size = int(payload.get("total_size") or 0)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not node.get("enabled", True):
        return jsonify({"ok": False, "message": "node disabled"}), 400
    base_url = node_base_url(node)
    if not base_url:
        return jsonify({"ok": False, "message": "missing node base_url"}), 400
    if not upload_id or not filename or total_size <= 0:
        return jsonify({"ok": False, "message": "upload_id, filename and total_size are required"}), 400
    ticket = request_node_upload_ticket(node, upload_id=upload_id, filename=filename, total_size=total_size)
    if not ticket.get("ok"):
        return jsonify({
            "ok": False,
            "message": ticket.get("message") or "Agent did not issue an upload ticket",
            "status_code": ticket.get("status_code"),
        }), int(ticket.get("status_code") or 502)
    headers = {
        "X-Upload-Route": "direct-browser",
        "X-Upload-Ticket": str(ticket.get("ticket") or ""),
    }
    public_status = request_node_json(node, "/api/public-upload", timeout=10)
    discovered_public_url = (
        str(public_status.get("public_origin") or "").rstrip("/")
        if public_status.get("ok") and public_status.get("supported")
        else ""
    )
    upload_urls = []
    for url in [discovered_public_url, *node_upload_base_urls(node)]:
        if url and is_public_upload_url(url) and url not in upload_urls:
            upload_urls.append(url)
    if not upload_urls:
        return jsonify({
            "ok": False,
            "message": "该节点没有可用的公网上传地址；请配置 upload_base_url，Tailscale 内网上传已禁用",
            "node_id": node_id,
        }), 409
    candidates = []
    for url in upload_urls:
        candidates.append({
            "url": url,
            "label": upload_route_label(url, base_url),
            "upload_url": f"{url}/api/upload-chunk",
            "probe_url": f"{url}/api/upload-probe",
            "cancel_url": f"{url}/api/upload-chunk/cancel",
            "headers": headers,
        })
    verified_candidates = []
    probe_failures = []
    for candidate in candidates:
        probe = probe_upload_route({
            "upload_base_url": candidate["url"],
            "headers": candidate["headers"],
        })
        candidate["server_probe"] = probe
        if probe.get("ok"):
            verified_candidates.append(candidate)
        else:
            probe_failures.append({"url": candidate["url"], "message": probe.get("message") or "probe failed"})
    candidates = verified_candidates
    if not candidates:
        return jsonify({
            "ok": False,
            "message": "Agent 公网 HTTP 探测失败；请在云防火墙和 VPS 防火墙放行 TCP 8787",
            "node_id": node_id,
            "probe_failures": probe_failures,
        }), 502
    return jsonify({
        "ok": True,
        "node_id": node_id,
        "filename": filename,
        "original_filename": original_filename,
        "base_url": base_url,
        "upload_url": candidates[0]["upload_url"],
        "cancel_url": candidates[0]["cancel_url"],
        "probe_url": candidates[0]["probe_url"],
        "candidates": candidates,
        "chunk_bytes": DIRECT_AGENT_UPLOAD_CHUNK_BYTES,
        "headers": headers,
        "ticket_expires_in": ticket.get("expires_in"),
        "public_status": public_upload_summary(public_status),
    })


@APP.get("/api/policy")
def api_policy():
    return jsonify({
        "ok": True,
        "policy": upload_policy(),
    })


@APP.get("/api/push-audit")
def api_push_audit():
    try:
        limit = int(request.args.get("limit") or 50)
    except ValueError:
        limit = 50
    return jsonify({
        "ok": True,
        "events": recent_push_audit(limit),
    })


@APP.get("/api/tailscale/status")
def api_tailscale_status():
    return jsonify(tailscale_status())


@APP.get("/api/install-commands")
def api_install_commands():
    response = jsonify(latest_install_commands())
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@APP.get("/api/tailscale/precheck")
def api_tailscale_precheck():
    return jsonify(tailscale_precheck())


@APP.post("/api/tailscale/install")
def api_tailscale_install():
    if not dangerous_local_action_allowed():
        return reject_forbidden("Tailscale install requires localhost, trusted network, or STREAM_HUB_CONTROL_TOKEN")
    result = tailscale_install()
    return jsonify(result), 200 if result.get("ok") else 500


@APP.post("/api/tailscale/connect")
def api_tailscale_connect():
    if not dangerous_local_action_allowed():
        return reject_forbidden("Tailscale connect requires localhost, trusted network, or STREAM_HUB_CONTROL_TOKEN")
    payload = request.get_json(silent=True) or {}
    auth_key = str(payload.get("auth_key") or "").strip()
    hostname = secure_filename(str(payload.get("hostname") or "stream-control-hub").strip()) or "stream-control-hub"
    if not auth_key.startswith("tskey-"):
        return jsonify({"ok": False, "message": "valid Tailscale auth key required"}), 400
    result = tailscale_connect(
        auth_key,
        hostname,
        accept_routes=bool(payload.get("accept_routes", True)),
        ssh=bool(payload.get("ssh", False)),
    )
    return jsonify(result), 200 if result.get("ok") else 500


@APP.post("/api/tailscale/connect-existing-ip")
def api_tailscale_connect_existing_ip():
    if not dangerous_local_action_allowed():
        return reject_forbidden("Tailscale Agent 接入需要 localhost、可信网络或 STREAM_HUB_CONTROL_TOKEN")
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    agent_name = str(payload.get("name") or payload.get("agent_name") or "").strip()
    supplied_token = str(payload.get("token") or "").strip()
    raw_ip = str(payload.get("tailscale_ip") or payload.get("ip") or "").strip()
    try:
        ip = ipaddress.ip_address(raw_ip.split("%", 1)[0])
    except ValueError:
        return jsonify({"ok": False, "message": "请输入有效的 Tailscale IP，例如 100.x.x.x"}), 400
    if ip not in TAILSCALE_CGNAT:
        return jsonify({"ok": False, "message": "这个地址不是 Tailscale 100.x 地址，请确认后再连接"}), 400

    peer = online_tailscale_peer_for_ip(str(ip))
    if not peer:
        return jsonify({
            "ok": False,
            "message": "该 IP 不是当前 Tailnet 中的在线设备，请确认 Hub 与目标设备登录了同一 Tailscale 网络",
            "tailscale_ip": str(ip),
        }), 400

    base_url = f"http://{ip}:8787"
    pairing = pair_tailscale_agent(base_url)
    if not pairing.get("ok"):
        hub_url = f"http://{ip}:8788"
        hub_status = request_hub_status_url(hub_url)
        if hub_status.get("ok") and (hub_status.get("roles") or {}).get("hub"):
            node_id_for_ip = node_id
            nodes = load_nodes()
            target_index = -1
            if not node_id_for_ip:
                for index, item in enumerate(nodes):
                    if str(item.get("tailscale_ip") or "") == str(ip) or node_base_url(item) == base_url or node_role_urls(item)["hub"] == hub_url:
                        node_id_for_ip = str(item.get("id") or "")
                        target_index = index
                        break
            else:
                target_index = next((index for index, item in enumerate(nodes) if str(item.get("id") or "") == node_id_for_ip), -1)
            if target_index >= 0:
                node = dict(nodes[target_index])
            else:
                peer_name = str(peer.get("hostname") or peer.get("name") or peer.get("dns_name") or "").split(".", 1)[0].strip()
                base_id = secure_filename(node_id_for_ip or agent_name or peer_name or f"hub-{str(ip).replace('.', '-')}")
                node_id_for_ip = base_id or f"hub-{str(ip).replace('.', '-')}"
                suffix = 2
                existing_ids = {str(item.get("id") or "") for item in nodes}
                while node_id_for_ip in existing_ids:
                    node_id_for_ip = f"{base_id}-{suffix}"
                    suffix += 1
                node = {"id": node_id_for_ip, "name": agent_name or peer_name or node_id_for_ip, "role": "stream-node", "enabled": True}
            node["base_url"] = base_url
            node["hub_url"] = hub_url
            node["tailscale_ip"] = str(ip)
            node["hub_only"] = True
            node["hub_connected_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if target_index >= 0:
                nodes[target_index] = node
            else:
                nodes.append(node)
            save_nodes(nodes)
            return jsonify({
                "ok": True,
                "hub_only": True,
                "message": "?? VPS ? Hub ???? Agent 8787 ?????? Hub-only ?????????????/??/???????????? Agent?",
                "tailscale_ip": str(ip),
                "node_id": node_id_for_ip,
                "hub_url": hub_url,
                "hub_status": hub_status,
                "peer": peer,
                "created": target_index < 0,
            })
        status_code = int(pairing.get("status_code") or 502)
        message = pairing.get("message") or "目标设备未检测到兼容的 Headless Agent"
        if status_code == 404:
            message = "Tailscale 设备在线，但未安装新版 Agent；请安装或升级 Agent 后重试"
        elif status_code == 403:
            message = "Agent 已安装，但同 Tailnet 自动配对验证失败或已被禁用"
        return jsonify({
            "ok": False,
            "message": message,
            "tailscale_ip": str(ip),
            "peer": peer,
            "status_code": status_code,
        }), status_code
    supplied_token = str(pairing.get("token") or "").strip()
    agent_name = str(pairing.get("name") or pairing.get("hostname") or agent_name).strip()
    if not supplied_token:
        return jsonify({"ok": False, "message": "Agent 配对成功但未返回控制凭据，请升级 Agent"}), 502

    nodes = load_nodes()
    target_index = next((index for index, item in enumerate(nodes) if str(item.get("id")) == node_id), -1)
    if not node_id:
        target_index = next((
            index for index, item in enumerate(nodes)
            if str(item.get("tailscale_ip") or "") == str(ip)
            or node_base_url(item) == base_url
        ), -1)
        if target_index >= 0:
            node_id = str(nodes[target_index].get("id") or "")
    if node_id and target_index < 0:
        return jsonify({"ok": False, "message": "Agent 不存在"}), 404
    creating = target_index < 0
    if target_index >= 0:
        node = dict(nodes[target_index])
        node["token"] = supplied_token
    else:
        base_id = secure_filename(agent_name).strip("-_") or f"agent-{str(ip).replace('.', '-')}"
        node_id = base_id
        suffix = 2
        existing_ids = {str(item.get("id") or "") for item in nodes}
        while node_id in existing_ids:
            node_id = f"{base_id}-{suffix}"
            suffix += 1
        node = {
            "id": node_id,
            "name": agent_name or node_id,
            "role": "stream-node",
            "enabled": True,
            "token": supplied_token,
        }
    previous_base_url = node_base_url(node)
    probe_node = dict(node)
    probe_node["base_url"] = base_url
    status = request_node_json(probe_node, "/api/status", timeout=12)
    if not status.get("ok"):
        status_code = int(status.get("status_code") or 502)
        message = status.get("message") or "无法连接到这个 Tailscale IP 上的 Agent"
        if status_code == 403:
            message = "Agent 已响应但未授权；请把 STREAM_AGENT_CONTROL_HUB 设置为当前 Hub 的 Tailscale URL"
        return jsonify({
            "ok": False,
            "message": message,
            "node_id": node_id,
            "base_url": base_url,
            "status_code": status_code,
        }), status_code

    if previous_base_url and previous_base_url != base_url and not node.get("public_base_url"):
        node["public_base_url"] = previous_base_url
    node["base_url"] = base_url
    agent = status.get("agent") if isinstance(status.get("agent"), dict) else {}
    if creating:
        node["name"] = str(agent.get("name") or status.get("hostname") or node_id)
    node["tailscale_ip"] = str(ip)
    node["tailscale_connected_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if target_index >= 0:
        nodes[target_index] = node
    else:
        nodes.append(node)
    save_nodes(nodes)
    return jsonify({
        "ok": True,
        "message": "同一 Tailnet、Agent 服务和控制权限均已验证，节点已直接接入",
        "node_id": node_id,
        "base_url": base_url,
        "previous_base_url": previous_base_url,
        "hostname": status.get("hostname"),
        "platform": status.get("platform"),
        "created": creating,
    })


@APP.post("/api/media/upload")
def api_media_upload():
    ensure_dirs()
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "message": "missing file"}), 400
    if not media_allowed(upload.filename):
        return jsonify({"ok": False, "message": "unsupported media extension"}), 400
    incoming_size = int(request.content_length or 0)
    if incoming_size > 0:
        try:
            ensure_media_disk_space(incoming_size)
        except RuntimeError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 507
    try:
        name = safe_media_filename(upload.filename)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    target = MEDIA_DIR / name
    counter = 1
    while target.exists():
        target = MEDIA_DIR / f"{Path(name).stem}-{counter}{Path(name).suffix}"
        counter += 1
    upload.save(target)
    return jsonify({"ok": True, "media": target.name, "size": target.stat().st_size})


@APP.post("/api/media/push")
def api_media_push():
    payload = request.get_json(silent=True) or {}
    node_ids = [str(item) for item in payload.get("node_ids") or []]
    try:
        media_name = safe_media_filename(str(payload.get("media_name") or ""))
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    media_path = MEDIA_DIR / media_name
    if not node_ids:
        return jsonify({"ok": False, "message": "no nodes selected"}), 400
    if not media_path.exists():
        return jsonify({"ok": False, "message": "media not found"}), 404
    if not media_path.is_file() or not media_allowed(media_path.name):
        return jsonify({"ok": False, "message": "unsupported media file"}), 400

    results = []
    for node_id in node_ids:
        node = node_by_id(node_id)
        if not node:
            results.append({"node_id": node_id, "ok": False, "message": "node not found"})
            continue
        if not node.get("enabled", True):
            results.append({"node_id": node_id, "ok": False, "message": "node disabled"})
            continue
        results.append(push_media_to_node(node, media_path))
    return jsonify({
        "ok": all(item.get("ok") for item in results) if results else False,
        "media": media_name,
        "results": results,
    })


@APP.post("/api/media/share")
def api_media_share():
    payload = request.get_json(silent=True) or {}
    source_node_id = str(payload.get("source_node_id") or "").strip()
    target_node_ids = [str(item) for item in payload.get("target_node_ids") or []]
    media = str(payload.get("media") or payload.get("video_path") or "").strip()
    source_node = node_by_id(source_node_id)
    if not source_node:
        return jsonify({"ok": False, "message": "source node not found"}), 404
    if not source_node.get("enabled", True):
        return jsonify({"ok": False, "message": "source node disabled"}), 400
    if not target_node_ids:
        return jsonify({"ok": False, "message": "no target agents selected"}), 400
    if not media:
        return jsonify({"ok": False, "message": "no media selected"}), 400

    target_nodes = []
    for target_node_id in target_node_ids:
        if target_node_id == source_node_id:
            continue
        target_node = node_by_id(target_node_id)
        if not target_node:
            return jsonify({"ok": False, "message": f"target node not found: {target_node_id}"}), 404
        if not target_node.get("enabled", True):
            return jsonify({"ok": False, "message": f"target node disabled: {target_node_id}"}), 400
        target_nodes.append(target_node)
    if not target_nodes:
        return jsonify({"ok": False, "message": "no target agents selected"}), 400

    task_id = f"share_{uuid.uuid4().hex}"
    progress_url = request.host_url.rstrip("/") + f"/api/media/share/progress/{task_id}"
    with SHARE_TASKS_LOCK:
        SHARE_TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "source_node_id": source_node_id,
            "target_node_ids": [str(node.get("id") or "") for node in target_nodes],
            "media": media,
            "message": "共享任务已创建",
            "done_bytes": 0,
            "total_bytes": 0,
            "current_bps": 0,
            "average_bps": 0,
            "results": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    worker = threading.Thread(
        target=run_share_task,
        args=(task_id, source_node, target_nodes, media, progress_url),
        daemon=True,
    )
    worker.start()
    return jsonify({"ok": True, **share_task_payload(share_task_snapshot(task_id) or {})})


@APP.get("/api/media/share/status/<task_id>")
def api_media_share_status(task_id: str):
    task = share_task_snapshot(task_id)
    if not task:
        return jsonify({"ok": False, "message": "share task not found"}), 404
    return jsonify(share_task_payload(task))


@APP.post("/api/media/share/progress/<task_id>")
def api_media_share_progress(task_id: str):
    payload = request.get_json(silent=True) or {}
    task = share_task_snapshot(task_id)
    if not task:
        return jsonify({"ok": False, "message": "share task not found"}), 404
    target_index = max(0, int(payload.get("target_index") or 0))
    target_count = max(1, int(payload.get("target_count") or 1))
    single_total = int(payload.get("total_bytes") or 0)
    single_done = int(payload.get("done_bytes") or 0)
    aggregate_total = single_total * target_count if single_total else int(task.get("total_bytes") or 0)
    aggregate_done = (single_total * target_index + single_done) if single_total else single_done
    update_share_task(
        task_id,
        status="running",
        message=str(payload.get("message") or "正在共享"),
        done_bytes=aggregate_done,
        total_bytes=aggregate_total,
        single_target_total_bytes=single_total or int(task.get("single_target_total_bytes") or 0),
        current_bps=int(payload.get("current_bps") or 0),
        average_bps=int(payload.get("average_bps") or 0),
    )
    return jsonify({"ok": True})


@APP.post("/api/nodes/media/rename")
def api_node_media_rename():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    media = str(payload.get("media") or payload.get("video_path") or "").strip()
    try:
        new_name = safe_media_filename(str(payload.get("new_name") or "").strip())
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not media or not new_name:
        return jsonify({"ok": False, "message": "media and new_name are required"}), 400
    result = post_node_json(node, "/api/media/rename", {"media": media, "new_name": new_name}, timeout=30)
    if result.get("ok"):
        old_name = Path(media).name
        metadata = load_media_groups()
        assignments = dict(metadata.get("assignments") or {})
        if old_name in assignments:
            assignments[new_name] = assignments.pop(old_name)
            metadata["assignments"] = assignments
            save_media_groups(metadata)
    return jsonify({"node_id": node_id, **result}), 200 if result.get("ok") else int(result.get("status_code") or 502)


@APP.post("/api/nodes/media/delete")
def api_node_media_delete():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    media = str(payload.get("media") or payload.get("video_path") or "").strip()
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not media:
        return jsonify({"ok": False, "message": "media is required"}), 400
    result = post_node_json(node, "/api/media/delete", {"media": media}, timeout=30)
    return jsonify({"node_id": node_id, **result}), 200 if result.get("ok") else int(result.get("status_code") or 502)


def stream_payload_for_node(payload: dict[str, Any]) -> dict[str, Any]:
    stream_url = str(payload.get("stream_url") or "rtmp://a.rtmp.youtube.com/live2").strip().rstrip("/")
    stream_key = str(payload.get("stream_key") or "").strip()
    if stream_key.lower().startswith(("rtmp://", "rtmps://")):
        parsed_key = stream_key.rstrip("/")
        head, sep, tail = parsed_key.rpartition("/")
        if sep and head.lower().startswith(("rtmp://", "rtmps://")) and tail:
            stream_url = head.rstrip("/")
            stream_key = tail.strip()
    return {
        "stream_url": stream_url,
        "stream_key": stream_key,
        "youtube_profile_id": safe_youtube_profile_id(str(payload.get("youtube_profile_id") or payload.get("profile_id") or active_youtube_profile_id())),
        "youtube_stream_id": str(payload.get("youtube_stream_id") or "").strip(),
        "youtube_ingestion_url": str(payload.get("youtube_ingestion_url") or "").strip(),
        "video_path": str(payload.get("video_path") or "").strip(),
        "copy_mode": bool(payload.get("copy_mode")),
        "adaptive_mode": str(payload.get("adaptive_mode") or "auto").strip().lower() or "auto",
        "stream_output_mode": str(payload.get("stream_output_mode") or "direct").strip().lower() or "direct",
        "preset": str(payload.get("preset") or "veryfast").strip() or "veryfast",
        "video_bitrate": int(payload.get("video_bitrate") or 4500),
        "audio_bitrate": int(payload.get("audio_bitrate") or 192),
        "fps": int(payload.get("fps") or 30),
        "resolution": str(payload.get("resolution") or "1280x720").strip() or "1280x720",
        "keyframe_seconds": int(payload.get("keyframe_seconds") or 2),
    }


def redacted_stream_result(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data or {})
    result.pop("command", None)
    result.pop("youtube_ingestion_url", None)
    if isinstance(result.get("result"), dict):
        nested = dict(result["result"])
        nested.pop("command", None)
        nested.pop("youtube_ingestion_url", None)
        result["result"] = nested
    return result


def ensure_library_media_on_node(target_node: dict[str, Any], filename: str) -> dict[str, Any]:
    safe_name = safe_media_filename(filename)
    target_status = request_node_json(target_node, "/api/status", timeout=12)
    for video in target_status.get("videos") or []:
        if str(video.get("name") or "") == safe_name:
            return {"ok": True, "copied": False, "video_path": str(video.get("video_path") or safe_name)}

    source_node = None
    for candidate in load_nodes():
        if str(candidate.get("id") or "") == str(target_node.get("id") or "") or not candidate.get("enabled", True):
            continue
        status = request_node_json(candidate, "/api/status", timeout=12)
        if any(str(video.get("name") or "") == safe_name for video in status.get("videos") or []):
            source_node = candidate
            break
    if not source_node:
        return {"ok": False, "message": f"媒体库中没有可复制的在线副本：{safe_name}"}

    task_id = f"ensure_{uuid.uuid4().hex}"
    with SHARE_TASKS_LOCK:
        SHARE_TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "source_node_id": str(source_node.get("id") or ""),
            "target_node_ids": [str(target_node.get("id") or "")],
            "media": safe_name,
            "message": "开播前自动复制媒体",
            "done_bytes": 0,
            "total_bytes": 0,
            "results": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    progress_url = request.host_url.rstrip("/") + f"/api/media/share/progress/{task_id}"
    run_share_task(task_id, source_node, [target_node], safe_name, progress_url)
    task = share_task_snapshot(task_id) or {}
    if task.get("status") != "done":
        return {"ok": False, "message": task.get("error") or task.get("message") or "自动复制失败"}
    refreshed = request_node_json(target_node, "/api/status", timeout=12)
    video = next((item for item in refreshed.get("videos") or [] if str(item.get("name") or "") == safe_name), None)
    return {
        "ok": bool(video),
        "copied": True,
        "source_node_id": str(source_node.get("id") or ""),
        "video_path": str((video or {}).get("video_path") or safe_name),
        "message": "媒体已自动复制到开播节点" if video else "复制完成但目标文件未找到",
    }


@APP.post("/api/nodes/stream/recommend")
def api_nodes_stream_recommend():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "")
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not node.get("enabled", True):
        return jsonify({"ok": False, "message": "node disabled"}), 409
    node_payload = stream_payload_for_node(payload)
    node_payload["stream_key"] = ""
    youtube_client = youtube_client_for_id(str(node_payload.get("youtube_profile_id") or ""))
    result = post_node_json(node, "/api/stream/recommend", node_payload, timeout=45)
    if (
        result.get("ok")
        and node_payload.get("stream_output_mode") == "youtube_api"
        and node_payload.get("youtube_stream_id")
        and youtube_client.local_status().get("authorized")
    ):
        with suppress(Exception):
            health = youtube_client.stream_health(node_payload["youtube_stream_id"], result.get("recommendation") or node_payload)
            if health.get("ok"):
                result["youtube_health"] = health.get("health") or {}
                result["youtube_feedback"] = health.get("analysis") or {}
                result["recommendation"] = health.get("recommendation") or result.get("recommendation")
                analysis = dict(result.get("analysis") or {})
                analysis["youtube_health_status"] = (health.get("health") or {}).get("health_status", "")
                analysis["youtube_stream_status"] = (health.get("health") or {}).get("stream_status", "")
                analysis["youtube_reasons"] = (health.get("analysis") or {}).get("reasons") or []
                analysis["youtube_warnings"] = (health.get("analysis") or {}).get("warnings") or []
                result["analysis"] = analysis
    status_code = 200 if result.get("ok") else 502
    return jsonify({"node_id": node_id, **redacted_stream_result(result)}), status_code


@APP.post("/api/nodes/stream/start")
def api_nodes_stream_start():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "")
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not node.get("enabled", True):
        return jsonify({"ok": False, "message": "node disabled"}), 409
    node_payload = stream_payload_for_node(payload)
    library_media_name = str(payload.get("library_media_name") or "").strip()
    ensured: dict[str, Any] | None = None
    if library_media_name:
        ensured = ensure_library_media_on_node(node, library_media_name)
        if not ensured.get("ok"):
            return jsonify({"ok": False, "message": ensured.get("message") or "开播媒体自动复制失败"}), 502
        node_payload["video_path"] = str(ensured.get("video_path") or library_media_name)
    if not node_payload["video_path"]:
        return jsonify({"ok": False, "message": "missing node video_path"}), 400
    if node_payload["stream_output_mode"] == "direct" and not node_payload["stream_key"]:
        return jsonify({"ok": False, "message": "missing stream key"}), 400
    if node_payload["stream_output_mode"] == "youtube_api" and not node_payload["youtube_stream_id"]:
        return jsonify({"ok": False, "message": "missing YouTube API stream ID"}), 400
    if node_payload["stream_output_mode"] == "youtube_api" and not node_payload.get("youtube_ingestion_url"):
        try:
            node_payload["youtube_ingestion_url"] = youtube_client_for_id(str(node_payload.get("youtube_profile_id") or "")).ingestion_target(node_payload["youtube_stream_id"])
        except Exception as exc:
            return youtube_error_response(exc)
    result = post_node_json(node, "/api/start-stream", node_payload, timeout=60)
    status_code = 200 if result.get("ok") else 502
    return jsonify({
        "node_id": node_id,
        "media_ensure": ensured,
        **redacted_stream_result(result),
    }), status_code


@APP.post("/api/nodes/stop-stream")
def api_nodes_stop_stream():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "")
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not node.get("enabled", True):
        return jsonify({"ok": False, "message": "node disabled"}), 409
    stop_api = str(node.get("stop_stream_api") or "/api/stop-stream").strip() or "/api/stop-stream"
    if not stop_api.startswith("/"):
        stop_api = f"/{stop_api}"
    result = post_node_json(node, stop_api, {}, timeout=30)
    status_code = 200 if result.get("ok") else int(result.get("status_code") or 502)
    return jsonify({
        "node_id": node_id,
        **redacted_stream_result(result),
    }), status_code


@APP.post("/api/nodes/restart-stream")
def api_nodes_restart_stream():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "")
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    if not node.get("enabled", True):
        return jsonify({"ok": False, "message": "node disabled"}), 409

    status = request_node_json(node, "/api/status", timeout=10)
    if not status.get("ok"):
        return jsonify({
            "ok": False,
            "node_id": node_id,
            "message": status.get("message") or "node health check failed",
            "status": status,
        }), 502

    stream_config = status.get("stream_config") or {}
    if not stream_config.get("restart_ready"):
        return jsonify({
            "ok": False,
            "node_id": node_id,
            "message": "node has no active stream recovery configuration",
        }), 409

    restart_api = str(node.get("restart_stream_api") or "/api/restart-stream").strip() or "/api/restart-stream"
    if not restart_api.startswith("/"):
        restart_api = f"/{restart_api}"
    result = post_node_json(node, restart_api, {}, timeout=30)
    return jsonify({
        "ok": bool(result.get("ok")),
        "node_id": node_id,
        "message": result.get("message") or ("restart request accepted" if result.get("ok") else "restart request failed"),
        "result": result,
    }), 200 if result.get("ok") else int(result.get("status_code") or 502)


def youtube_node_from_payload(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, tuple[Any, int] | None]:
    node_id = str(payload.get("node_id") or "").strip()
    node = node_by_id(node_id)
    if not node:
        return None, (jsonify({"ok": False, "message": "node not found"}), 404)
    if not node.get("enabled", True):
        return None, (jsonify({"ok": False, "message": "node disabled"}), 409)
    return node, None


def youtube_profile_active_streams(profile_id: str) -> list[dict[str, str]]:
    target = safe_youtube_profile_id(profile_id or YOUTUBE_DEFAULT_PROFILE_ID)
    active = []
    for node in load_nodes():
        if not node.get("enabled", True):
            continue
        status = request_node_json(node, "/api/status", timeout=4)
        if not status.get("ok"):
            continue
        stream = status.get("stream") or {}
        stream_config = status.get("stream_config") or {}
        if not (stream.get("running") or status.get("stream_desired")):
            continue
        if stream_config.get("stream_output_mode") != "youtube_api":
            continue
        running_profile = safe_youtube_profile_id(str(stream_config.get("youtube_profile_id") or YOUTUBE_DEFAULT_PROFILE_ID))
        if running_profile != target:
            continue
        active.append({
            "node_id": str(node.get("id") or ""),
            "node_name": str(node.get("name") or node.get("id") or ""),
            "youtube_stream_id": str(stream_config.get("youtube_stream_id") or ""),
        })
    return active


@APP.get("/api/youtube/profiles")
def api_youtube_profiles():
    config = load_youtube_profiles_config()
    return jsonify({
        "ok": True,
        "active_profile_id": config["active_profile_id"],
        "profiles": [public_youtube_profile(profile) for profile in config["profiles"]],
    })


@APP.post("/api/youtube/profiles")
def api_youtube_profile_save():
    payload = request.get_json(silent=True) or {}
    profile_id = safe_youtube_profile_id(str(payload.get("profile_id") or payload.get("id") or payload.get("name") or ""))
    name = str(payload.get("name") or profile_id or "YouTube Profile").strip()[:80]
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "").strip()
    existing = None
    with suppress(Exception):
        existing = youtube_profile_by_id(profile_id)
    if not client_id and existing:
        client_id = str(existing.get("client_id") or "")
    if not client_secret and existing:
        client_secret = str(existing.get("client_secret") or "")
    updates = {
        "name": name or profile_id,
        "client_id": client_id,
        "client_secret": client_secret,
        "auto_tune_enabled": bool(payload.get("auto_tune_enabled")),
        "auto_tune_interval_seconds": max(60, min(3600, int(payload.get("auto_tune_interval_seconds") or 300))),
        "auto_tune_cooldown_seconds": max(60, min(7200, int(payload.get("auto_tune_cooldown_seconds") or 900))),
        "auto_tune_min_bitrate": max(300, min(20000, int(payload.get("auto_tune_min_bitrate") or 800))),
        "auto_tune_max_bitrate": max(800, min(30000, int(payload.get("auto_tune_max_bitrate") or 6000))),
    }
    profile = save_youtube_profile_config(profile_id, updates)
    return jsonify({
        "ok": True,
        "active_profile_id": profile["id"],
        "profile": public_youtube_profile(profile),
        "profiles": [public_youtube_profile(item) for item in load_youtube_profiles_config()["profiles"]],
    })


@APP.post("/api/youtube/profiles/select")
def api_youtube_profile_select():
    payload = request.get_json(silent=True) or {}
    profile_id = safe_youtube_profile_id(str(payload.get("profile_id") or payload.get("id") or ""))
    config = load_youtube_profiles_config()
    if profile_id not in {profile["id"] for profile in config["profiles"]}:
        return jsonify({"ok": False, "message": "YouTube profile not found"}), 404
    config["active_profile_id"] = profile_id
    save_youtube_profiles_config(config)
    return jsonify({"ok": True, "active_profile_id": profile_id})


@APP.post("/api/youtube/profiles/delete")
def api_youtube_profile_delete():
    payload = request.get_json(silent=True) or {}
    profile_id = safe_youtube_profile_id(str(payload.get("profile_id") or payload.get("id") or ""))
    config = load_youtube_profiles_config()
    profiles = [profile for profile in config["profiles"] if profile["id"] != profile_id]
    if len(profiles) == len(config["profiles"]):
        return jsonify({"ok": False, "message": "YouTube profile not found"}), 404
    if not profiles:
        return jsonify({"ok": False, "message": "At least one YouTube profile is required"}), 409
    removed = next((profile for profile in config["profiles"] if profile["id"] == profile_id), {})
    config["profiles"] = profiles
    if config.get("active_profile_id") == profile_id:
        config["active_profile_id"] = profiles[0]["id"]
    save_youtube_profiles_config(config)
    credential_file = Path(str(removed.get("credential_file") or ""))
    if credential_file.parent == YOUTUBE_PROFILE_CREDENTIALS_DIR:
        credential_file.unlink(missing_ok=True)
    YOUTUBE_CLIENT_CACHE.pop(profile_id, None)
    return jsonify({
        "ok": True,
        "active_profile_id": config["active_profile_id"],
        "profiles": [public_youtube_profile(profile) for profile in profiles],
    })


@APP.post("/api/nodes/youtube/resources")
def api_nodes_youtube_resources():
    payload = request.get_json(silent=True) or {}
    node, error = youtube_node_from_payload(payload)
    if error:
        return error
    assert node is not None
    profile_id, client = youtube_client_from_payload(payload)
    profile = youtube_profile_by_id(profile_id)
    status = client.local_status()
    if status["authorized"]:
        try:
            channel = client.channel()
            streams = client.list_streams()
            broadcasts = client.list_broadcasts()
        except Exception as exc:
            return youtube_error_response(exc)
    else:
        channel = {}
        streams = []
        broadcasts = []
    return jsonify({
        "ok": True,
        "node_id": str(payload.get("node_id") or ""),
        "mode": "hub",
        "profile_id": profile_id,
        "profile": public_youtube_profile(profile),
        "configured": bool(status.get("configured")),
        "authorized": bool(status.get("authorized")),
        "channel": channel,
        "streams": streams,
        "broadcasts": broadcasts,
    })


@APP.post("/api/nodes/youtube/oauth/start")
def api_nodes_youtube_oauth_start():
    payload = request.get_json(silent=True) or {}
    node, error = youtube_node_from_payload(payload)
    if error:
        return error
    assert node is not None
    profile_id, client = youtube_client_from_payload(payload)
    try:
        result = client.start_device_authorization()
    except Exception as exc:
        return youtube_error_response(exc)
    return jsonify({"ok": True, "node_id": str(payload.get("node_id") or ""), "mode": "hub", "profile_id": profile_id, **result})


@APP.post("/api/nodes/youtube/config")
def api_nodes_youtube_config():
    payload = request.get_json(silent=True) or {}
    node, error = youtube_node_from_payload(payload)
    if error:
        return error
    assert node is not None
    profile_id = safe_youtube_profile_id(str(payload.get("profile_id") or payload.get("youtube_profile_id") or active_youtube_profile_id()))
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "").strip()
    if not client_id:
        return jsonify({"ok": False, "message": "YOUTUBE_CLIENT_ID is required"}), 400
    existing = None
    with suppress(Exception):
        existing = youtube_profile_by_id(profile_id)
    if not client_secret and existing:
        client_secret = str(existing.get("client_secret") or "")
    profile = save_youtube_profile_config(
        profile_id,
        {
            "name": str(payload.get("profile_name") or payload.get("name") or (existing or {}).get("name") or profile_id),
            "client_id": client_id,
            "client_secret": client_secret,
            "auto_tune_enabled": bool(payload.get("auto_tune_enabled", (existing or {}).get("auto_tune_enabled", False))),
            "auto_tune_interval_seconds": max(60, min(3600, int(payload.get("auto_tune_interval_seconds") or (existing or {}).get("auto_tune_interval_seconds") or 300))),
            "auto_tune_cooldown_seconds": max(60, min(7200, int(payload.get("auto_tune_cooldown_seconds") or (existing or {}).get("auto_tune_cooldown_seconds") or 900))),
            "auto_tune_min_bitrate": max(300, min(20000, int(payload.get("auto_tune_min_bitrate") or (existing or {}).get("auto_tune_min_bitrate") or 800))),
            "auto_tune_max_bitrate": max(800, min(30000, int(payload.get("auto_tune_max_bitrate") or (existing or {}).get("auto_tune_max_bitrate") or 6000))),
        },
    )
    client = youtube_client_for_id(profile_id)
    return jsonify({
        "ok": True,
        "node_id": str(payload.get("node_id") or ""),
        "mode": "hub",
        "profile_id": profile_id,
        "profile": public_youtube_profile(profile),
        "message": "YouTube API configuration saved on this Hub profile",
        **client.local_status(),
    })


@APP.post("/api/nodes/youtube/oauth/poll")
def api_nodes_youtube_oauth_poll():
    payload = request.get_json(silent=True) or {}
    node, error = youtube_node_from_payload(payload)
    if error:
        return error
    assert node is not None
    profile_id, client = youtube_client_from_payload(payload)
    try:
        result = client.poll_device_authorization(str(payload.get("session_id") or ""))
    except Exception as exc:
        return youtube_error_response(exc)
    return jsonify({"ok": True, "node_id": str(payload.get("node_id") or ""), "mode": "hub", "profile_id": profile_id, **result})


@APP.post("/api/nodes/youtube/health")
def api_nodes_youtube_health():
    payload = request.get_json(silent=True) or {}
    node, error = youtube_node_from_payload(payload)
    if error:
        return error
    assert node is not None
    stream_id = str(payload.get("youtube_stream_id") or payload.get("stream_id") or "").strip()
    profile_id, client = youtube_client_from_payload(payload)
    try:
        result = client.stream_health(stream_id, stream_payload_for_node(payload))
    except Exception as exc:
        return youtube_error_response(exc)
    return jsonify({
        "node_id": str(payload.get("node_id") or ""),
        "mode": "hub",
        "profile_id": profile_id,
        "profile": public_youtube_profile(youtube_profile_by_id(profile_id)),
        **result,
    })


@APP.post("/api/nodes/youtube/prepare")
def api_nodes_youtube_prepare():
    payload = request.get_json(silent=True) or {}
    node, error = youtube_node_from_payload(payload)
    if error:
        return error
    assert node is not None
    allowed = {
        "title",
        "description",
        "privacy_status",
        "scheduled_start_time",
        "stream_id",
        "stream_title",
        "resolution",
        "frame_rate",
        "made_for_kids",
        "enable_auto_start",
        "enable_auto_stop",
        "enable_dvr",
    }
    node_payload = {key: payload.get(key) for key in allowed if key in payload}
    profile_id, client = youtube_client_from_payload(payload)
    try:
        result = client.prepare_broadcast(node_payload)
    except Exception as exc:
        return youtube_error_response(exc)
    return jsonify({
        "ok": True,
        "node_id": str(payload.get("node_id") or ""),
        "mode": "hub",
        "profile_id": profile_id,
        "profile": public_youtube_profile(youtube_profile_by_id(profile_id)),
        "message": "YouTube broadcast prepared and bound by Hub",
        "result": result,
    })


@APP.post("/api/nodes/youtube/oauth/revoke")
def api_nodes_youtube_oauth_revoke():
    payload = request.get_json(silent=True) or {}
    node, error = youtube_node_from_payload(payload)
    if error:
        return error
    assert node is not None
    profile_id, client = youtube_client_from_payload(payload)
    active_streams = youtube_profile_active_streams(profile_id)
    if active_streams:
        labels = ", ".join(item["node_name"] or item["node_id"] for item in active_streams[:5])
        return jsonify({
            "ok": False,
            "profile_id": profile_id,
            "active_streams": active_streams,
            "message": f"Stop active YouTube API streams before revoking this profile: {labels}",
        }), 409
    try:
        client.revoke()
    except Exception as exc:
        return youtube_error_response(exc)
    return jsonify({
        "ok": True,
        "node_id": str(payload.get("node_id") or ""),
        "mode": "hub",
        "profile_id": profile_id,
        "message": "YouTube authorization revoked from Hub",
    })


@APP.post("/api/nodes/reboot")
def api_nodes_reboot():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "")
    confirm_text = str(payload.get("confirm_text") or "")
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "message": "node not found"}), 404
    expected = f"REBOOT {node_id}"
    if confirm_text != expected:
        return jsonify({"ok": False, "message": f"confirmation required: {expected}"}), 400
    if not bool(node.get("allow_vps_reboot") or node.get("reboot_enabled")):
        return jsonify({
            "ok": False,
            "node_id": node_id,
            "message": "VPS reboot is disabled for this node; set allow_vps_reboot only after secure transport is configured",
        }), 403

    reboot_api = str(node.get("reboot_api") or "").strip()
    if reboot_api:
        result = post_node_json(node, reboot_api, {"confirm_text": confirm_text}, timeout=15)
        return jsonify({
            "ok": bool(result.get("ok")),
            "node_id": node_id,
            "message": result.get("message") or ("reboot request accepted" if result.get("ok") else "reboot request failed"),
            "result": result,
        }), 200 if result.get("ok") else 502

    return jsonify({
        "ok": False,
        "node_id": node_id,
        "message": "secure reboot transport is not configured; blocked by protection policy",
    }), 501


def run_git(args: list[str], cwd: Path | None = None, timeout: int = 60) -> dict[str, Any]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


@APP.post("/api/github/check")
def api_github_check():
    ensure_dirs()
    if not (ROOT / ".git").exists():
        return jsonify({
            "ok": False,
            "step": "checkout",
            "message": "Hub checkout has no git metadata; reinstall from the official repository before checking updates",
            "repo": SOURCE_REPO,
            "branch": SOURCE_BRANCH,
        }), 409

    fetch = run_git(["fetch", "--quiet", "--no-tags", SOURCE_REPO, SOURCE_BRANCH], cwd=ROOT, timeout=120)
    if not fetch["ok"]:
        safe_fetch = dict(fetch)
        safe_fetch["stderr"] = redact_secret(str(fetch.get("stderr") or ""), SOURCE_REPO)
        return jsonify({
            "ok": False,
            "step": "fetch",
            "repo": SOURCE_REPO,
            "branch": SOURCE_BRANCH,
            "fetch": safe_fetch,
        }), 502

    local = run_git(["rev-parse", "HEAD"], cwd=ROOT)
    remote = run_git(["rev-parse", "FETCH_HEAD"], cwd=ROOT)
    behind = run_git(["rev-list", "--count", "HEAD..FETCH_HEAD"], cwd=ROOT)
    ahead = run_git(["rev-list", "--count", "FETCH_HEAD..HEAD"], cwd=ROOT)
    diff = run_git(["diff", "--stat", "HEAD", "FETCH_HEAD"], cwd=ROOT)
    local_label = run_git(["log", "-1", "--format=%h %s", "HEAD"], cwd=ROOT)
    remote_label = run_git(["log", "-1", "--format=%h %s", "FETCH_HEAD"], cwd=ROOT)
    checks = (local, remote, behind, ahead, diff, local_label, remote_label)
    ok = all(item["ok"] for item in checks)
    behind_count = int(behind.get("stdout") or 0) if behind["ok"] else None
    ahead_count = int(ahead.get("stdout") or 0) if ahead["ok"] else None
    return jsonify({
        "ok": ok,
        "repo": SOURCE_REPO,
        "branch": SOURCE_BRANCH,
        "local": local.get("stdout"),
        "remote": remote.get("stdout"),
        "local_label": local_label.get("stdout"),
        "remote_label": remote_label.get("stdout"),
        "behind_count": behind_count,
        "ahead_count": ahead_count,
        "has_updates": bool(behind_count) if behind_count is not None else None,
        "diff_stat": diff.get("stdout"),
    }), 200 if ok else 500


@APP.post("/api/nodes/upgrade")
def api_nodes_upgrade():
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "node_id": node_id, "message": "node not found"}), 404
    if not node.get("enabled", True):
        return jsonify({"ok": False, "node_id": node_id, "message": "node disabled"}), 409
    upgrade_api = str(node.get("upgrade_api") or "/api/upgrade").strip() or "/api/upgrade"
    if not upgrade_api.startswith("/"):
        upgrade_api = f"/{upgrade_api}"
    result = post_node_json(node, upgrade_api, {}, timeout=30)
    status_code = 202 if result.get("ok") else int(result.get("status_code") or 502)
    return jsonify({"node_id": node_id, **result}), status_code


@APP.post("/api/nodes/roles/<role>/activate")
def api_activate_node_role(role: str):
    if role not in {"agent", "hub"}:
        return jsonify({"ok": False, "message": "unsupported role"}), 404
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "node_id": node_id, "message": "node not found"}), 404
    if role == "hub":
        result = post_node_json(node, "/api/roles/hub/activate", {"nodes": load_nodes(), "source_hub": request.host_url.rstrip("/")}, timeout=30)
    else:
        hub_url = node_role_urls(node)["hub"]
        if not hub_url:
            return jsonify({"ok": False, "node_id": node_id, "message": "Hub role is unavailable; SSH bootstrap is required"}), 409
        result = post_url_json(
            f"{hub_url}/api/roles/agent/activate",
            {"control_hub_url": request.host_url.rstrip("/")},
            timeout=30,
        )
    status_code = 202 if result.get("ok") else int(result.get("status_code") or 502)
    return jsonify({"node_id": node_id, "role": role, **result}), status_code


@APP.post("/api/nodes/roles/<role>/upgrade")
def api_upgrade_node_role(role: str):
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "node_id": node_id, "message": "node not found"}), 404
    if role == "agent":
        result = post_node_json(node, "/api/upgrade", {}, timeout=30)
    elif role == "hub":
        hub_url = node_role_urls(node)["hub"]
        result = post_url_json(f"{hub_url}/api/upgrade", {}, timeout=30)
    else:
        return jsonify({"ok": False, "message": "unsupported role"}), 404
    status_code = 202 if result.get("ok") else int(result.get("status_code") or 502)
    return jsonify({"node_id": node_id, "role": role, **result}), status_code


@APP.post("/api/nodes/roles/<role>/deactivate")
def api_deactivate_node_role(role: str):
    if role not in {"agent", "hub"}:
        return jsonify({"ok": False, "message": "unsupported role"}), 404
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id") or "").strip()
    node = node_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "node_id": node_id, "message": "node not found"}), 404
    if role == "agent":
        result = post_node_json(node, "/api/roles/agent/deactivate", {}, timeout=30)
    else:
        result = post_node_json(node, "/api/roles/hub/deactivate", {}, timeout=30)
        if not result.get("ok"):
            hub_url = node_role_urls(node)["hub"]
            result = post_url_json(f"{hub_url}/api/roles/hub/deactivate", {}, timeout=30)
    status_code = 202 if result.get("ok") else int(result.get("status_code") or 502)
    return jsonify({"node_id": node_id, "role": role, **result}), status_code


def main() -> None:
    ensure_dirs()
    threading.Thread(target=youtube_autotune_loop, name="youtube-autotune", daemon=True).start()
    host = os.environ.get("STREAM_HUB_HOST", "127.0.0.1")
    try:
        from waitress import serve

        serve(APP, host=host, port=PORT)
    except ImportError:
        APP.run(host=host, port=PORT, threaded=True)
