from __future__ import annotations

import json
import hmac
import ipaddress
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request
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


load_env_file(ROOT / ".agent.env")
DATA_DIR = Path(os.environ.get("STREAM_AGENT_DATA_DIR", str(ROOT / "agent_data")))
MEDIA_DIR = DATA_DIR / "media"
STATE_FILE = DATA_DIR / "state.json"
STREAM_RESTART_FILE = Path(os.environ.get("STREAM_AGENT_RESTART_FILE", str(DATA_DIR / "stream_restart.json")))
PORT = int(os.environ.get("STREAM_AGENT_PORT", "8787"))
CONTROL_HUB = os.environ.get("STREAM_AGENT_CONTROL_HUB", "")
AGENT_NAME = os.environ.get("STREAM_AGENT_NAME", platform.node() or "stream-agent")
MAX_CHUNK_BYTES = int(os.environ.get("STREAM_AGENT_MAX_CHUNK_BYTES", str(64 * 1024 ** 2)))
CONTROL_TOKEN = os.environ.get("STREAM_AGENT_CONTROL_TOKEN", "").strip()
PUBLIC_ORIGIN = os.environ.get("STREAM_AGENT_PUBLIC_ORIGIN", "").strip()
TRUSTED_REMOTE_WRITES = os.environ.get("STREAM_AGENT_TRUSTED_REMOTE_WRITES", "").strip().lower() in {"1", "true", "yes"}
FFMPEG_BIN = os.environ.get("STREAM_AGENT_FFMPEG_BIN", "ffmpeg")
APP_STARTED_AT = time.time()
SHARE_CHUNK_BYTES = int(os.environ.get("STREAM_AGENT_SHARE_CHUNK_BYTES", str(32 * 1024 ** 2)))
SHARE_TIMEOUT_SECONDS = int(os.environ.get("STREAM_AGENT_SHARE_TIMEOUT_SECONDS", "300"))
SHARE_RETRIES = int(os.environ.get("STREAM_AGENT_SHARE_RETRIES", "2"))
UPLOAD_TICKET_TTL_SECONDS = int(os.environ.get("STREAM_AGENT_UPLOAD_TICKET_TTL_SECONDS", "3600"))
UPLOAD_STALE_STATE_SECONDS = max(300, int(os.environ.get("STREAM_AGENT_UPLOAD_STALE_STATE_SECONDS", "3600")))
MIN_FREE_AFTER_UPLOAD_BYTES = int(os.environ.get("STREAM_AGENT_MIN_FREE_AFTER_UPLOAD_BYTES", str(2 * 1024 ** 3)))
UPLOAD_TICKETS: dict[str, dict[str, Any]] = {}
UPLOAD_TICKETS_LOCK = threading.Lock()
UPLOAD_LOCKS: dict[str, threading.Lock] = {}
UPLOAD_LOCKS_LOCK = threading.Lock()
UPLOAD_TICKET_PATHS = {"/api/upload-probe", "/api/upload-chunk", "/api/upload-chunk/cancel"}
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
PUBLIC_IP_SERVICES = ("https://api.ipify.org", "https://ifconfig.me/ip")
PUBLIC_ORIGIN_CACHE: dict[str, Any] = {"value": "", "checked_at": 0.0}
PUBLIC_ORIGIN_LOCK = threading.Lock()
STREAM_LIFECYCLE_LOCK = threading.RLock()
STREAM_WATCHDOG_STOP = threading.Event()
STREAM_WATCHDOG_THREAD: threading.Thread | None = None
STREAM_AUTO_RESTART_ENABLED = os.environ.get("STREAM_AUTO_RESTART_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
STREAM_WATCHDOG_INTERVAL_SECONDS = max(2, int(os.environ.get("STREAM_WATCHDOG_INTERVAL_SECONDS", "5")))
STREAM_RESTART_BASE_SECONDS = max(2, int(os.environ.get("STREAM_RESTART_BASE_SECONDS", "5")))
STREAM_RESTART_MAX_SECONDS = max(STREAM_RESTART_BASE_SECONDS, int(os.environ.get("STREAM_RESTART_MAX_SECONDS", "60")))
STREAM_RESTART_STABLE_SECONDS = max(15, int(os.environ.get("STREAM_RESTART_STABLE_SECONDS", "60")))
STREAM_START_VERIFY_SECONDS = max(0.0, float(os.environ.get("STREAM_AGENT_START_VERIFY_SECONDS", "3")))
STREAM_START_VERIFY_INTERVAL_SECONDS = max(
    0.1,
    float(os.environ.get("STREAM_AGENT_START_VERIFY_INTERVAL_SECONDS", "0.5")),
)
YOUTUBE_CREDENTIAL_FILE = Path(
    os.environ.get("YOUTUBE_CREDENTIAL_FILE", str(DATA_DIR / "youtube_credentials.json"))
)
AGENT_ENV_FILE = ROOT / ".agent.env"
YOUTUBE_CLIENT = YouTubeAPIClient(
    client_id=os.environ.get("YOUTUBE_CLIENT_ID", ""),
    client_secret=os.environ.get("YOUTUBE_CLIENT_SECRET", ""),
    credential_path=YOUTUBE_CREDENTIAL_FILE,
)


def agent_version_status() -> dict[str, Any]:
    revision = ""
    branch = ""
    if (ROOT / ".git").exists():
        try:
            revision_result = subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                text=True,
                capture_output=True,
                timeout=5,
            )
            branch_result = subprocess.run(
                ["git", "-C", str(ROOT), "branch", "--show-current"],
                text=True,
                capture_output=True,
                timeout=5,
            )
            if revision_result.returncode == 0:
                revision = revision_result.stdout.strip()
            if branch_result.returncode == 0:
                branch = branch_result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "version": revision or "unmanaged",
        "revision": revision,
        "branch": branch,
        "managed_install": bool(revision),
        "upgrade_supported": bool(revision and shutil.which("systemd-run")),
    }


def schedule_agent_upgrade() -> dict[str, Any]:
    version = agent_version_status()
    if not version["managed_install"]:
        raise RuntimeError("Agent is not a Git-managed installation; reinstall it once with install-agent.sh")
    if not shutil.which("systemd-run"):
        raise RuntimeError("systemd-run is required for safe background upgrades")
    unit = f"stream-control-agent-upgrade-{int(time.time())}"
    root = shlex.quote(str(ROOT))
    script = (
        "sleep 2; "
        f"git -C {root} fetch origin main && "
        f"git -C {root} checkout main && "
        f"git -C {root} pull --ff-only origin main && "
        f"env BRANCH=main CHOICE=1 sh {root}/scripts/install-agent.sh"
    )
    result = subprocess.run(
        ["systemd-run", "--unit", unit, "--collect", "--no-block", "/bin/sh", "-c", script],
        text=True,
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "failed to schedule upgrade").strip())
    return {"unit": unit, "from_version": version["version"], "target_branch": "main"}

APP = Flask(__name__)
APP.config["MAX_CONTENT_LENGTH"] = MAX_CHUNK_BYTES + 1024 * 1024


def ensure_dirs() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def public_origin_from_ip(value: str) -> str:
    try:
        ip = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return ""
    if ip.version != 4 or not ip.is_global:
        return ""
    return f"http://{ip}:{PORT}"


def normalize_public_origin(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    origin = public_origin_from_ip(parsed.hostname)
    if not origin:
        return ""
    try:
        port = parsed.port or PORT
    except ValueError:
        return ""
    return f"{parsed.scheme}://{parsed.hostname}:{port}"


def discover_public_origin(*, force: bool = False) -> str:
    configured = normalize_public_origin(PUBLIC_ORIGIN)
    if configured:
        return configured

    now = time.time()
    with PUBLIC_ORIGIN_LOCK:
        cached = str(PUBLIC_ORIGIN_CACHE.get("value") or "")
        checked_at = float(PUBLIC_ORIGIN_CACHE.get("checked_at") or 0)
        ttl = 3600 if cached else 300
        if not force and now - checked_at < ttl:
            return cached

        discovered = ""
        for service_url in PUBLIC_IP_SERVICES:
            try:
                response = requests.get(
                    service_url,
                    timeout=4,
                    headers={"User-Agent": "stream-control-headless-agent/1.0"},
                )
                response.raise_for_status()
                discovered = public_origin_from_ip(response.text)
            except requests.RequestException:
                discovered = ""
            if discovered:
                break
        PUBLIC_ORIGIN_CACHE.update({"value": discovered, "checked_at": now})
        return discovered


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


def complete_upload_ticket(ticket: str, video_path: Path) -> None:
    if not ticket:
        return
    with UPLOAD_TICKETS_LOCK:
        record = UPLOAD_TICKETS.get(ticket)
        if record is not None:
            record["completed"] = True
            record["completed_at"] = time.time()
            record["video_path"] = str(video_path)


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


def upload_lock(upload_id: str) -> threading.Lock:
    with UPLOAD_LOCKS_LOCK:
        lock = UPLOAD_LOCKS.get(upload_id)
        if lock is None:
            lock = threading.Lock()
            UPLOAD_LOCKS[upload_id] = lock
        return lock


def release_upload_lock(upload_id: str) -> None:
    if not upload_id:
        return
    with UPLOAD_LOCKS_LOCK:
        UPLOAD_LOCKS.pop(upload_id, None)


def request_is_local() -> bool:
    return (request.remote_addr or "") in {"127.0.0.1", "::1"}


def configured_control_hub_ip() -> str:
    raw = CONTROL_HUB.strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    hostname = parsed.hostname or ""
    try:
        ip = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        return ""
    return str(ip) if ip in TAILSCALE_CGNAT else ""


def request_is_control_hub() -> bool:
    control_hub_ip = configured_control_hub_ip()
    if not control_hub_ip:
        return False
    try:
        remote_ip = ipaddress.ip_address((request.remote_addr or "").split("%", 1)[0])
    except ValueError:
        return False
    return str(remote_ip) == control_hub_ip


@APP.before_request
def protect_agent_api():
    if not request.path.startswith("/api"):
        return None
    if request.method == "OPTIONS":
        return None
    if request_is_control_hub():
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
    if request.path in UPLOAD_TICKET_PATHS:
        response.headers.setdefault("Access-Control-Allow-Origin", "*")
        response.headers.setdefault("Access-Control-Allow-Methods", "POST,OPTIONS")
        response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type,X-Upload-Route,X-Upload-Ticket")
    return response


def load_state() -> dict[str, Any]:
    ensure_dirs()
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


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


def reload_youtube_client(*, client_id: str, client_secret: str) -> None:
    global YOUTUBE_CLIENT
    os.environ["YOUTUBE_CLIENT_ID"] = client_id
    os.environ["YOUTUBE_CLIENT_SECRET"] = client_secret
    YOUTUBE_CLIENT = YouTubeAPIClient(
        client_id=client_id,
        client_secret=client_secret,
        credential_path=YOUTUBE_CREDENTIAL_FILE,
    )


def save_state(state: dict[str, Any]) -> None:
    ensure_dirs()
    write_private_json(STATE_FILE, state)


def save_stream_restart_payload(payload: dict[str, Any], video_path: Path) -> None:
    allowed = {
        "stream_url",
        "stream_key",
        "youtube_stream_id",
        "copy_mode",
        "adaptive_mode",
        "stream_output_mode",
        "preset",
        "video_bitrate",
        "audio_bitrate",
        "fps",
        "resolution",
        "keyframe_seconds",
    }
    recovery = {
        key: payload.get(key)
        for key in allowed
        if key in payload and not (key == "stream_key" and not payload.get(key))
    }
    recovery["video_path"] = str(video_path)
    write_private_json(STREAM_RESTART_FILE, recovery)


def load_stream_restart_payload() -> dict[str, Any] | None:
    if not STREAM_RESTART_FILE.exists():
        return None
    try:
        payload = json.loads(STREAM_RESTART_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def remove_stream_restart_payload() -> None:
    STREAM_RESTART_FILE.unlink(missing_ok=True)


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


def prune_stale_upload_state(state: dict[str, Any], *, now: float | None = None) -> int:
    active_uploads = dict(state.get("active_uploads") or {})
    current_time = time.time() if now is None else now
    removed = 0
    for raw_upload_id, raw_item in list(active_uploads.items()):
        item = raw_item if isinstance(raw_item, dict) else {}
        updated_at = float(item.get("updated_at") or 0)
        if current_time - updated_at < UPLOAD_STALE_STATE_SECONDS:
            continue
        upload_id = secure_filename(str(raw_upload_id or ""))
        filename = Path(str(item.get("filename") or "")).name
        part_path = MEDIA_DIR / f".{upload_id}.{filename}.part"
        if part_path.exists():
            continue
        active_uploads.pop(raw_upload_id, None)
        removed += 1
    if removed:
        state["active_uploads"] = active_uploads
        state["stale_uploads_pruned_total"] = int(state.get("stale_uploads_pruned_total") or 0) + removed
    return removed


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
        pid = int(parts[0])
        if stream_process_owned(pid):
            processes.append({"pid": pid, "cpu_percent": cpu})
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
        stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        if len(stat_fields) > 2 and stat_fields[2] == "Z":
            with suppress(ChildProcessError, OSError):
                os.waitpid(pid, os.WNOHANG)
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stream_process_owned(pid: int | None) -> bool:
    if not process_running(pid):
        return False
    assert pid is not None
    try:
        executable = Path(os.readlink(f"/proc/{pid}/exe")).name.lower()
        working_directory = Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
        return "ffmpeg" in executable and working_directory == DATA_DIR.resolve()
    except OSError:
        return False


def stop_process(pid: int | None, timeout: int = 8) -> dict[str, Any]:
    if not process_running(pid):
        return {"ok": True, "skipped": True}
    assert pid is not None
    if not stream_process_owned(pid):
        return {"ok": True, "pid": pid, "skipped": True, "message": "pid is not owned by this Agent"}
    try:
        os.killpg(pid, signal.SIGTERM)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not process_running(pid):
                return {"ok": True, "pid": pid}
            time.sleep(0.2)
        os.killpg(pid, signal.SIGKILL)
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


def safe_media_filename(value: str) -> str:
    raw = Path(str(value or "").strip()).name
    name = secure_filename(raw)
    suffix = Path(raw).suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".m4v", ".webm"} and not media_allowed(name):
        raise ValueError("unsupported media extension")
    if not name or not media_allowed(name):
        name = f"upload-{uuid.uuid4().hex}{suffix}"
    if not media_allowed(name):
        raise ValueError("unsupported media extension")
    return name


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


def stream_output_url(payload: dict[str, Any]) -> str:
    stream_url = str(payload.get("stream_url") or "rtmp://a.rtmp.youtube.com/live2").strip().rstrip("/")
    stream_key = str(payload.get("stream_key") or "").strip()
    output_mode = str(payload.get("stream_output_mode") or "direct").strip().lower()
    if output_mode == "local_relay":
        return stream_url
    if output_mode == "youtube_api":
        return YOUTUBE_CLIENT.ingestion_target(str(payload.get("youtube_stream_id") or ""))
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


def redact_stream_log_line(line: str) -> str:
    return re.sub(r"(rtmps?://[^/\s]+/\S+/)[^\s]+", r"\1[redacted]", line)


def recent_ffmpeg_log_lines(log_path: Path, *, max_lines: int = 20) -> list[str]:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [redact_stream_log_line(line)[-500:] for line in lines[-max_lines:]]


def stream_restart_backoff(consecutive_failures: int) -> int:
    multiplier = 2 ** min(max(0, consecutive_failures - 1), 6)
    return min(STREAM_RESTART_MAX_SECONDS, STREAM_RESTART_BASE_SECONDS * multiplier)


def launch_stream_process(
    payload: dict[str, Any],
    *,
    reason: str,
    persist_recovery: bool,
    resolved_output_url: str = "",
) -> dict[str, Any]:
    video_path = resolve_media_path(str(payload.get("video_path") or ""))
    output_url = resolved_output_url or stream_output_url(payload)
    command = ffmpeg_command(payload, video_path, output_url)
    if persist_recovery:
        save_stream_restart_payload(payload, video_path)

    log_path = DATA_DIR / "ffmpeg.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    log_path.chmod(0o600)
    try:
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(DATA_DIR),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
        except Exception as exc:
            if persist_recovery:
                state = load_state()
                auto_restart = dict(state.get("auto_restart") or {})
                auto_restart.update({
                    "enabled": STREAM_AUTO_RESTART_ENABLED,
                    "desired": True,
                    "status": "waiting",
                    "next_retry_at": time.time() + STREAM_RESTART_BASE_SECONDS,
                    "last_error": str(exc),
                })
                state["stream_desired"] = True
                state["stream_pid"] = 0
                state["auto_restart"] = auto_restart
                save_state(state)
            raise
    finally:
        log_file.close()

    now = time.time()
    state = load_state()
    auto_restart = dict(state.get("auto_restart") or {})
    if reason == "auto-recovery":
        consecutive_failures = int(auto_restart.get("consecutive_failures") or 0) + 1
        auto_restart["restart_count"] = int(auto_restart.get("restart_count") or 0) + 1
        auto_restart["consecutive_failures"] = consecutive_failures
        auto_restart["next_retry_at"] = now + stream_restart_backoff(consecutive_failures)
    else:
        auto_restart["consecutive_failures"] = 0
        auto_restart["next_retry_at"] = 0
        if reason == "manual-start":
            auto_restart["restart_count"] = 0
    auto_restart.update({
        "enabled": STREAM_AUTO_RESTART_ENABLED,
        "desired": True,
        "status": "restarted" if reason == "auto-recovery" else "running",
        "last_reason": reason,
        "last_error": "",
        "last_started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    })
    state["has_stream_key"] = bool(payload.get("stream_key"))
    state["stream_desired"] = True
    state["stream_pid"] = proc.pid
    state["stream_started_at_epoch"] = now
    state["stream_config"] = {
        key: value for key, value in payload.items() if key != "stream_key" and not key.startswith("_")
    }
    state["stream_config"]["video_path"] = str(video_path)
    state["stream_config"]["stream_url"] = str(
        payload.get("stream_url") or "rtmp://a.rtmp.youtube.com/live2"
    ).strip().rstrip("/")
    state["auto_restart"] = auto_restart
    state["last_started_at"] = auto_restart["last_started_at"]
    save_state(state)
    return {"pid": proc.pid, "log_path": str(log_path), "video_path": str(video_path)}


def verify_stream_started(pid: int, log_path: Path) -> dict[str, Any]:
    if STREAM_START_VERIFY_SECONDS <= 0:
        return {"ok": True, "skipped": True}
    deadline = time.time() + STREAM_START_VERIFY_SECONDS
    while time.time() < deadline:
        if not stream_process_owned(pid):
            lines = recent_ffmpeg_log_lines(log_path)
            return {
                "ok": False,
                "message": lines[-1] if lines else "ffmpeg exited immediately",
                "log_tail": lines,
            }
        time.sleep(STREAM_START_VERIFY_INTERVAL_SECONDS)
    return {"ok": True}


def stream_watchdog_tick() -> dict[str, Any]:
    if not STREAM_AUTO_RESTART_ENABLED:
        return {"ok": True, "skipped": True, "reason": "disabled"}
    with STREAM_LIFECYCLE_LOCK:
        state = load_state()
        if not state.get("stream_desired"):
            return {"ok": True, "skipped": True, "reason": "not desired"}

        now = time.time()
        pid = int(state.get("stream_pid") or 0)
        auto_restart = dict(state.get("auto_restart") or {})
        if stream_process_owned(pid):
            started_at = float(state.get("stream_started_at_epoch") or now)
            if now - started_at >= STREAM_RESTART_STABLE_SECONDS and int(auto_restart.get("consecutive_failures") or 0):
                auto_restart.update({
                    "status": "running",
                    "consecutive_failures": 0,
                    "next_retry_at": 0,
                    "last_error": "",
                })
                state["auto_restart"] = auto_restart
                save_state(state)
            return {"ok": True, "running": True, "pid": pid}

        next_retry_at = float(auto_restart.get("next_retry_at") or 0)
        if now < next_retry_at:
            return {"ok": True, "waiting": True, "next_retry_at": next_retry_at}

        payload = load_stream_restart_payload()
        if not payload:
            auto_restart.update({
                "enabled": True,
                "desired": True,
                "status": "blocked",
                "last_error": "stream recovery payload is missing or unreadable",
            })
            state["auto_restart"] = auto_restart
            save_state(state)
            return {"ok": False, "message": auto_restart["last_error"]}

        try:
            result = launch_stream_process(payload, reason="auto-recovery", persist_recovery=False)
            return {"ok": True, "restarted": True, **result}
        except Exception as exc:
            consecutive_failures = int(auto_restart.get("consecutive_failures") or 0) + 1
            auto_restart.update({
                "enabled": True,
                "desired": True,
                "status": "waiting",
                "consecutive_failures": consecutive_failures,
                "next_retry_at": now + stream_restart_backoff(consecutive_failures),
                "last_error": str(exc),
                "last_failure_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            })
            state["stream_pid"] = 0
            state["auto_restart"] = auto_restart
            save_state(state)
            return {"ok": False, "message": str(exc)}


def stream_watchdog_loop() -> None:
    while not STREAM_WATCHDOG_STOP.wait(STREAM_WATCHDOG_INTERVAL_SECONDS):
        with suppress(Exception):
            stream_watchdog_tick()


def start_stream_watchdog() -> None:
    global STREAM_WATCHDOG_THREAD
    if STREAM_WATCHDOG_THREAD and STREAM_WATCHDOG_THREAD.is_alive():
        return
    STREAM_WATCHDOG_STOP.clear()
    STREAM_WATCHDOG_THREAD = threading.Thread(
        target=stream_watchdog_loop,
        name="stream-auto-restart",
        daemon=True,
    )
    STREAM_WATCHDOG_THREAD.start()


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
    ensure_dirs()
    usage = shutil.disk_usage(MEDIA_DIR)
    with STREAM_LIFECYCLE_LOCK:
        state = load_state()
        prune_stale_upload_state(state)
        net = network_status(state)
        stream_pid = int(state.get("stream_pid") or 0)
        stream_running = stream_process_owned(stream_pid)
        processes = ffmpeg_processes()
        if not stream_running and processes:
            stream_pid = int(processes[0].get("pid") or 0)
            stream_running = stream_pid > 0
            state["stream_pid"] = stream_pid
        auto_restart = dict(state.get("auto_restart") or {})
        auto_restart.setdefault("enabled", STREAM_AUTO_RESTART_ENABLED)
        auto_restart.setdefault("desired", bool(state.get("stream_desired")))
        auto_restart.setdefault("status", "running" if stream_running else "idle")
        auto_restart.setdefault("restart_count", 0)
        auto_restart.setdefault("consecutive_failures", 0)
        auto_restart.setdefault("last_error", "")
        save_state(state)
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
    public_origin = discover_public_origin()
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
            "control_hub": CONTROL_HUB,
            **agent_version_status(),
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
            "auto_restart": auto_restart,
            "relay": {"enabled": False},
            "tuning": {"fifo_enabled": False},
        },
        "stream_config": {
            "has_stream_key": bool(state.get("has_stream_key")) and STREAM_RESTART_FILE.exists(),
            "restart_ready": STREAM_RESTART_FILE.exists(),
            **(state.get("stream_config") or {}),
        },
        "youtube": YOUTUBE_CLIENT.local_status(),
        "transfer": upload_transfer_status(state),
        "public_upload": {
            "enabled": bool(public_origin),
            "supported": bool(public_origin),
            "window_supported": False,
            "public_origin": public_origin,
            "restrict_public_to_upload": True,
            "ticket_required": True,
            "last_reason": "auto-public-origin" if public_origin else "public-ip-discovery-unavailable",
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
    with STREAM_LIFECYCLE_LOCK:
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
    public_origin = discover_public_origin()
    return jsonify({
        "ok": True,
        "supported": bool(public_origin),
        "window_supported": False,
        "public_origin": public_origin,
        "restrict_public_to_upload": True,
        "ticket_required": True,
        "ticket_ttl_seconds": UPLOAD_TICKET_TTL_SECONDS,
        "message": (
            "public browser uploads require a short-lived Hub-issued ticket"
            if public_origin
            else "public IPv4 discovery is unavailable; use the Tailscale fallback"
        ),
    })


@APP.route("/api/upload-chunk", methods=["POST", "OPTIONS"])
def api_upload_chunk():
    if request.method == "OPTIONS":
        return ("", 204)
    ensure_dirs()
    started_at = time.time()
    upload_id = secure_filename(str(request.form.get("upload_id") or "upload"))
    try:
        filename = safe_media_filename(str(request.form.get("filename") or "media.bin"))
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    if not upload_id:
        return jsonify({"ok": False, "message": "invalid upload metadata"}), 400
    chunk = request.files.get("chunk")
    if not chunk:
        return jsonify({"ok": False, "message": "missing chunk"}), 400
    try:
        chunk_index = int(request.form.get("chunk_index") or 0)
        total_chunks = int(request.form.get("total_chunks") or 1)
        offset = int(request.form.get("offset") or 0)
        total_size = int(request.form.get("total_size") or 0)
        chunk_size = int(request.form.get("chunk_size") or 0)
    except ValueError:
        return jsonify({"ok": False, "message": "invalid chunk metadata"}), 400
    if total_size <= 0 or total_chunks <= 0 or chunk_index < 0 or chunk_index >= total_chunks:
        return jsonify({"ok": False, "message": "invalid chunk metadata"}), 400
    if chunk_size <= 0 or chunk_size > MAX_CHUNK_BYTES:
        return jsonify({"ok": False, "message": "invalid chunk size"}), 400
    expected_total_chunks = (total_size + chunk_size - 1) // chunk_size
    expected_offset = chunk_index * chunk_size
    if total_chunks != expected_total_chunks or offset != expected_offset or offset >= total_size:
        return jsonify({"ok": False, "message": "chunk offset or count does not match upload metadata"}), 409
    expected_chunk_bytes = min(chunk_size, total_size - offset)
    ticket_value = request_upload_ticket()
    ticket_record = upload_ticket_record()
    ticket_error = validate_upload_ticket(ticket_record, upload_id, filename, total_size)
    if ticket_value and ticket_error:
        return jsonify({"ok": False, "message": ticket_error}), 403
    if ticket_record and ticket_record.get("completed"):
        return jsonify({
            "ok": True,
            "complete": True,
            "received_size": total_size,
            "chunk_bytes": expected_chunk_bytes,
            "bytes_per_second": 0,
            "rate_label": "0 B/s",
            "video_path": str(ticket_record.get("video_path") or ""),
            "idempotent": True,
        })

    temp_path = MEDIA_DIR / f".{upload_id}.{filename}.part"
    final_path = MEDIA_DIR / filename
    with upload_lock(upload_id):
        current_size = temp_path.stat().st_size if temp_path.exists() else 0
        if offset > current_size:
            return jsonify({"ok": False, "message": "previous upload chunk is missing"}), 409
        chunk_bytes_payload = chunk.stream.read()
        if len(chunk_bytes_payload) != expected_chunk_bytes:
            return jsonify({"ok": False, "message": "chunk size does not match upload metadata"}), 400
        if offset + expected_chunk_bytes <= current_size:
            with temp_path.open("rb") as existing:
                existing.seek(offset)
                existing_bytes = existing.read(expected_chunk_bytes)
            if existing_bytes == chunk_bytes_payload:
                elapsed = max(0.001, time.time() - started_at)
                return jsonify({
                    "ok": True,
                    "complete": current_size == total_size,
                    "received_size": current_size,
                    "chunk_bytes": expected_chunk_bytes,
                    "bytes_per_second": int(expected_chunk_bytes / elapsed),
                    "rate_label": f"{file_size_label(int(expected_chunk_bytes / elapsed))}/s",
                    "video_path": str(final_path if current_size == total_size and final_path.exists() else temp_path),
                    "idempotent": True,
                })
            return jsonify({"ok": False, "message": "replayed chunk does not match existing upload data"}), 409
        additional_bytes = max(0, offset + expected_chunk_bytes - current_size)
        free_bytes = shutil.disk_usage(MEDIA_DIR).free
        if free_bytes - additional_bytes < MIN_FREE_AFTER_UPLOAD_BYTES:
            return jsonify({
                "ok": False,
                "message": "not enough free disk space for upload",
                "free_bytes": free_bytes,
                "required_free_after_upload_bytes": MIN_FREE_AFTER_UPLOAD_BYTES,
            }), 507

        with temp_path.open("r+b" if temp_path.exists() else "wb") as target:
            target.seek(offset)
            target.write(chunk_bytes_payload)
            target.truncate(offset + expected_chunk_bytes)

        received_size = temp_path.stat().st_size
        complete = chunk_index + 1 >= total_chunks and received_size == total_size
        if complete:
            counter = 1
            while final_path.exists():
                final_path = MEDIA_DIR / f"{Path(filename).stem}-{counter}{Path(filename).suffix}"
                counter += 1
            temp_path.replace(final_path)
            complete_upload_ticket(ticket_value, final_path)
            release_upload_lock(upload_id)
    elapsed = max(0.001, time.time() - started_at)
    chunk_bytes = expected_chunk_bytes
    with STREAM_LIFECYCLE_LOCK:
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
        with upload_lock(upload_id):
            for path in MEDIA_DIR.glob(f".{upload_id}.*.part"):
                path.unlink(missing_ok=True)
                removed += 1
        release_upload_lock(upload_id)
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
        with STREAM_LIFECYCLE_LOCK:
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


def youtube_error_response(exc: Exception):
    if isinstance(exc, YouTubeAPIError):
        return jsonify({"ok": False, "message": str(exc), "reason": exc.reason}), exc.status_code
    return jsonify({"ok": False, "message": str(exc)}), 502


@APP.get("/api/youtube/status")
def api_youtube_status():
    status = YOUTUBE_CLIENT.local_status()
    if request.args.get("verify") in {"1", "true", "yes"} and status["authorized"]:
        try:
            status["channel"] = YOUTUBE_CLIENT.channel()
        except Exception as exc:
            return youtube_error_response(exc)
    return jsonify({"ok": True, **status})


@APP.post("/api/youtube/config")
def api_youtube_config():
    payload = request.get_json(silent=True) or {}
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "").strip()
    if not client_id:
        return jsonify({"ok": False, "message": "YOUTUBE_CLIENT_ID is required"}), 400
    update_env_file_values(
        AGENT_ENV_FILE,
        {
            "YOUTUBE_CLIENT_ID": client_id,
            "YOUTUBE_CLIENT_SECRET": client_secret,
            "YOUTUBE_CREDENTIAL_FILE": str(YOUTUBE_CREDENTIAL_FILE),
        },
    )
    reload_youtube_client(client_id=client_id, client_secret=client_secret)
    return jsonify({
        "ok": True,
        "message": "YouTube API configuration saved on this Agent",
        **YOUTUBE_CLIENT.local_status(),
    })


@APP.post("/api/youtube/oauth/start")
def api_youtube_oauth_start():
    try:
        result = YOUTUBE_CLIENT.start_device_authorization()
    except Exception as exc:
        return youtube_error_response(exc)
    return jsonify({"ok": True, **result})


@APP.post("/api/youtube/oauth/poll")
def api_youtube_oauth_poll():
    payload = request.get_json(silent=True) or {}
    try:
        result = YOUTUBE_CLIENT.poll_device_authorization(str(payload.get("session_id") or ""))
    except Exception as exc:
        return youtube_error_response(exc)
    return jsonify({"ok": True, **result})


@APP.post("/api/youtube/oauth/revoke")
def api_youtube_oauth_revoke():
    state = load_state()
    stream_config = state.get("stream_config") or {}
    if state.get("stream_desired") and stream_config.get("stream_output_mode") == "youtube_api":
        return jsonify({
            "ok": False,
            "message": "stop the active YouTube API stream before revoking authorization",
        }), 409
    try:
        YOUTUBE_CLIENT.revoke()
    except Exception as exc:
        return youtube_error_response(exc)
    return jsonify({"ok": True, "message": "YouTube authorization revoked"})


@APP.get("/api/youtube/streams")
def api_youtube_streams():
    try:
        streams = YOUTUBE_CLIENT.list_streams()
    except Exception as exc:
        return youtube_error_response(exc)
    return jsonify({"ok": True, "streams": streams})


@APP.get("/api/youtube/broadcasts")
def api_youtube_broadcasts():
    try:
        broadcasts = YOUTUBE_CLIENT.list_broadcasts()
    except Exception as exc:
        return youtube_error_response(exc)
    return jsonify({"ok": True, "broadcasts": broadcasts})


@APP.post("/api/youtube/prepare")
def api_youtube_prepare():
    try:
        result = YOUTUBE_CLIENT.prepare_broadcast(request.get_json(silent=True) or {})
    except Exception as exc:
        return youtube_error_response(exc)
    return jsonify({"ok": True, "message": "YouTube broadcast prepared and bound", "result": result})


@APP.post("/api/start-stream")
def api_start_stream():
    payload = request.get_json(silent=True) or {}
    try:
        video_path = resolve_media_path(str(payload.get("video_path") or ""))
        output_url = stream_output_url(payload)
        ffmpeg_command(payload, video_path, output_url)
    except Exception as exc:
        if isinstance(exc, YouTubeAPIError):
            return youtube_error_response(exc)
        return jsonify({"ok": False, "message": str(exc)}), 400

    with STREAM_LIFECYCLE_LOCK:
        state = load_state()
        previous_pid = int(state.get("stream_pid") or 0)
        stop_result = stop_process(previous_pid)
        if not stop_result.get("ok"):
            return jsonify({"ok": False, "message": "failed to stop previous stream", "stop": stop_result}), 500
        try:
            result = launch_stream_process(
                payload,
                reason="manual-start",
                persist_recovery=True,
                resolved_output_url=output_url,
            )
            verify = verify_stream_started(result["pid"], Path(result["log_path"]))
            if not verify.get("ok"):
                state = load_state()
                auto_restart = dict(state.get("auto_restart") or {})
                auto_restart.update({"desired": False, "status": "failed", "last_error": verify.get("message") or "ffmpeg exited"})
                state["stream_desired"] = False
                state["stream_pid"] = 0
                state["auto_restart"] = auto_restart
                save_state(state)
                remove_stream_restart_payload()
                return jsonify({
                    "ok": False,
                    "message": verify.get("message") or "ffmpeg exited immediately",
                    "log_tail": verify.get("log_tail") or [],
                }), 502
        except Exception as exc:
            return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({
        "ok": True,
        "message": "stream started",
        "result": {
            "started_pid": result["pid"],
            "duplicate_processes": 1 if previous_pid and stop_result.get("ok") and not stop_result.get("skipped") else 0,
            "log_path": result["log_path"],
            "auto_restart": STREAM_AUTO_RESTART_ENABLED,
        },
    })


@APP.post("/api/stop-stream")
def api_stop_stream():
    with STREAM_LIFECYCLE_LOCK:
        state = load_state()
        stream_pid = int(state.get("stream_pid") or 0)
        auto_restart = dict(state.get("auto_restart") or {})
        auto_restart.update({"desired": False, "status": "stopping", "next_retry_at": 0})
        state["stream_desired"] = False
        state["auto_restart"] = auto_restart
        save_state(state)
        remove_stream_restart_payload()
        stop_result = stop_process(stream_pid)
        if not stop_result.get("ok"):
            return jsonify({
                "ok": False,
                "message": "failed to stop the Agent-owned stream process",
                "result": stop_result,
            }), 500

        state = load_state()
        state["has_stream_key"] = False
        state["stream_pid"] = 0
        state["stream_desired"] = False
        auto_restart = dict(state.get("auto_restart") or {})
        auto_restart.update({"desired": False, "status": "stopped", "next_retry_at": 0, "last_error": ""})
        state["auto_restart"] = auto_restart
        state["last_stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state["last_stop_result"] = stop_result
        save_state(state)
    return jsonify({
        "ok": True,
        "message": "stream stopped" if not stop_result.get("skipped") else "stream was not running",
        "result": stop_result,
    })


@APP.post("/api/restart-stream")
def api_restart_stream():
    with STREAM_LIFECYCLE_LOCK:
        payload = load_stream_restart_payload()
        if not payload:
            return jsonify({"ok": False, "message": "no active stream recovery configuration"}), 409
        state = load_state()
        previous_pid = int(state.get("stream_pid") or 0)
        state["stream_desired"] = True
        save_state(state)
        stop_result = stop_process(previous_pid)
        if not stop_result.get("ok"):
            return jsonify({"ok": False, "message": "failed to stop current stream", "stop": stop_result}), 500
        try:
            result = launch_stream_process(payload, reason="manual-restart", persist_recovery=False)
        except Exception as exc:
            return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({
        "ok": True,
        "message": "stream restarted",
        "result": {"previous_pid": previous_pid, "started_pid": result["pid"], "auto_restart": STREAM_AUTO_RESTART_ENABLED},
    })


@APP.post("/api/upgrade")
def api_upgrade():
    try:
        result = schedule_agent_upgrade()
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409
    return jsonify({
        "ok": True,
        "accepted": True,
        "message": "Agent upgrade scheduled; it will restart automatically",
        "result": result,
    }), 202


def main() -> None:
    ensure_dirs()
    start_stream_watchdog()
    try:
        from waitress import serve

        serve(APP, host=os.environ.get("STREAM_AGENT_HOST", "0.0.0.0"), port=PORT)
    except ImportError:
        APP.run(host=os.environ.get("STREAM_AGENT_HOST", "0.0.0.0"), port=PORT, threaded=True)


if __name__ == "__main__":
    main()
