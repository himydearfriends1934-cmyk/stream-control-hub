from __future__ import annotations

import ipaddress
import os
import platform
import shlex
import signal
import secrets
import subprocess
import time
from functools import wraps
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from flask import Flask, jsonify, redirect, request, session, url_for
import psutil

from .settings import *  # noqa: F403 - runtime keeps legacy constant names during extraction.
from .state import *  # noqa: F403 - runtime keeps legacy state names during extraction.
from .chat import (
    chat_runtime_snapshot,
    chat_scheduler_loop,
    load_chat_plan,
    save_chat_plan_data,
    update_chat_runtime,
)
from .youtube import youtube_auth_status
from .uploads import (
    PUBLIC_UPLOAD_PATHS,
    add_public_upload_cors,
    is_public_upload_authorized,
    is_public_upload_request,
    list_uploaded_videos,
    log_debug,
    public_upload_status,
    reset_public_upload_window_on_startup,
    transfer_runtime_snapshot,
)
from .streaming import (
    adaptive_state_snapshot,
    compute_resume_position_seconds,
    derive_resume_position_seconds,
    find_target_index,
    format_seek_seconds,
    load_stream_config,
    load_stream_runtime_state,
    load_stream_tuning,
    normalize_stream_payload,
    recommend_stream_settings,
    save_stream_config,
    save_stream_runtime_state,
    save_stream_tuning,
    select_runtime_target,
    stream_config_public_view,
    stream_output_url,
    stream_payload_signature,
    stream_relay_status,
    stream_target_view,
    update_adaptive_state,
)

APP = Flask(__name__)
APP.config["MAX_CONTENT_LENGTH"] = 50 * 1024 ** 3
UPLOAD_XHR = None
STREAM_MANUAL_STOP = False


def ensure_secret_key() -> str:
    if DASHBOARD_SECRET_FILE.exists():
        secret = DASHBOARD_SECRET_FILE.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    secret = secrets.token_urlsafe(32)
    DASHBOARD_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_SECRET_FILE.write_text(secret, encoding="utf-8")
    try:
        os.chmod(DASHBOARD_SECRET_FILE, 0o600)
    except OSError:
        pass
    return secret


APP.secret_key = ensure_secret_key()


def canonical_host() -> str:
    return (
        CANONICAL_PUBLIC_BASE.replace("https://", "")
        .replace("http://", "")
        .split("/", 1)[0]
        .strip()
        .lower()
    )


def is_internal_host(host: str) -> bool:
    host = host.strip().lower()
    if not host:
        return False
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    if host.endswith(".ts.net"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_private or addr.is_link_local:
        return True
    return addr in ipaddress.ip_network("100.64.0.0/10")


def normalize_remote_address(value: str | None) -> str:
    address = (value or "").strip()
    if address.startswith("::ffff:"):
        return address[7:]
    return address


def is_internal_address(value: str | None) -> bool:
    address = normalize_remote_address(value)
    if not address:
        return False
    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr in ipaddress.ip_network("100.64.0.0/10")
    )


def is_public_request() -> bool:
    return not is_internal_address(request.remote_addr)


def should_redirect_to_canonical() -> bool:
    if not canonical_host():
        return False
    requested_host = (request.host or "").strip().lower()
    host = requested_host.rsplit(":", 1)[0] if ":" in requested_host and not requested_host.startswith("[") else requested_host
    if not host:
        return False
    if is_internal_host(host):
        return False
    return requested_host != canonical_host()


@APP.before_request
def redirect_to_canonical_host():
    public_guard = guard_public_upload_request()
    if public_guard is not None:
        return public_guard
    if is_public_upload_request() and is_public_upload_authorized():
        return None
    if not should_redirect_to_canonical():
        return None
    target = f"{CANONICAL_PUBLIC_BASE}{request.full_path if request.query_string else request.path}"
    if target.endswith("?"):
        target = target[:-1]
    return redirect(target, code=302)


def dashboard_password() -> str:
    value = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if value:
        return value
    if DASHBOARD_PASSWORD_FILE.exists():
        return DASHBOARD_PASSWORD_FILE.read_text(encoding="utf-8").strip()
    return ""


def dashboard_auth_enabled() -> bool:
    return bool(dashboard_password())


def is_logged_in() -> bool:
    return bool(session.get("dashboard_authenticated"))


def guard_public_upload_request():
    if not PUBLIC_UPLOAD_RESTRICT or not is_public_request():
        return None
    if is_public_upload_request() and is_public_upload_authorized():
        return None
    if request.method == "OPTIONS" and request.path in PUBLIC_UPLOAD_PATHS:
        response = APP.response_class("", status=204)
        return add_public_upload_cors(response)
    return jsonify({
        "ok": False,
        "message": "??????????????????????",
    }), 403




def require_login_json():
    return jsonify({"ok": False, "message": "请先登录控制台"}), 401




def protected(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if dashboard_auth_enabled() and not is_logged_in():
            if request.path in PUBLIC_UPLOAD_PATHS and is_public_upload_authorized():
                return view(*args, **kwargs)
            if request.path in PUBLIC_UPLOAD_PATHS:
                log_debug(
                    f"upload blocked before handler ip={request.remote_addr} "
                    f"content_length={request.content_length} "
                    f"cookie_present={'session=' in (request.headers.get('Cookie', ''))}"
                )
            if request.path.startswith("/api/"):
                return require_login_json()
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        return view(*args, **kwargs)
    return wrapper



@APP.after_request
def add_cors_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    if request.path in PUBLIC_UPLOAD_PATHS:
        add_public_upload_cors(response)
    return response





@APP.errorhandler(RequestEntityTooLarge)
def handle_request_entity_too_large(exc):
    if request.path.startswith("/api/"):
        return jsonify({
            "ok": False,
            "message": "上传文件过大，超过当前允许的大小上限（50 GB）",
        }), 413
    return exc


@APP.errorhandler(Exception)
def handle_api_exception(exc):
    if isinstance(exc, HTTPException) and not request.path.startswith("/api/"):
        return exc
    if request.path.startswith("/api/"):
        if isinstance(exc, HTTPException):
            return jsonify({
                "ok": False,
                "message": exc.description or "请求失败",
            }), exc.code or 500
        return jsonify({
            "ok": False,
            "message": f"服务器内部错误：{exc}",
        }), 500
    raise exc




def format_seconds(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}??{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def bytes_to_rate_percent(total_bytes: int) -> float:
    gb = total_bytes / (1024 ** 3)
    return min(100.0, gb * 10.0)


def read_tracked_stream_pid() -> int | None:
    try:
        if PID_FILE.exists():
            return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    return None


def is_managed_stream_process(
    name: str,
    cmdline: str,
    *,
    pid: int | None = None,
    config: dict | None = None,
) -> bool:
    merged = f"{name} {cmdline}".lower()
    if "ffmpeg" not in merged:
        return False
    tracked_pid = read_tracked_stream_pid()
    if pid is not None and tracked_pid is not None and pid == tracked_pid:
        return True
    config = config or load_stream_config() or {}
    video_path = str(config.get("video_path") or "").strip().lower()
    if video_path and video_path in merged:
        return True
    return False


def is_youtube_stream_process(name: str, cmdline: str) -> bool:
    if is_managed_stream_process(name, cmdline):
        return True
    merged = f"{name} {cmdline}".lower()
    if "ffmpeg" not in merged:
        return False
    return any(tag in merged for tag in ("youtube", "live2", "rtmp"))


def stream_process_match_score(cmdline: str, config: dict | None = None) -> int:
    config = config or load_stream_config() or {}
    score = 0
    video_path = str(config.get("video_path") or "").strip()
    stream_url = str(config.get("stream_url") or "").strip().rstrip("/")
    relay_url = str(load_stream_tuning().get("relay_local_url") or "").strip()
    if video_path and video_path in cmdline:
        score += 10
    if stream_url and stream_url in cmdline:
        score += 6
    if relay_url and relay_url in cmdline:
        score += 6
    if "live2" in cmdline or "youtube" in cmdline.lower():
        score += 3
    if "-f flv" in cmdline:
        score += 1
    return score


def recover_tracked_stream_process() -> int | None:
    config = load_stream_config() or {}
    candidates = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            if proc.status() == psutil.STATUS_ZOMBIE:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            name = (proc.info.get("name") or "").lower()
            if not is_managed_stream_process(name, cmdline, pid=proc.pid, config=config):
                continue
            candidates.append((
                stream_process_match_score(cmdline, config),
                proc.info.get("create_time") or 0,
                proc.pid,
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    pid = int(candidates[0][2])
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(pid), encoding="utf-8")
    except OSError:
        pass
    return pid


def list_ffmpeg_processes() -> list[dict]:
    items = []
    tracked_pid = None
    try:
        if PID_FILE.exists():
            tracked_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        tracked_pid = None
    if tracked_pid is None:
        tracked_pid = recover_tracked_stream_process()
    config = load_stream_config() or {}
    for proc in psutil.process_iter(["pid", "name", "cmdline", "memory_info"]):
        try:
            status = proc.status()
            if status == psutil.STATUS_ZOMBIE:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            name = (proc.info.get("name") or "").lower()
            is_stream_proc = is_managed_stream_process(name, cmdline, pid=proc.pid, config=config)
            if "ffmpeg" not in name and "ffmpeg" not in cmdline.lower():
                continue
            if tracked_pid is not None:
                if proc.pid != tracked_pid:
                    continue
            elif not is_stream_proc:
                continue
            items.append({
                "pid": proc.pid,
                "cmdline": cmdline or "(empty)",
                "status": status,
                "cpu_percent": proc.cpu_percent(interval=0.0),
                "memory_mb": (proc.info["memory_info"].rss / 1024 / 1024) if proc.info.get("memory_info") else 0.0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return items


def tracked_stream_process_alive() -> bool:
    try:
        if not PID_FILE.exists():
            return recover_tracked_stream_process() is not None
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return recover_tracked_stream_process() is not None
        return proc.is_running()
    except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return recover_tracked_stream_process() is not None


def tracked_stream_process_start_time() -> float | None:
    try:
        pid = read_tracked_stream_pid()
        if pid is None:
            return None
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return float(proc.create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
        return None


def stream_progress_stalled(now: float) -> bool:
    if not tracked_stream_process_alive():
        return False
    started_at = tracked_stream_process_start_time() or STREAM_RESTART_STATE.get("last_started_at") or 0.0
    if started_at and now - started_at < STREAM_STARTUP_STALL_GRACE_SECONDS:
        return False
    position = read_recent_ffmpeg_position_seconds()
    if position is None:
        last_progress_at = float(STREAM_RESTART_STATE.get("last_progress_at") or started_at or now)
        STREAM_RESTART_STATE["last_progress_at"] = last_progress_at
        return now - last_progress_at > STREAM_STALL_TIMEOUT_SECONDS
    previous_position = STREAM_RESTART_STATE.get("last_progress_position")
    if previous_position is None or float(position) > float(previous_position) + 0.2:
        STREAM_RESTART_STATE["last_progress_position"] = float(position)
        STREAM_RESTART_STATE["last_progress_at"] = now
        return False
    last_progress_at = float(STREAM_RESTART_STATE.get("last_progress_at") or now)
    return now - last_progress_at > STREAM_STALL_TIMEOUT_SECONDS


def reap_tracked_stream_process() -> None:
    try:
        if not PID_FILE.exists():
            return
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return
    try:
        proc = psutil.Process(pid)
        parent = proc.parent()
        if proc.status() == psutil.STATUS_ZOMBIE and parent and parent.pid == os.getpid():
            os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        pass


def schedule_stream_restart(reason: str, *, delay: int | None = None) -> None:
    delay_seconds = STREAM_AUTORESTART_DELAY_SECONDS if delay is None else max(0, int(delay))
    now = time.time()
    STREAM_RESTART_STATE["last_exit_at"] = now
    STREAM_RESTART_STATE["next_restart_at"] = now + delay_seconds
    STREAM_RESTART_STATE["last_error"] = reason
    append_stream_log(f"{reason}; restart in {delay_seconds}s")


def stream_manual_stop_requested() -> bool:
    return STREAM_MANUAL_STOP or bool(load_stream_runtime_state().get("manual_stop"))


def append_stream_log(message: str) -> None:
    STREAM_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(STREAM_LOG_FILE, "ab") as log_file:
        log_file.write(f"\n===== dashboard {ts} {message} =====\n".encode("utf-8"))


def stop_stream_processes(manual: bool = True) -> dict:
    global STREAM_MANUAL_STOP
    if manual:
        STREAM_MANUAL_STOP = True
        save_stream_runtime_state(manual_stop=True, reason="manual stop requested")
    killed = []
    skipped = []
    targets = []
    config = load_stream_config() or {}
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
            name = (proc.info.get("name") or "").lower()
            if not is_managed_stream_process(name, cmdline, pid=proc.pid, config=config):
                skipped.append({"pid": proc.pid, "cmdline": cmdline})
                continue
            targets.append(proc)
            proc.send_signal(signal.SIGTERM)
            killed.append({"pid": proc.pid, "cmdline": cmdline})
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            skipped.append({"pid": proc.pid, "reason": str(exc)})

    gone, alive = psutil.wait_procs(targets, timeout=8)
    forced = []
    for proc in alive:
        try:
            proc.kill()
            forced.append({"pid": proc.pid})
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            skipped.append({"pid": proc.pid, "reason": f"kill failed: {exc}"})
    if alive:
        psutil.wait_procs(alive, timeout=3)

    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except OSError:
            pass
    append_stream_log("manual stop requested" if manual else "automatic cleanup requested")

    return {
        "killed": killed,
        "forced": forced,
        "stopped_count": len(gone) + len(forced),
        "skipped": skipped,
    }


def find_duplicate_stream_processes(keep_pid: int) -> list[dict]:
    duplicates = []
    config = load_stream_config() or {}
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.pid == keep_pid:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            name = (proc.info.get("name") or "").lower()
            if not is_managed_stream_process(name, cmdline, pid=proc.pid, config=config):
                continue
            duplicates.append({"pid": proc.pid, "cmdline": cmdline})
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            duplicates.append({"pid": proc.pid, "reason": str(exc)})
    return duplicates





def build_stream_command(payload: dict, *, disable_fifo: bool = False) -> list[str]:
    normalized = normalize_stream_payload(payload)
    stream_url = normalized["stream_url"]
    stream_key = normalized["stream_key"]
    video_path = normalized["video_path"]
    output_mode = normalized["stream_output_mode"]
    copy_mode = normalized["copy_mode"]
    preset = normalized["preset"]
    video_bitrate = normalized["video_bitrate"]
    audio_bitrate = normalized["audio_bitrate"]
    fps = normalized["fps"]
    resolution = normalized["resolution"]
    keyframe_seconds = normalized["keyframe_seconds"]
    gop = fps * keyframe_seconds
    bufsize = video_bitrate * 2
    resume_position_seconds = compute_resume_position_seconds(payload)
    output_url = stream_output_url(normalized)

    if not video_path:
        raise ValueError("Missing required video path")
    if output_mode == "direct" and (not stream_url or not stream_key):
        raise ValueError("Missing required stream configuration")
    if output_mode == "local_relay" and not output_url:
        raise ValueError("Missing local relay output URL")

    command = ["ffmpeg", "-re"]
    if resume_position_seconds > 0:
        command.extend(["-ss", format_seek_seconds(resume_position_seconds)])
    command.extend([
        "-stream_loop",
        "-1",
        "-i",
        video_path,
    ])

    tuning = load_stream_tuning()
    fifo_enabled = bool(tuning.get("fifo_enabled")) and not disable_fifo and not bool(payload.get("disable_fifo"))
    output_tail = [
        "-f",
        "flv",
        output_url,
    ]
    if fifo_enabled:
        output_tail = [
            "-f",
            "fifo",
            "-fifo_format",
            "flv",
            "-queue_size",
            str(tuning["fifo_queue_size"]),
            "-attempt_recovery",
            "1",
            "-drop_pkts_on_overflow",
            "1",
            "-restart_with_keyframe",
            "1",
            "-recovery_wait_time",
            str(tuning["fifo_recovery_wait_seconds"]),
            output_url,
        ]

    if copy_mode:
        return command + [
            "-c",
            "copy",
        ] + output_tail

    return command + [
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-b:v",
        f"{video_bitrate}k",
        "-maxrate",
        f"{video_bitrate}k",
        "-bufsize",
        f"{bufsize}k",
        "-r",
        str(fps),
        "-s",
        resolution,
        "-g",
        str(gop),
        "-keyint_min",
        str(gop),
        "-sc_threshold",
        "0",
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_bitrate}k",
        "-ar",
        "44100",
    ] + output_tail


def apply_runtime_recommendation(base_config: dict, target: dict) -> dict:
    merged = dict(base_config or {})
    merged.update(normalize_stream_payload(base_config))
    merged.update({
        "copy_mode": bool(target.get("copy_mode")),
        "preset": str(target.get("preset") or merged.get("preset") or "veryfast"),
        "video_bitrate": int(target.get("video_bitrate") or merged.get("video_bitrate") or 4500),
        "audio_bitrate": int(target.get("audio_bitrate") or merged.get("audio_bitrate") or 192),
        "fps": int(target.get("fps") or merged.get("fps") or 30),
        "resolution": str(target.get("resolution") or merged.get("resolution") or "1280x720"),
        "keyframe_seconds": int(target.get("keyframe_seconds") or merged.get("keyframe_seconds") or 2),
    })
    if merged["copy_mode"]:
        merged["preset"] = "copy"
    return merged


def start_stream_process(payload: dict, *, automatic_restart: bool = False) -> dict:
    global STREAM_MANUAL_STOP
    raw_payload = dict(payload or {})
    payload = dict(raw_payload)
    payload.update(normalize_stream_payload(raw_payload))
    saved_bounds = raw_payload.get("adaptive_bounds") if isinstance(raw_payload.get("adaptive_bounds"), dict) else {}
    saved_ladder = raw_payload.get("adaptive_ladder") if isinstance(raw_payload.get("adaptive_ladder"), list) else []
    if payload.get("adaptive_mode", "auto") == "auto":
        if saved_bounds and saved_ladder:
            payload["adaptive_bounds"] = saved_bounds
            payload["adaptive_ladder"] = saved_ladder
        else:
            recommendation = recommend_stream_settings(payload)
            payload = apply_runtime_recommendation(payload, recommendation.get("recommendation") or {})
            payload["adaptive_bounds"] = recommendation.get("quality_bounds") or {}
            payload["adaptive_ladder"] = recommendation.get("adaptive_ladder") or []
    else:
        payload["adaptive_bounds"] = saved_bounds
        payload["adaptive_ladder"] = saved_ladder or [stream_target_view(payload)]

    persist_payload = dict(payload)
    persist_payload.pop("resume_position_seconds", None)

    with STREAM_LOCK:
        STREAM_MANUAL_STOP = False
        save_stream_runtime_state(
            manual_stop=False,
            reason="automatic restart" if automatic_restart else "stream start",
        )
        stop_result = stop_stream_processes(manual=False)
        save_stream_config(persist_payload)
    command = build_stream_command(payload)
    tuning = load_stream_tuning()
    used_fifo = bool(tuning.get("fifo_enabled")) and not bool(payload.get("disable_fifo"))
    STREAM_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    def launch(command_to_run: list[str], label: str) -> subprocess.Popen:
        with open(STREAM_LOG_FILE, "ab") as log_file:
            log_file.write(
                f"\n===== {label} "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode("utf-8")
            )
            return subprocess.Popen(
                command_to_run,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(UPLOAD_DIR),
            )

    proc = launch(command, "automatic restart" if automatic_restart else "stream start")
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    STREAM_RESTART_STATE["last_started_at"] = time.time()
    STREAM_RESTART_STATE["last_progress_at"] = 0.0
    STREAM_RESTART_STATE["last_progress_position"] = None
    time.sleep(1.0)
    poll_result = proc.poll()
    if poll_result is not None:
        if used_fifo:
            append_stream_log(f"fifo output failed early with code {poll_result}; retrying without fifo")
            payload["disable_fifo"] = True
            persist_payload["disable_fifo"] = True
            save_stream_config(persist_payload)
            command = build_stream_command(payload, disable_fifo=True)
            proc = launch(command, "stream start fifo fallback")
            PID_FILE.write_text(str(proc.pid), encoding="utf-8")
            STREAM_RESTART_STATE["last_started_at"] = time.time()
            STREAM_RESTART_STATE["last_progress_at"] = 0.0
            STREAM_RESTART_STATE["last_progress_position"] = None
            time.sleep(1.0)
            poll_result = proc.poll()
        if poll_result is not None:
            raise RuntimeError(f"ffmpeg exited early with code {poll_result}")
    duplicate_processes = find_duplicate_stream_processes(proc.pid)
    tuning = load_stream_tuning()
    if automatic_restart:
        STREAM_RESTART_STATE["last_restart_at"] = time.time()
        STREAM_RESTART_STATE["restart_count"] += 1
        STREAM_RESTART_STATE["last_error"] = ""
        STREAM_RESTART_STATE["next_restart_at"] = 0.0
    update_adaptive_state(
        enabled=payload.get("adaptive_mode", "auto") == "auto" and bool(tuning["adaptive_enabled"]),
        active=payload.get("adaptive_mode", "auto") == "auto",
        status="running" if payload.get("adaptive_mode", "auto") == "auto" else "manual",
        last_error="",
        last_action="stream-start",
            last_reason="initial start",
            last_applied_at=time.time(),
            current_target=stream_target_view(payload),
            recommended_target=stream_target_view(payload),
            pending_direction="",
            pending_key="",
            pending_since=0.0,
            pending_streak=0,
            required_streak=0,
            required_delay_seconds=int(tuning.get("shift_confirm_seconds") or STREAM_ADAPTIVE_SHIFT_CONFIRM_SECONDS),
        )
    return {
        "started_pid": proc.pid,
        "command": shlex.join(command),
        "log_file": str(STREAM_LOG_FILE),
        "stopped_before_start": stop_result,
        "duplicate_processes": duplicate_processes,
    }


def evaluate_adaptive_stream() -> None:
    config = load_stream_config()
    if not config:
        update_adaptive_state(active=False, status="idle")
        return

    tuning = load_stream_tuning()
    raw_config = dict(config)
    config = dict(raw_config)
    config.update(normalize_stream_payload(raw_config))
    adaptive_mode = config.get("adaptive_mode", "auto")
    adaptive_enabled = bool(tuning["adaptive_enabled"]) and adaptive_mode == "auto"
    if not adaptive_enabled:
        update_adaptive_state(
            enabled=bool(tuning["adaptive_enabled"]),
            active=False,
            status="manual",
            current_target=stream_target_view(config),
            recommended_target=stream_target_view(config),
            last_metrics={},
        )
        return

    recommendation = recommend_stream_settings(config)
    bounds = raw_config.get("adaptive_bounds") if isinstance(raw_config.get("adaptive_bounds"), dict) else {}
    ladder = raw_config.get("adaptive_ladder") if isinstance(raw_config.get("adaptive_ladder"), list) else []
    if not bounds or not ladder:
        bounds = recommendation.get("quality_bounds") or {}
        ladder = recommendation.get("adaptive_ladder") or [stream_target_view(config)]
    analysis = recommendation.get("analysis") or {}
    source = analysis.get("source") or {}
    current_target = stream_target_view(config)
    current_index = find_target_index(ladder, current_target)
    recommended_index, recommended_target, reason = select_runtime_target(
        bounds=bounds,
        ladder=ladder,
        analysis=analysis,
        current_payload=current_target,
        tuning=tuning,
    )
    now = time.time()
    metrics = {
        "cpu_percent": analysis.get("cpu_percent"),
        "ffmpeg_speed": analysis.get("ffmpeg_speed"),
        "current_upload_kbps": analysis.get("current_upload_kbps"),
        "network_budget_kbps": analysis.get("network_budget_kbps"),
        "current_index": current_index,
        "recommended_index": recommended_index,
        "ladder_size": len(ladder),
    }
    update_adaptive_state(
        enabled=bool(tuning["adaptive_enabled"]),
        active=True,
        status="monitoring",
        last_error="",
        last_evaluated_at=now,
        current_target=current_target,
        recommended_target=stream_target_view(recommended_target),
        last_metrics=metrics,
    )

    if not tracked_stream_process_alive():
        update_adaptive_state(status="waiting-for-stream", last_reason="tracked FFmpeg process is not running")
        return

    adaptive_state = adaptive_state_snapshot()
    startup_reference = max(
        STREAM_RESTART_STATE.get("last_restart_at", 0.0),
        adaptive_state.get("last_applied_at", 0.0),
    )
    if startup_reference and now - startup_reference < int(tuning["warmup_seconds"]):
        update_adaptive_state(status="warmup", last_reason="waiting for post-start warmup window")
        return

    if recommended_index == current_index:
        update_adaptive_state(
            status="steady",
            last_reason=reason,
            pending_direction="",
            pending_key="",
            pending_since=0.0,
            pending_streak=0,
            required_streak=0,
            required_delay_seconds=int(tuning["shift_confirm_seconds"]),
            cooldown_until=max(adaptive_state.get("cooldown_until", 0.0), 0.0),
        )
        return

    direction = "down" if recommended_index > current_index else "up"
    required_delay_seconds = int(tuning["shift_confirm_seconds"])
    cooldown_seconds = (
        int(tuning["change_cooldown_seconds"])
        if direction == "down"
        else int(tuning["upshift_cooldown_seconds"])
    )
    target_key = str(recommended_index)
    same_pending = adaptive_state.get("pending_direction") == direction and adaptive_state.get("pending_key") == target_key
    pending_since = float(adaptive_state.get("pending_since") or 0.0) if same_pending else now
    pending_streak = (int(adaptive_state.get("pending_streak") or 0) + 1) if same_pending else 1
    pending_age = max(0.0, now - pending_since)

    cooldown_until = float(adaptive_state.get("cooldown_until") or 0.0)
    if cooldown_until and now < cooldown_until:
        update_adaptive_state(
            status="cooldown",
            last_reason=f"{reason}; cooldown active",
            pending_direction=direction,
            pending_key=target_key,
            pending_since=pending_since,
            pending_streak=pending_streak,
            required_streak=0,
            required_delay_seconds=required_delay_seconds,
        )
        return

    if pending_age < required_delay_seconds:
        update_adaptive_state(
            status="pending-change",
            last_reason=f"{reason}; confirming for {required_delay_seconds}s before shift",
            pending_direction=direction,
            pending_key=target_key,
            pending_since=pending_since,
            pending_streak=pending_streak,
            required_streak=0,
            required_delay_seconds=required_delay_seconds,
        )
        return

    new_config = apply_runtime_recommendation(config, recommended_target)
    new_config["adaptive_bounds"] = bounds
    new_config["adaptive_ladder"] = ladder
    new_config["resume_position_seconds"] = derive_resume_position_seconds(source)
    start_stream_process(new_config, automatic_restart=True)
    update_adaptive_state(
        enabled=bool(tuning["adaptive_enabled"]),
        active=True,
        status="applied-change",
        last_action=f"adaptive-{direction}",
        last_reason=reason,
        last_applied_at=time.time(),
        cooldown_until=time.time() + cooldown_seconds,
        pending_direction="",
        pending_key="",
        pending_since=0.0,
        pending_streak=0,
        required_streak=0,
        required_delay_seconds=required_delay_seconds,
        current_target=stream_target_view(new_config),
        recommended_target=stream_target_view(new_config),
        last_metrics=metrics,
    )
    append_stream_log(
        f"adaptive {direction} applied within quality guardrail: "
        f"{current_target.get('resolution')} {current_target.get('fps')}fps {current_target.get('video_bitrate')}k "
        f"-> {new_config.get('resolution')} {new_config.get('fps')}fps {new_config.get('video_bitrate')}k"
    )


def stream_watchdog_loop():
    while True:
        try:
            if not STREAM_AUTORESTART_ENABLED:
                time.sleep(STREAM_AUTORESTART_CHECK_SECONDS)
                continue

            now = time.time()
            if stream_manual_stop_requested():
                STREAM_RESTART_STATE["next_restart_at"] = 0.0
                STREAM_RESTART_STATE["last_error"] = "manual stop requested; autorestart paused"
                time.sleep(STREAM_AUTORESTART_CHECK_SECONDS)
                continue

            config = load_stream_config()
            process_alive = tracked_stream_process_alive()
            if not process_alive:
                reap_tracked_stream_process()
                try:
                    if PID_FILE.exists():
                        PID_FILE.unlink()
                except OSError:
                    pass
                if config:
                    if not STREAM_RESTART_STATE.get("next_restart_at"):
                        schedule_stream_restart("ffmpeg missing or exited unexpectedly")
                else:
                    STREAM_RESTART_STATE["last_error"] = "cannot auto restart: no saved stream config"
                    STREAM_RESTART_STATE["next_restart_at"] = 0.0
            elif stream_progress_stalled(now):
                STREAM_RESTART_STATE["stall_count"] += 1
                schedule_stream_restart("ffmpeg progress stalled; scheduled automatic restart", delay=0)

            next_restart_at = STREAM_RESTART_STATE.get("next_restart_at", 0.0)
            if next_restart_at and now >= next_restart_at:
                config = config or load_stream_config()
                if not config:
                    STREAM_RESTART_STATE["last_error"] = "cannot auto restart: no saved stream config"
                    STREAM_RESTART_STATE["next_restart_at"] = 0.0
                    append_stream_log("cannot auto restart: no saved stream config")
                    time.sleep(STREAM_AUTORESTART_CHECK_SECONDS)
                    continue
                try:
                    start_stream_process(config, automatic_restart=True)
                except Exception as exc:
                    STREAM_RESTART_STATE["last_error"] = f"auto restart failed: {exc}"
                    STREAM_RESTART_STATE["next_restart_at"] = time.time() + max(30, STREAM_AUTORESTART_DELAY_SECONDS)
                    append_stream_log(STREAM_RESTART_STATE["last_error"])

            try:
                evaluate_adaptive_stream()
            except Exception as exc:
                update_adaptive_state(status="error", last_error=str(exc))
                append_stream_log(f"adaptive watchdog error: {exc}")

            time.sleep(STREAM_AUTORESTART_CHECK_SECONDS)
        except Exception as exc:
            STREAM_RESTART_STATE["last_error"] = f"watchdog error: {exc}"
            append_stream_log(STREAM_RESTART_STATE["last_error"])
            time.sleep(10)


# UI routes live in dashboard_ui.py.
# Agent API routes live in agent_api.py.
