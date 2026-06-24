from __future__ import annotations

import json
import hmac
import os
import platform
import secrets
import shutil
import signal
import subprocess
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request
from werkzeug.utils import secure_filename


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


load_env_file(ROOT / ".agent.env")
DATA_DIR = Path(os.environ.get("STREAM_AGENT_DATA_DIR", str(ROOT / "agent_data")))
MEDIA_DIR = DATA_DIR / "media"
STATE_FILE = DATA_DIR / "state.json"
PORT = int(os.environ.get("STREAM_AGENT_PORT", "8787"))
CONTROL_HUB = os.environ.get("STREAM_AGENT_CONTROL_HUB", "")
AGENT_NAME = os.environ.get("STREAM_AGENT_NAME", platform.node() or "stream-agent")
MAX_CHUNK_BYTES = int(os.environ.get("STREAM_AGENT_MAX_CHUNK_BYTES", str(64 * 1024 ** 2)))
CONTROL_TOKEN = os.environ.get("STREAM_AGENT_CONTROL_TOKEN", "").strip()
TRUSTED_REMOTE_WRITES = os.environ.get("STREAM_AGENT_TRUSTED_REMOTE_WRITES", "").strip().lower() in {"1", "true", "yes"}
FFMPEG_BIN = os.environ.get("STREAM_AGENT_FFMPEG_BIN", "ffmpeg")
APP_STARTED_AT = time.time()
SHARE_CHUNK_BYTES = int(os.environ.get("STREAM_AGENT_SHARE_CHUNK_BYTES", str(32 * 1024 ** 2)))
SHARE_TIMEOUT_SECONDS = int(os.environ.get("STREAM_AGENT_SHARE_TIMEOUT_SECONDS", "300"))
SHARE_RETRIES = int(os.environ.get("STREAM_AGENT_SHARE_RETRIES", "2"))
UPLOAD_TICKET_TTL_SECONDS = int(os.environ.get("STREAM_AGENT_UPLOAD_TICKET_TTL_SECONDS", "3600"))
UPLOAD_TICKETS: dict[str, dict[str, Any]] = {}
UPLOAD_TICKETS_LOCK = threading.Lock()
UPLOAD_TICKET_PATHS = {"/api/upload-probe", "/api/upload-chunk", "/api/upload-chunk/cancel"}

APP = Flask(__name__)
APP.config["MAX_CONTENT_LENGTH"] = MAX_CHUNK_BYTES + 1024 * 1024


def ensure_dirs() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def request_control_token() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return request.headers.get("X-Control-Token", "").strip()


def has_valid_control_token() -> bool:
    return bool(CONTROL_TOKEN and hmac.compare_digest(request_control_token(), CONTROL_TOKEN))


def request_upload_ticket() -> str:
    return request.headers.get("X-Upload-Ticket", "").strip() or request.args.get("ticket", "").strip()


def cleanup_expired_upload_tickets() -> None:
    now = time.time()
    with UPLOAD_TICKETS_LOCK:
        expired = [ticket for ticket, record in UPLOAD_TICKETS.items() if float(record.get("expires_at") or 0) < now]
        for ticket in expired:
            UPLOAD_TICKETS.pop(ticket, None)


def upload_ticket_record() -> dict[str, Any] | None:
    ticket = request_upload_ticket()
    if not ticket:
        return None
    cleanup_expired_upload_tickets()
    with UPLOAD_TICKETS_LOCK:
        record = UPLOAD_TICKETS.get(ticket)
        return dict(record) if record else None


def has_valid_upload_ticket() -> bool:
    return upload_ticket_record() is not None


def expire_upload_ticket(ticket: str) -> None:
    if not ticket:
        return
    with UPLOAD_TICKETS_LOCK:
        UPLOAD_TICKETS.pop(ticket, None)


def validate_upload_ticket(record: dict[str, Any] | None, upload_id: str, filename: str, total_size: int) -> str:
    if not record:
        return "upload ticket required"
    if str(record.get("upload_id") or "") != upload_id:
        return "upload ticket does not match upload id"
    expected_name = str(record.get("filename") or "")
    if expected_name and expected_name != filename:
        return "upload ticket does not match filename"
    expected_size = int(record.get("total_size") or 0)
    if expected_size and expected_size != total_size:
        return "upload ticket does not match file size"
    return ""


def request_is_local() -> bool:
    return (request.remote_addr or "") in {"127.0.0.1", "::1"}


@APP.before_request
def protect_agent_api():
    if not request.path.startswith("/api"):
        return None
    if request.method == "OPTIONS":
        return None
    if CONTROL_TOKEN:
        if has_valid_control_token():
            return None
        if request.method == "POST" and request.path in UPLOAD_TICKET_PATHS and has_valid_upload_ticket():
            return None
        return jsonify({"ok": False, "message": "agent control token or upload ticket required"}), 403
    if request.method == "POST" and request.path in UPLOAD_TICKET_PATHS and has_valid_upload_ticket():
        return None
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not (request_is_local() or TRUSTED_REMOTE_WRITES):
        return jsonify({"ok": False, "message": "set STREAM_AGENT_CONTROL_TOKEN for remote writes"}), 403
    return None


@APP.after_request
def add_agent_cors_headers(response):
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type,X-Control-Token,Authorization,X-Upload-Route,X-Upload-Ticket")
    return response


def load_state() -> dict[str, Any]:
    ensure_dirs()
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    ensure_dirs()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def read_proc_stat() -> tuple[int, int] | None:
    try:
        parts = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
        values = [int(item) for item in parts]
    except Exception:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total, idle


def cpu_percent_sample() -> float:
    first = read_proc_stat()
    if not first:
        return 0.0
    time.sleep(0.08)
    second = read_proc_stat()
    if not second:
        return 0.0
    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 2)


def memory_status() -> dict[str, Any]:
    meminfo: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw_value = line.split(":", 1)
            meminfo[key] = int(raw_value.strip().split()[0]) * 1024
    except Exception:
        return {"total": 0, "used": 0, "available": 0, "percent": 0}
    total = meminfo.get("MemTotal", 0)
    available = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
    used = max(0, total - available)
    return {
        "total": total,
        "used": used,
        "available": available,
        "percent": round((used / total) * 100, 2) if total else 0,
    }


def net_counters() -> dict[str, int]:
    bytes_recv = 0
    bytes_sent = 0
    try:
        for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]:
            name, data = line.split(":", 1)
            iface = name.strip()
            if iface == "lo":
                continue
            fields = data.split()
            bytes_recv += int(fields[0])
            bytes_sent += int(fields[8])
    except Exception:
        pass
    return {"bytes_recv": bytes_recv, "bytes_sent": bytes_sent}


def network_status(state: dict[str, Any]) -> dict[str, Any]:
    counters = net_counters()
    now = time.time()
    previous = state.get("last_net_sample") or {}
    elapsed = max(0.001, now - float(previous.get("at") or now))
    recv_delta = max(0, counters["bytes_recv"] - int(previous.get("bytes_recv") or counters["bytes_recv"]))
    sent_delta = max(0, counters["bytes_sent"] - int(previous.get("bytes_sent") or counters["bytes_sent"]))
    state["last_net_sample"] = {"at": now, **counters}
    return {
        **counters,
        "current_upload_bps": int(sent_delta / elapsed),
        "current_download_bps": int(recv_delta / elapsed),
        "rate_label": "live",
    }


def upload_transfer_status(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "active_upload_count": len(state.get("active_uploads") or {}),
        "bytes_received_total": int(state.get("bytes_received_total") or 0),
        "chunks_received_total": int(state.get("chunks_received_total") or 0),
        "completed_uploads_total": int(state.get("completed_uploads_total") or 0),
        "last_event": state.get("last_event") or "--",
        "last_route": state.get("last_route") or "direct-agent",
        "last_error": state.get("last_error") or "",
        "last_event_at_label": state.get("last_event_at_label") or "--",
        "last_probe": state.get("last_probe") or {},
    }


def ffmpeg_processes() -> list[dict[str, Any]]:
    result = run_command(["ps", "-eo", "pid=,pcpu=,comm="], timeout=5)
    if not result.get("ok"):
        return []
    processes = []
    for line in result.get("stdout", "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or "ffmpeg" not in parts[2].lower():
            continue
        try:
            cpu = float(parts[1])
        except ValueError:
            cpu = 0.0
        processes.append({"pid": int(parts[0]), "cpu_percent": cpu})
    return processes


def run_command(args: list[str], timeout: int = 15) -> dict[str, Any]:
    if not shutil.which(args[0]):
        return {"ok": False, "message": f"{args[0]} is not installed"}
    try:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def process_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_process(pid: int | None, timeout: int = 8) -> dict[str, Any]:
    if not process_running(pid):
        return {"ok": True, "skipped": True}
    assert pid is not None
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not process_running(pid):
                return {"ok": True, "pid": pid}
            time.sleep(0.2)
        os.kill(pid, signal.SIGKILL)
        return {"ok": True, "pid": pid, "forced": True}
    except Exception as exc:
        return {"ok": False, "pid": pid, "message": str(exc)}


def tailscale_status() -> dict[str, Any]:
    result = run_command(["tailscale", "status", "--json"], timeout=10)
    if not result.get("ok"):
        return result
    try:
        data = json.loads(result.get("stdout") or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "message": "tailscale returned invalid json", "raw": result.get("stdout", "")[:500]}
    self_info = data.get("Self") or {}
    return {
        "ok": True,
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


def media_allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in {".mp4", ".mov", ".mkv", ".m4v", ".webm"}


def list_media() -> list[dict[str, Any]]:
    ensure_dirs()
    items: list[dict[str, Any]] = []
    for path in sorted(MEDIA_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_file() or not media_allowed(path.name):
            continue
        stat = path.stat()
        items.append({
            "name": path.name,
            "video_path": str(path),
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "modified_label": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        })
    return items


def file_size_label(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.1f} {units[index]}" if index else f"{int(value)} B"


def media_by_name_or_path(value: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("missing media name")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = MEDIA_DIR / secure_filename(raw)
    resolved = candidate.resolve()
    media_root = MEDIA_DIR.resolve()
    try:
        resolved.relative_to(media_root)
    except ValueError as exc:
        raise ValueError("media must be inside agent media directory") from exc
    if not resolved.is_file() or not media_allowed(resolved.name):
        raise ValueError("media file not found or unsupported")
    return resolved


def resolve_media_path(value: str) -> Path:
    return media_by_name_or_path(value)


def safe_media_filename(value: str) -> str:
    name = secure_filename(str(value or "").strip())
    if not name:
        raise ValueError("invalid filename")
    if not media_allowed(name):
        raise ValueError("unsupported media extension")
    return name


def stream_output_url(payload: dict[str, Any]) -> str:
    stream_url = str(payload.get("stream_url") or "rtmp://a.rtmp.youtube.com/live2").strip().rstrip("/")
    stream_key = str(payload.get("stream_key") or "").strip()
    if str(payload.get("stream_output_mode") or "direct").strip().lower() == "local_relay":
        return stream_url
    if not stream_key:
        raise ValueError("missing stream key")
    return f"{stream_url}/{stream_key}"


def ffmpeg_command(payload: dict[str, Any], video_path: Path, output_url: str) -> list[str]:
    if not shutil.which(FFMPEG_BIN):
        raise RuntimeError(f"{FFMPEG_BIN} is not installed")
    if bool(payload.get("copy_mode")):
        return [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            str(video_path),
            "-c",
            "copy",
            "-f",
            "flv",
            output_url,
        ]
    fps = max(15, min(60, int(payload.get("fps") or 30)))
    keyframe_seconds = max(1, min(4, int(payload.get("keyframe_seconds") or 2)))
    video_bitrate = max(800, min(20000, int(payload.get("video_bitrate") or 4500)))
    audio_bitrate = max(64, min(320, int(payload.get("audio_bitrate") or 192)))
    resolution = str(payload.get("resolution") or "1280x720").strip() or "1280x720"
    preset = str(payload.get("preset") or "veryfast").strip() or "veryfast"
    return [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-re",
        "-stream_loop",
        "-1",
        "-i",
        str(video_path),
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-b:v",
        f"{video_bitrate}k",
        "-maxrate",
        f"{video_bitrate}k",
        "-bufsize",
        f"{video_bitrate * 2}k",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-s",
        resolution,
        "-g",
        str(fps * keyframe_seconds),
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_bitrate}k",
        "-ar",
        "44100",
        "-f",
        "flv",
        output_url,
    ]


@APP.get("/")
def index():
    return jsonify({
        "ok": True,
        "name": AGENT_NAME,
        "mode": "headless-agent",
        "message": "Stream Control Hub headless agent is running.",
    })


@APP.get("/api/status")
def api_status():
    state = load_state()
    usage = shutil.disk_usage(MEDIA_DIR)
    net = network_status(state)
    save_state(state)
    stream_pid = int(state.get("stream_pid") or 0)
    stream_running = process_running(stream_pid)
    processes = ffmpeg_processes()
    if stream_running and not any(item.get("pid") == stream_pid for item in processes):
        processes.insert(0, {"pid": stream_pid, "cpu_percent": 0.0})
    load_avg = list(os.getloadavg()) if hasattr(os, "getloadavg") else []
    memory = memory_status()
    boot_time = 0.0
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                boot_time = float(line.split()[1])
                break
    except Exception:
        boot_time = 0.0
    quota_limit = int(os.environ.get("STREAM_AGENT_TRAFFIC_QUOTA_BYTES", "0") or 0)
    total_used = net["bytes_recv"] + net["bytes_sent"]
    return jsonify({
        "ok": True,
        "hostname": platform.node(),
        "platform": platform.platform(),
        "cpu_percent": cpu_percent_sample(),
        "cpu_count": os.cpu_count() or 1,
        "load_avg": [round(item, 2) for item in load_avg],
        "memory": memory,
        "uptime": format_duration(time.time() - boot_time) if boot_time else "--",
        "app_uptime": format_duration(time.time() - APP_STARTED_AT),
        "net": net,
        "quota": {
            "limit": quota_limit,
            "total_used": total_used,
            "remaining": max(0, quota_limit - total_used) if quota_limit else 0,
            "total_percent": round((total_used / quota_limit) * 100, 2) if quota_limit else 0,
        },
        "agent": {
            "name": AGENT_NAME,
            "mode": "headless-agent",
            "headless": True,
            "version": "0.1.0",
            "control_hub": CONTROL_HUB,
        },
        "disk": {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": round((usage.used / usage.total) * 100, 2) if usage.total else 0,
        },
        "stream": {
            "running": stream_running,
            "pid": stream_pid if stream_running else None,
            "processes": processes,
            "current_bitrate_kbps": 0,
            "current_bitrate_label": "待开播后校正",
            "adaptive": {"enabled": False, "status": "off"},
            "auto_restart": {"enabled": False},
            "relay": {"enabled": False},
            "tuning": {"fifo_enabled": False},
        },
        "stream_config": {
            "has_stream_key": bool(state.get("has_stream_key")),
            **(state.get("stream_config") or {}),
        },
        "transfer": upload_transfer_status(state),
        "public_upload": {
            "enabled": False,
            "supported": False,
            "public_origin": "",
            "last_reason": "direct-agent-upload",
        },
        "videos": list_media(),
        "tailscale": tailscale_status(),
    })


@APP.route("/api/upload-probe", methods=["POST", "OPTIONS"])
def api_upload_probe():
    if request.method == "OPTIONS":
        return ("", 204)
    started_at = time.time()
    size = len(request.get_data(cache=False))
    elapsed = max(0.001, time.time() - started_at)
    state = load_state()
    state["last_probe"] = {
        "size": size,
        "elapsed_ms": int(elapsed * 1000),
        "bytes_per_second": int(size / elapsed),
        "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    }
    save_state(state)
    return jsonify({"ok": True, "received": size, "bytes_per_second": int(size / elapsed)})


@APP.post("/api/upload-ticket")
def api_upload_ticket():
    payload = request.get_json(silent=True) or {}
    try:
        upload_id = secure_filename(str(payload.get("upload_id") or ""))
        filename = safe_media_filename(str(payload.get("filename") or ""))
        total_size = int(payload.get("total_size") or 0)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    if not upload_id:
        return jsonify({"ok": False, "message": "upload_id is required"}), 400
    if total_size <= 0:
        return jsonify({"ok": False, "message": "total_size is required"}), 400
    ticket = secrets.token_urlsafe(32)
    expires_at = time.time() + UPLOAD_TICKET_TTL_SECONDS
    cleanup_expired_upload_tickets()
    with UPLOAD_TICKETS_LOCK:
        UPLOAD_TICKETS[ticket] = {
            "upload_id": upload_id,
            "filename": filename,
            "total_size": total_size,
            "created_at": time.time(),
            "expires_at": expires_at,
        }
    return jsonify({
        "ok": True,
        "ticket": ticket,
        "expires_at": expires_at,
        "expires_in": UPLOAD_TICKET_TTL_SECONDS,
        "upload_id": upload_id,
        "filename": filename,
    })


@APP.get("/api/public-upload")
def api_public_upload():
    return jsonify({
        "ok": True,
        "supported": True,
        "restrict_public_to_upload": True,
        "ticket_required": True,
        "ticket_ttl_seconds": UPLOAD_TICKET_TTL_SECONDS,
        "message": "public browser uploads require a short-lived Hub-issued ticket",
    })


@APP.route("/api/upload-chunk", methods=["POST", "OPTIONS"])
def api_upload_chunk():
    if request.method == "OPTIONS":
        return ("", 204)
    ensure_dirs()
    started_at = time.time()
    upload_id = secure_filename(str(request.form.get("upload_id") or "upload"))
    filename = secure_filename(str(request.form.get("filename") or "media.bin"))
    if not upload_id or not filename:
        return jsonify({"ok": False, "message": "invalid upload metadata"}), 400
    if not media_allowed(filename):
        return jsonify({"ok": False, "message": "unsupported media extension"}), 400
    chunk = request.files.get("chunk")
    if not chunk:
        return jsonify({"ok": False, "message": "missing chunk"}), 400
    try:
        chunk_index = int(request.form.get("chunk_index") or 0)
        total_chunks = int(request.form.get("total_chunks") or 1)
        offset = int(request.form.get("offset") or 0)
        total_size = int(request.form.get("total_size") or 0)
    except ValueError:
        return jsonify({"ok": False, "message": "invalid chunk metadata"}), 400
    ticket_value = request_upload_ticket()
    ticket_error = validate_upload_ticket(upload_ticket_record(), upload_id, filename, total_size)
    if ticket_value and ticket_error:
        return jsonify({"ok": False, "message": ticket_error}), 403

    temp_path = MEDIA_DIR / f".{upload_id}.{filename}.part"
    final_path = MEDIA_DIR / filename
    with temp_path.open("r+b" if temp_path.exists() else "wb") as target:
        target.seek(offset)
        shutil.copyfileobj(chunk.stream, target)
    received_size = temp_path.stat().st_size
    complete = chunk_index + 1 >= total_chunks and received_size >= total_size
    if complete:
        counter = 1
        while final_path.exists():
            final_path = MEDIA_DIR / f"{Path(filename).stem}-{counter}{Path(filename).suffix}"
            counter += 1
        temp_path.replace(final_path)
        expire_upload_ticket(ticket_value)
    elapsed = max(0.001, time.time() - started_at)
    chunk_bytes = int(received_size - offset) if received_size >= offset else 0
    state = load_state()
    active_uploads = dict(state.get("active_uploads") or {})
    if complete:
        active_uploads.pop(upload_id, None)
        state["completed_uploads_total"] = int(state.get("completed_uploads_total") or 0) + 1
    else:
        active_uploads[upload_id] = {
            "filename": filename,
            "received_size": received_size,
            "total_size": total_size,
            "updated_at": time.time(),
        }
    state["active_uploads"] = active_uploads
    state["bytes_received_total"] = int(state.get("bytes_received_total") or 0) + max(0, chunk_bytes)
    state["chunks_received_total"] = int(state.get("chunks_received_total") or 0) + 1
    state["last_event"] = "upload-complete" if complete else "upload-chunk"
    state["last_route"] = str(request.headers.get("X-Upload-Route") or "direct-agent")
    state["last_error"] = ""
    state["last_event_at_label"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    save_state(state)
    return jsonify({
        "ok": True,
        "complete": complete,
        "received_size": received_size,
        "chunk_bytes": chunk_bytes,
        "bytes_per_second": int(chunk_bytes / elapsed),
        "rate_label": f"{file_size_label(int(chunk_bytes / elapsed))}/s",
        "video_path": str(final_path if complete else temp_path),
    })


@APP.post("/api/upload-chunk/cancel")
def api_upload_cancel():
    ensure_dirs()
    payload = request.get_json(silent=True) or {}
    upload_id = secure_filename(str(payload.get("upload_id") or ""))
    expire_ticket_after_cancel = bool(payload.get("expire_ticket", True))
    ticket_value = request_upload_ticket()
    record = upload_ticket_record()
    if ticket_value and (not record or str(record.get("upload_id") or "") != upload_id):
        return jsonify({"ok": False, "message": "upload ticket does not match upload id"}), 403
    removed = 0
    if upload_id:
        for path in MEDIA_DIR.glob(f".{upload_id}.*.part"):
            path.unlink(missing_ok=True)
            removed += 1
    if expire_ticket_after_cancel:
        expire_upload_ticket(ticket_value)
    return jsonify({"ok": True, "removed": removed})


@APP.post("/api/media/rename")
def api_media_rename():
    payload = request.get_json(silent=True) or {}
    try:
        source = media_by_name_or_path(str(payload.get("media") or payload.get("video_path") or ""))
        new_name = safe_media_filename(str(payload.get("new_name") or ""))
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    target = MEDIA_DIR / new_name
    if target.exists():
        return jsonify({"ok": False, "message": "target filename already exists"}), 409
    source.rename(target)
    return jsonify({
        "ok": True,
        "message": "media renamed",
        "old_name": source.name,
        "name": target.name,
        "video_path": str(target),
        "videos": list_media(),
    })


@APP.post("/api/media/delete")
def api_media_delete():
    payload = request.get_json(silent=True) or {}
    try:
        source = media_by_name_or_path(str(payload.get("media") or payload.get("video_path") or ""))
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    deleted_name = source.name
    source.unlink()
    return jsonify({
        "ok": True,
        "message": "media deleted",
        "name": deleted_name,
        "videos": list_media(),
    })


def target_headers(payload: dict[str, Any]) -> dict[str, str]:
    headers = {"X-Upload-Route": "agent-share"}
    ticket = str(payload.get("target_upload_ticket") or payload.get("upload_ticket") or "").strip()
    if ticket:
        headers["X-Upload-Ticket"] = ticket
        return headers
    token = str(payload.get("target_token") or "").strip()
    if token:
        headers["X-Control-Token"] = token
    return headers


def post_share_chunk(
    target_base_url: str,
    headers: dict[str, str],
    media_path: Path,
    *,
    upload_id: str,
    chunk_index: int,
    total_chunks: int,
    offset: int,
    total_size: int,
    chunk_size: int,
) -> dict[str, Any]:
    with media_path.open("rb") as source:
        source.seek(offset)
        chunk_bytes = source.read(min(chunk_size, total_size - offset))
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
    response = requests.post(
        f"{target_base_url.rstrip('/')}/api/upload-chunk",
        data=data,
        files=files,
        headers=headers,
        timeout=SHARE_TIMEOUT_SECONDS,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"message": response.text[:500]}
    payload["ok"] = response.ok and bool(payload.get("ok", False))
    payload.setdefault("status_code", response.status_code)
    return payload


def report_share_progress(payload: dict[str, Any], *, done_bytes: int, total_bytes: int, current_bps: int, started_at: float) -> None:
    progress_url = str(payload.get("progress_url") or "").strip()
    if not progress_url:
        return
    elapsed = max(0.001, time.time() - started_at)
    with suppress(Exception):
        requests.post(
            progress_url,
            json={
                "done_bytes": done_bytes,
                "total_bytes": total_bytes,
                "current_bps": current_bps,
                "average_bps": int(done_bytes / elapsed),
                "target_index": int(payload.get("progress_target_index") or 0),
                "target_count": int(payload.get("progress_target_count") or 1),
                "target_node_id": str(payload.get("progress_target_node_id") or ""),
                "message": f"正在共享 {file_size_label(done_bytes)} / {file_size_label(total_bytes)}",
            },
            timeout=5,
        )


@APP.post("/api/share-media")
def api_share_media():
    payload = request.get_json(silent=True) or {}
    target_base_url = str(payload.get("target_base_url") or "").strip().rstrip("/")
    target_base_urls = [
        str(item or "").strip().rstrip("/")
        for item in (payload.get("target_base_urls") or [])
        if str(item or "").strip()
    ]
    if target_base_url:
        target_base_urls.insert(0, target_base_url)
    target_base_urls = list(dict.fromkeys(target_base_urls))
    if not target_base_urls:
        return jsonify({"ok": False, "message": "missing target_base_url"}), 400
    try:
        media_path = media_by_name_or_path(str(payload.get("media") or payload.get("video_path") or ""))
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    total_size = media_path.stat().st_size
    if total_size <= 0:
        return jsonify({"ok": False, "message": "media file is empty"}), 400
    chunk_size = max(1024 * 1024, min(SHARE_CHUNK_BYTES, MAX_CHUNK_BYTES))
    total_chunks = (total_size + chunk_size - 1) // chunk_size
    upload_id = secure_filename(str(payload.get("upload_id") or "")) or f"share_{uuid.uuid4().hex}"
    headers = target_headers(payload)
    started_at = time.time()
    last_payload: dict[str, Any] = {}
    last_error = ""
    try:
        active_target_base_url = ""
        for candidate_index, candidate_url in enumerate(target_base_urls):
            last_error = ""
            if candidate_index:
                with suppress(Exception):
                    requests.post(
                        f"{active_target_base_url}/api/upload-chunk/cancel",
                        json={"upload_id": upload_id, "expire_ticket": False},
                        headers=headers,
                        timeout=30,
                    )
            active_target_base_url = candidate_url
            for chunk_index in range(total_chunks):
                offset = chunk_index * chunk_size
                chunk_started_at = time.time()
                for attempt in range(SHARE_RETRIES + 1):
                    last_payload = post_share_chunk(
                        candidate_url,
                        headers,
                        media_path,
                        upload_id=upload_id,
                        chunk_index=chunk_index,
                        total_chunks=total_chunks,
                        offset=offset,
                        total_size=total_size,
                        chunk_size=chunk_size,
                    )
                    if last_payload.get("ok"):
                        break
                    if attempt < SHARE_RETRIES:
                        time.sleep(min(5, 0.8 * (attempt + 1)))
                if not last_payload.get("ok"):
                    last_error = last_payload.get("message") or f"chunk {chunk_index + 1} share failed"
                    break
                done_bytes = min(total_size, offset + min(chunk_size, total_size - offset))
                chunk_elapsed = max(0.001, time.time() - chunk_started_at)
                report_share_progress(
                    payload,
                    done_bytes=done_bytes,
                    total_bytes=total_size,
                    current_bps=int(min(chunk_size, total_size - offset) / chunk_elapsed),
                    started_at=started_at,
                )
            if not last_error:
                break
        if last_error:
            raise RuntimeError(last_error)
        if not last_payload.get("complete"):
            raise RuntimeError("target did not report upload completion")
        elapsed = max(0.001, time.time() - started_at)
        return jsonify({
            "ok": True,
            "message": "media shared to target agent",
            "media": media_path.name,
            "size": total_size,
            "size_label": file_size_label(total_size),
            "elapsed_seconds": round(elapsed, 2),
            "average_bytes_per_second": int(total_size / elapsed),
            "average_rate_label": f"{file_size_label(int(total_size / elapsed))}/s",
            "target_base_url": active_target_base_url,
            "video_path": last_payload.get("video_path"),
        })
    except Exception as exc:
        with suppress(Exception):
            requests.post(
                f"{target_base_urls[0]}/api/upload-chunk/cancel",
                json={"upload_id": upload_id},
                headers=headers,
                timeout=30,
            )
        state = load_state()
        state["last_error"] = str(exc)
        state["last_event"] = "share-failed"
        state["last_event_at_label"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        save_state(state)
        return jsonify({
            "ok": False,
            "message": str(exc),
            "media": media_path.name,
            "last_response": last_payload,
            "target_base_url": target_base_urls[0],
        }), 502


@APP.post("/api/stream/recommend")
def api_stream_recommend():
    return jsonify({
        "ok": True,
        "recommendation": {
            "copy_mode": False,
            "preset": "veryfast",
            "video_bitrate": 4500,
            "audio_bitrate": 192,
            "fps": 30,
            "resolution": "1280x720",
            "keyframe_seconds": 2,
        },
        "analysis": {"score": 75, "warnings": ["basic headless recommendation"]},
    })


@APP.post("/api/start-stream")
def api_start_stream():
    payload = request.get_json(silent=True) or {}
    state = load_state()
    try:
        video_path = resolve_media_path(str(payload.get("video_path") or ""))
        output_url = stream_output_url(payload)
        command = ffmpeg_command(payload, video_path, output_url)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    previous_pid = int(state.get("stream_pid") or 0)
    stop_result = stop_process(previous_pid)
    if not stop_result.get("ok"):
        return jsonify({"ok": False, "message": "failed to stop previous stream", "stop": stop_result}), 500

    log_path = DATA_DIR / "ffmpeg.log"
    log_file = log_path.open("ab")
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(DATA_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
        )
    finally:
        log_file.close()

    state["has_stream_key"] = bool(payload.get("stream_key")) or bool(state.get("has_stream_key"))
    state["stream_pid"] = proc.pid
    state["stream_config"] = {
        key: value
        for key, value in payload.items()
        if key not in {"stream_key"}
    }
    state["stream_config"]["video_path"] = str(video_path)
    state["stream_config"]["stream_url"] = str(payload.get("stream_url") or "rtmp://a.rtmp.youtube.com/live2").strip().rstrip("/")
    state["last_started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_state(state)
    return jsonify({
        "ok": True,
        "message": "stream started",
        "result": {
            "started_pid": proc.pid,
            "duplicate_processes": 1 if previous_pid and stop_result.get("ok") and not stop_result.get("skipped") else 0,
            "log_path": str(log_path),
        },
    })


@APP.post("/api/restart-stream")
def api_restart_stream():
    return jsonify({
        "ok": False,
        "message": "cached restart is disabled because the headless agent does not persist stream keys",
    }), 501


def main() -> None:
    ensure_dirs()
    try:
        from waitress import serve

        serve(APP, host=os.environ.get("STREAM_AGENT_HOST", "0.0.0.0"), port=PORT)
    except ImportError:
        APP.run(host=os.environ.get("STREAM_AGENT_HOST", "0.0.0.0"), port=PORT, threaded=True)


if __name__ == "__main__":
    main()
