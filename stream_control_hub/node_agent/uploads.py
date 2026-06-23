"""Upload operations exposed by the VPS node agent."""

from __future__ import annotations

import secrets
import shlex
import subprocess
import threading
import time
from pathlib import Path

from flask import request
from werkzeug.utils import secure_filename

from .settings import (
    ALLOWED_UPLOAD_EXTENSIONS,
    APP_VERSION,
    MAX_UPLOAD_CHUNK_BYTES,
    PUBLIC_UPLOAD_CLOSE_ON_START,
    PUBLIC_UPLOAD_FIREWALL,
    PUBLIC_UPLOAD_FIREWALL_HELPER,
    PUBLIC_UPLOAD_INTERFACE,
    PUBLIC_UPLOAD_ORIGIN,
    PUBLIC_UPLOAD_PORT,
    PUBLIC_UPLOAD_RESTRICT,
    UPLOAD_COMPLETED_TTL_SECONDS,
    UPLOAD_DIR,
    UPLOAD_ID_PATTERN,
    UPLOAD_PART_DIR,
)
from .state import (
    PUBLIC_UPLOAD_LOCK,
    PUBLIC_UPLOAD_STATE,
    TRANSFER_RUNTIME,
    TRANSFER_RUNTIME_LOCK,
    TRANSFER_UPLOADS,
    UPLOAD_COMPLETED,
    UPLOAD_PART_LOCK,
)

PUBLIC_UPLOAD_PATHS = {"/api/upload-video", "/api/upload-probe", "/api/upload-chunk"}


def is_public_upload_request() -> bool:
    return request.method == "POST" and request.path in PUBLIC_UPLOAD_PATHS


def public_upload_token_from_request() -> str:
    return (request.headers.get("X-Public-Upload-Token") or "").strip()


def is_public_upload_authorized() -> bool:
    token = public_upload_token_from_request()
    with PUBLIC_UPLOAD_LOCK:
        expected = PUBLIC_UPLOAD_STATE.get("token") or ""
        enabled = bool(PUBLIC_UPLOAD_STATE.get("enabled"))
        expires_at = float(PUBLIC_UPLOAD_STATE.get("expires_at") or 0)
    return bool(
        enabled
        and expected
        and token
        and time.time() < expires_at
        and secrets.compare_digest(token, expected)
    )



def log_debug(message: str) -> None:
    print(f"[dashboard {APP_VERSION}] {message}", flush=True)


def upload_route_label() -> str:
    if is_public_upload_authorized():
        return "public-window"
    return "internal-or-dashboard"


def note_transfer_event(event: str, **kwargs) -> None:
    now = time.time()
    upload_id = str(kwargs.get("upload_id") or "").strip()
    chunk_size = int(kwargs.get("chunk_size") or 0)
    total_size = int(kwargs.get("total_size") or 0)
    filename = str(kwargs.get("filename") or "").strip()
    route = str(kwargs.get("route") or upload_route_label()).strip()
    error = str(kwargs.get("error") or "").strip()

    with TRANSFER_RUNTIME_LOCK:
        TRANSFER_RUNTIME["last_event_at"] = now
        TRANSFER_RUNTIME["last_event"] = event
        TRANSFER_RUNTIME["last_route"] = route
        if error:
            TRANSFER_RUNTIME["last_error"] = error
        elif event not in {"upload-error", "chunk-error", "probe-error"}:
            TRANSFER_RUNTIME["last_error"] = ""

        if chunk_size > 0:
            TRANSFER_RUNTIME["bytes_received_total"] += chunk_size
            TRANSFER_RUNTIME["chunks_received_total"] += 1
        if event in {"upload-complete", "chunk-complete"}:
            TRANSFER_RUNTIME["completed_uploads_total"] += 1

        if event in {"upload-start", "upload-complete", "upload-error", "chunk-start", "chunk-progress", "chunk-complete", "chunk-error"}:
            upload_view = {
                "upload_id": upload_id,
                "filename": filename,
                "route": route,
                "event": event,
                "updated_at": now,
                "received_size": int(kwargs.get("received_size") or 0),
                "total_size": total_size,
                "chunk_index": int(kwargs.get("chunk_index") or -1),
                "total_chunks": int(kwargs.get("total_chunks") or 0),
                "message": error or str(kwargs.get("message") or ""),
            }
            TRANSFER_RUNTIME["last_upload"] = upload_view
            if upload_id:
                if event in {"chunk-complete", "upload-complete", "chunk-error", "upload-error"}:
                    TRANSFER_UPLOADS.pop(upload_id, None)
                else:
                    TRANSFER_UPLOADS[upload_id] = upload_view

        if event in {"probe", "probe-error"}:
            TRANSFER_RUNTIME["last_probe"] = {
                "updated_at": now,
                "route": route,
                "size": int(kwargs.get("size") or 0),
                "elapsed_ms": float(kwargs.get("elapsed_ms") or 0),
                "message": error or str(kwargs.get("message") or ""),
            }


def transfer_runtime_snapshot() -> dict:
    with TRANSFER_RUNTIME_LOCK:
        active_uploads = [dict(item) for item in TRANSFER_UPLOADS.values()]
        snapshot = dict(TRANSFER_RUNTIME)
        snapshot["active_uploads"] = active_uploads
        snapshot["active_upload_count"] = len(active_uploads)
        snapshot["last_event_at_label"] = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snapshot["last_event_at"]))
            if snapshot.get("last_event_at") else ""
        )
        return snapshot


def add_public_upload_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Public-Upload-Token"
    return response


def public_upload_status(include_token: bool = False) -> dict:
    with PUBLIC_UPLOAD_LOCK:
        token = PUBLIC_UPLOAD_STATE.get("token", "")
        status = {
            "ok": True,
            "supported": PUBLIC_UPLOAD_FIREWALL in {"helper", "iptables"},
            "enabled": bool(PUBLIC_UPLOAD_STATE.get("enabled")),
            "public_origin": PUBLIC_UPLOAD_ORIGIN,
            "public_port": PUBLIC_UPLOAD_PORT,
            "expires_at": PUBLIC_UPLOAD_STATE.get("expires_at") or 0,
            "last_changed_at": PUBLIC_UPLOAD_STATE.get("last_changed_at") or 0,
            "last_changed_by": PUBLIC_UPLOAD_STATE.get("last_changed_by") or "",
            "last_reason": PUBLIC_UPLOAD_STATE.get("last_reason") or "",
            "active_uploads": int(PUBLIC_UPLOAD_STATE.get("active_uploads") or 0),
            "restrict_public_to_upload": PUBLIC_UPLOAD_RESTRICT,
        }
    if include_token:
        status["token"] = token
    return status


def ensure_public_upload_supported() -> None:
    if PUBLIC_UPLOAD_FIREWALL not in {"helper", "iptables"}:
        raise RuntimeError("当前环境没有启用公网窗口控制")


def set_public_upload_firewall(enabled: bool) -> None:
    ensure_public_upload_supported()
    if PUBLIC_UPLOAD_FIREWALL == "helper":
        action = "open" if enabled else "close"
        subprocess.run(
            ["sudo", "-n", PUBLIC_UPLOAD_FIREWALL_HELPER, action, str(PUBLIC_UPLOAD_PORT), PUBLIC_UPLOAD_INTERFACE],
            check=True,
            timeout=10,
            capture_output=True,
            text=True,
        )
        return

    port = shlex.quote(str(PUBLIC_UPLOAD_PORT))
    iface = shlex.quote(PUBLIC_UPLOAD_INTERFACE)
    delete_rules = (
        "while true; do "
        "line=$(iptables -L INPUT --line-numbers -n | "
        f"awk '$1 ~ /^[0-9]+$/ && $0 ~ /tcp dpt:{port}/ && ($0 ~ /DROP/ || $0 ~ /ACCEPT/) {{print $1; exit}}'); "
        "[ -n \"$line\" ] || break; "
        "iptables -D INPUT \"$line\"; "
        "done"
    )
    add_accept = f"iptables -C INPUT -p tcp -m tcp --dport {port} -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp -m tcp --dport {port} -j ACCEPT"
    add_drop = f"iptables -C INPUT -p tcp -m tcp --dport {port} -j DROP 2>/dev/null || iptables -I INPUT 1 -p tcp -m tcp --dport {port} -j DROP"
    add_tailscale_accept = f"iptables -C INPUT -i {iface} -p tcp -m tcp --dport {port} -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -i {iface} -p tcp -m tcp --dport {port} -j ACCEPT"
    add_loopback_accept = f"iptables -C INPUT -i lo -p tcp -m tcp --dport {port} -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -i lo -p tcp -m tcp --dport {port} -j ACCEPT"
    script = f"{delete_rules}; {add_accept}" if enabled else f"{delete_rules}; {add_drop}; {add_tailscale_accept}; {add_loopback_accept}"
    subprocess.run(
        ["sudo", "-n", "sh", "-c", script],
        check=True,
        timeout=10,
        capture_output=True,
        text=True,
    )


def clamp_public_upload_ttl(ttl_seconds: int | None) -> int:
    return max(60, min(3600, int(ttl_seconds or 900)))


def schedule_public_upload_expiry_locked(ttl_seconds: int) -> None:
    ttl = clamp_public_upload_ttl(ttl_seconds)

    old_timer = PUBLIC_UPLOAD_STATE.get("timer")
    if old_timer:
        old_timer.cancel()

    def expire_window():
        try:
            close_public_upload_window(reason="timeout", changed_by="system", force=True)
        except Exception as exc:
            log_debug(f"failed to close expired public upload window: {exc}")

    timer = threading.Timer(ttl, expire_window)
    timer.daemon = True
    PUBLIC_UPLOAD_STATE["timer"] = timer
    PUBLIC_UPLOAD_STATE["expires_at"] = time.time() + ttl
    timer.start()


def close_public_upload_window(
    reason: str = "manual",
    changed_by: str = "",
    *,
    release_auto: bool = False,
    force: bool = False,
) -> dict:
    should_disable_firewall = False
    keep_window_open = False
    with PUBLIC_UPLOAD_LOCK:
        enabled = bool(PUBLIC_UPLOAD_STATE.get("enabled"))
        active_uploads = int(PUBLIC_UPLOAD_STATE.get("active_uploads") or 0)
        if release_auto and active_uploads > 0:
            active_uploads -= 1

        if active_uploads > 0 and not force:
            if reason == "manual":
                raise RuntimeError("自动上传进行中，不能手动关闭公网窗口。")
            PUBLIC_UPLOAD_STATE.update({
                "active_uploads": active_uploads,
                "last_changed_at": time.time(),
                "last_changed_by": changed_by,
                "last_reason": reason,
            })
            schedule_public_upload_expiry_locked(900)
            keep_window_open = True
        else:
            timer = PUBLIC_UPLOAD_STATE.get("timer")
            if timer:
                timer.cancel()
            PUBLIC_UPLOAD_STATE.update({
                "enabled": False,
                "expires_at": 0.0,
                "last_changed_at": time.time(),
                "last_changed_by": changed_by,
                "last_reason": reason,
                "active_uploads": 0,
                "token": "",
                "timer": None,
            })
            should_disable_firewall = enabled

    if keep_window_open:
        return public_upload_status()
    if should_disable_firewall:
        set_public_upload_firewall(False)
    return public_upload_status()


def open_public_upload_window(
    ttl_seconds: int = 900,
    reason: str = "manual",
    changed_by: str = "",
    *,
    mode: str = "manual",
) -> dict:
    ttl = clamp_public_upload_ttl(ttl_seconds)
    mode = (mode or "manual").strip().lower()
    if mode not in {"manual", "auto"}:
        mode = "manual"

    with PUBLIC_UPLOAD_LOCK:
        if mode == "manual" and int(PUBLIC_UPLOAD_STATE.get("active_uploads") or 0) > 0:
            raise RuntimeError("自动上传进行中，不能手动改动公网窗口。")

    set_public_upload_firewall(True)

    with PUBLIC_UPLOAD_LOCK:
        token = PUBLIC_UPLOAD_STATE.get("token") or ""
        if not token or mode != "auto":
            token = secrets.token_urlsafe(32)
        active_uploads = int(PUBLIC_UPLOAD_STATE.get("active_uploads") or 0)
        if mode == "auto":
            active_uploads += 1
        PUBLIC_UPLOAD_STATE.update({
            "enabled": True,
            "last_changed_at": time.time(),
            "last_changed_by": changed_by,
            "last_reason": reason,
            "active_uploads": active_uploads,
            "token": token,
        })
        schedule_public_upload_expiry_locked(ttl)
    return public_upload_status(include_token=True)


def reset_public_upload_window_on_startup() -> None:
    if PUBLIC_UPLOAD_FIREWALL not in {"helper", "iptables"} or not PUBLIC_UPLOAD_CLOSE_ON_START:
        return
    try:
        set_public_upload_firewall(False)
        with PUBLIC_UPLOAD_LOCK:
            timer = PUBLIC_UPLOAD_STATE.get("timer")
            if timer:
                timer.cancel()
            PUBLIC_UPLOAD_STATE.update({
                "enabled": False,
                "expires_at": 0.0,
                "last_changed_at": time.time(),
                "last_changed_by": "system",
                "last_reason": "startup",
                "active_uploads": 0,
                "token": "",
                "timer": None,
            })
    except Exception as exc:
        log_debug(f"failed to close public upload window on startup: {exc}")


def touch_public_upload_window(
    ttl_seconds: int = 900,
    reason: str = "upload-heartbeat",
    changed_by: str = "",
    *,
    token: str = "",
) -> dict:
    ttl = clamp_public_upload_ttl(ttl_seconds)
    with PUBLIC_UPLOAD_LOCK:
        if not PUBLIC_UPLOAD_STATE.get("enabled"):
            raise RuntimeError("公网上传窗口当前未开启。")

        expected = str(PUBLIC_UPLOAD_STATE.get("token") or "")
        if expected and token and not secrets.compare_digest(token, expected):
            raise RuntimeError("公网上传窗口令牌已失效，请重新开始上传。")

        PUBLIC_UPLOAD_STATE.update({
            "last_changed_at": time.time(),
            "last_changed_by": changed_by,
            "last_reason": reason,
        })
        schedule_public_upload_expiry_locked(ttl)
    return public_upload_status(include_token=True)


def allowed_upload(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_UPLOAD_EXTENSIONS


def allocate_uploaded_video_target(original_name: str) -> Path:
    original_name = str(original_name or "").strip()
    original_suffix = Path(original_name).suffix.lower()
    if original_suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError("不支持这个视频格式，请上传 mp4、mov、mkv、m4v 或 webm")

    safe_name = secure_filename(original_name)
    safe_stem = Path(safe_name).stem.strip() if safe_name else ""
    if not safe_stem:
        safe_stem = f"upload-{time.strftime('%Y%m%d-%H%M%S')}"

    filename = f"{safe_stem}{original_suffix}"

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / filename
    counter = 1
    while target.exists():
        target = UPLOAD_DIR / f"{safe_stem}-{counter}{original_suffix}"
        counter += 1
    return target


def uploaded_video_result(target: Path) -> dict:
    return {
        "filename": target.name,
        "video_path": str(target),
        "size": target.stat().st_size,
    }


def save_uploaded_video(file_storage) -> dict:
    target = allocate_uploaded_video_target(str(file_storage.filename or "").strip())
    file_storage.save(target)
    return uploaded_video_result(target)


def partial_upload_path(upload_id: str) -> Path:
    upload_id = str(upload_id or "").strip()
    if not UPLOAD_ID_PATTERN.fullmatch(upload_id):
        raise ValueError("上传会话标识无效")
    UPLOAD_PART_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOAD_PART_DIR / f"{upload_id}.part"


def prune_completed_uploads_locked() -> None:
    cutoff = time.time() - UPLOAD_COMPLETED_TTL_SECONDS
    stale_ids = [
        upload_id
        for upload_id, result in UPLOAD_COMPLETED.items()
        if float(result.get("completed_at") or 0) < cutoff
    ]
    for upload_id in stale_ids:
        UPLOAD_COMPLETED.pop(upload_id, None)


def completed_upload_result(upload_id: str) -> dict | None:
    with UPLOAD_PART_LOCK:
        prune_completed_uploads_locked()
        result = UPLOAD_COMPLETED.get(upload_id)
        if not result:
            return None
        return dict(result.get("payload") or {})


def list_uploaded_videos() -> list[dict]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(UPLOAD_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_file():
            continue
        if path.suffix.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
            continue
        stat = path.stat()
        items.append({
            "name": path.name,
            "video_path": str(path),
            "size": stat.st_size,
            "modified": int(stat.st_mtime),
        })
    return items


def resolve_uploaded_video_path(video_path: str) -> Path:
    raw = Path(str(video_path or "").strip())
    if not raw:
        raise ValueError("没有收到视频路径")
    path = raw.resolve()
    upload_root = UPLOAD_DIR.resolve()
    try:
        path.relative_to(upload_root)
    except ValueError:
        raise ValueError("只能删除上传目录里的视频")
    if not path.exists() or not path.is_file():
        raise ValueError("视频文件不存在")
    if path.suffix.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError("不是受支持的视频文件")
    return path


__all__ = [
    "MAX_UPLOAD_CHUNK_BYTES",
    "PUBLIC_UPLOAD_PATHS",
    "UPLOAD_COMPLETED",
    "UPLOAD_PART_LOCK",
    "add_public_upload_cors",
    "allocate_uploaded_video_target",
    "allowed_upload",
    "close_public_upload_window",
    "completed_upload_result",
    "is_public_upload_authorized",
    "is_public_upload_request",
    "list_uploaded_videos",
    "log_debug",
    "note_transfer_event",
    "open_public_upload_window",
    "partial_upload_path",
    "prune_completed_uploads_locked",
    "public_upload_status",
    "reset_public_upload_window_on_startup",
    "resolve_uploaded_video_path",
    "save_uploaded_video",
    "touch_public_upload_window",
    "transfer_runtime_snapshot",
    "uploaded_video_result",
]
