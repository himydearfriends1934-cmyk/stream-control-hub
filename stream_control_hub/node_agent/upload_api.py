"""Upload and public upload-window API routes for the VPS stream node agent."""

from __future__ import annotations

import os
import shutil
import time

from flask import jsonify, request, session

from .runtime import APP, protected
from .uploads import (
    MAX_UPLOAD_CHUNK_BYTES,
    UPLOAD_COMPLETED,
    UPLOAD_PART_LOCK,
    allocate_uploaded_video_target,
    close_public_upload_window,
    is_public_upload_authorized,
    list_uploaded_videos,
    log_debug,
    note_transfer_event,
    open_public_upload_window,
    partial_upload_path,
    prune_completed_uploads_locked,
    public_upload_status,
    resolve_uploaded_video_path,
    save_uploaded_video,
    touch_public_upload_window,
    uploaded_video_result,
)

@APP.route("/api/public-upload", methods=["GET"])
@protected
def api_public_upload_status():
    return jsonify(public_upload_status())


@APP.route("/api/public-upload/open", methods=["POST"])
@protected
def api_public_upload_open():
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "auto").strip().lower()
    if mode != "auto":
        return jsonify({"ok": False, "message": "公网窗口只允许自动上传触发"}), 403
    try:
        status = open_public_upload_window(
            ttl_seconds=int(payload.get("ttl_seconds") or 900),
            reason=str(payload.get("reason") or "auto-upload"),
            changed_by=str(session.get("dashboard_authenticated") or "dashboard"),
            mode=mode,
        )
    except RuntimeError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify(status)


@APP.route("/api/public-upload/heartbeat", methods=["POST"])
@protected
def api_public_upload_heartbeat():
    payload = request.get_json(silent=True) or {}
    try:
        status = touch_public_upload_window(
            ttl_seconds=int(payload.get("ttl_seconds") or 900),
            reason=str(payload.get("reason") or "upload-heartbeat"),
            changed_by=str(session.get("dashboard_authenticated") or "dashboard"),
            token=str(payload.get("token") or ""),
        )
    except RuntimeError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify(status)


@APP.route("/api/public-upload/close", methods=["POST"])
@protected
def api_public_upload_close():
    payload = request.get_json(silent=True) or {}
    release_auto = bool(payload.get("release_auto"))
    force_close = bool(payload.get("force"))
    if not release_auto and not force_close:
        return jsonify({"ok": False, "message": "公网窗口只会在自动上传结束后自动关闭"}), 403
    try:
        status = close_public_upload_window(
            reason=str(payload.get("reason") or "manual"),
            changed_by="dashboard",
            release_auto=release_auto,
            force=force_close,
        )
    except RuntimeError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify(status)


@APP.route("/api/upload-video", methods=["POST"])
@protected
def api_upload_video():
    used_public_window = is_public_upload_authorized()
    route = "public-window" if used_public_window else "internal-or-dashboard"
    log_debug(
        f"upload request reached handler ip={request.remote_addr} "
        f"content_length={request.content_length} "
        f"files={list(request.files.keys())}"
    )
    file_storage = request.files.get("video")
    if not file_storage:
        log_debug(f"upload missing file ip={request.remote_addr}")
        note_transfer_event("upload-error", route=route, error="missing video file")
        if used_public_window:
            try:
                close_public_upload_window(reason="upload-ended", changed_by="system", release_auto=True)
            except Exception as exc:
                log_debug(f"failed to close public upload window after missing file: {exc}")
        return jsonify({"ok": False, "message": "没有收到视频文件"}), 400

    note_transfer_event(
        "upload-start",
        route=route,
        filename=getattr(file_storage, "filename", ""),
        total_size=int(request.content_length or 0),
    )

    try:
        result = save_uploaded_video(file_storage)
    except Exception as exc:
        log_debug(f"upload failed ip={request.remote_addr} filename={getattr(file_storage, 'filename', '')} error={exc}")
        note_transfer_event(
            "upload-error",
            route=route,
            filename=getattr(file_storage, "filename", ""),
            total_size=int(request.content_length or 0),
            error=str(exc),
        )
        if used_public_window:
            try:
                close_public_upload_window(reason="upload-ended", changed_by="system", release_auto=True)
            except Exception as close_exc:
                log_debug(f"failed to close public upload window after failed upload: {close_exc}")
        return jsonify({"ok": False, "message": str(exc)}), 400

    log_debug(
        f"upload success ip={request.remote_addr} "
        f"filename={getattr(file_storage, 'filename', '')} "
        f"path={result.get('video_path', '')}"
    )
    note_transfer_event(
        "upload-complete",
        route=route,
        filename=result.get("filename") or getattr(file_storage, "filename", ""),
        total_size=int(result.get("size") or request.content_length or 0),
        received_size=int(result.get("size") or 0),
        message="single request upload complete",
    )

    if used_public_window:
        try:
            close_public_upload_window(reason="upload-complete", changed_by="system", release_auto=True)
        except Exception as exc:
            log_debug(f"failed to close public upload window after upload: {exc}")

    return jsonify({
        "ok": True,
        "message": "视频已上传到服务器",
        **result,
    })


@APP.route("/api/upload-chunk", methods=["POST"])
@protected
def api_upload_chunk():
    used_public_window = is_public_upload_authorized()
    route = "public-window" if used_public_window else "internal-or-dashboard"
    chunk = request.files.get("chunk")
    try:
        upload_id = str(request.form.get("upload_id") or "").strip()
        original_name = str(request.form.get("filename") or "").strip()
        chunk_index = int(request.form.get("chunk_index") or -1)
        total_chunks = int(request.form.get("total_chunks") or 0)
        offset = int(request.form.get("offset") or -1)
        total_size = int(request.form.get("total_size") or 0)
        client_chunk_size = int(request.form.get("chunk_size") or MAX_UPLOAD_CHUNK_BYTES)
        part_path = partial_upload_path(upload_id)
        allocate_uploaded_video_target(original_name)
    except (TypeError, ValueError) as exc:
        note_transfer_event("chunk-error", route=route, error=str(exc))
        return jsonify({"ok": False, "message": str(exc)}), 400

    if not chunk:
        note_transfer_event("chunk-error", route=route, upload_id=upload_id, filename=original_name, error="missing chunk")
        return jsonify({"ok": False, "message": "没有收到上传分块"}), 400
    if chunk_index < 0 or total_chunks <= 0 or chunk_index >= total_chunks:
        note_transfer_event("chunk-error", route=route, upload_id=upload_id, filename=original_name, error="invalid chunk index")
        return jsonify({"ok": False, "message": "上传分块序号无效"}), 400
    if total_size <= 0 or total_size > APP.config["MAX_CONTENT_LENGTH"]:
        note_transfer_event("chunk-error", route=route, upload_id=upload_id, filename=original_name, error="invalid total size")
        return jsonify({"ok": False, "message": "视频大小超过当前允许范围"}), 400
    if offset < 0 or offset >= total_size:
        note_transfer_event("chunk-error", route=route, upload_id=upload_id, filename=original_name, error="invalid offset")
        return jsonify({"ok": False, "message": "上传分块偏移无效"}), 400
    if client_chunk_size <= 0 or client_chunk_size > MAX_UPLOAD_CHUNK_BYTES:
        note_transfer_event("chunk-error", route=route, upload_id=upload_id, filename=original_name, error="invalid client chunk size")
        return jsonify({"ok": False, "message": "上传分块配置无效"}), 400

    chunk.stream.seek(0, os.SEEK_END)
    chunk_size = chunk.stream.tell()
    chunk.stream.seek(0)
    if chunk_size <= 0 or chunk_size > MAX_UPLOAD_CHUNK_BYTES:
        note_transfer_event("chunk-error", route=route, upload_id=upload_id, filename=original_name, error="invalid chunk size")
        return jsonify({"ok": False, "message": "上传分块大小无效"}), 400
    if offset + chunk_size > total_size:
        note_transfer_event("chunk-error", route=route, upload_id=upload_id, filename=original_name, error="chunk exceeds total size")
        return jsonify({"ok": False, "message": "上传分块超出文件大小"}), 400
    expected_offset = chunk_index * client_chunk_size
    if chunk_index < total_chunks - 1 and chunk_size != client_chunk_size:
        note_transfer_event("chunk-error", route=route, upload_id=upload_id, filename=original_name, error="chunk size mismatch")
        return jsonify({"ok": False, "message": "非最后分块大小不一致，请重新上传"}), 400
    if offset != expected_offset:
        note_transfer_event("chunk-error", route=route, upload_id=upload_id, filename=original_name, error="offset and chunk index mismatch")
        return jsonify({"ok": False, "message": "上传分块偏移和序号不匹配"}), 400

    note_transfer_event(
        "chunk-start" if chunk_index == 0 else "chunk-progress",
        route=route,
        upload_id=upload_id,
        filename=original_name,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        chunk_size=chunk_size,
        total_size=total_size,
        received_size=offset + chunk_size,
    )

    with UPLOAD_PART_LOCK:
        prune_completed_uploads_locked()
        completed_payload = UPLOAD_COMPLETED.get(upload_id)
        if completed_payload:
            return jsonify({
                "ok": True,
                "complete": True,
                "received_size": total_size,
                "chunk_index": chunk_index,
                **dict(completed_payload.get("payload") or {}),
            })

        result = {}
        complete = False
        current_size = part_path.stat().st_size if part_path.exists() else 0
        expected_end = offset + chunk_size
        if current_size == offset:
            with part_path.open("ab") as part_file:
                shutil.copyfileobj(chunk.stream, part_file)
            current_size = part_path.stat().st_size
        elif current_size == total_size and chunk_index == total_chunks - 1:
            target = allocate_uploaded_video_target(original_name)
            part_path.replace(target)
            result = uploaded_video_result(target)
            UPLOAD_COMPLETED[upload_id] = {
                "completed_at": time.time(),
                "payload": result,
            }
            current_size = total_size
            complete = True
        elif current_size >= expected_end:
            return jsonify({
                "ok": True,
                "complete": False,
                "received_size": current_size,
                "chunk_index": chunk_index,
                "message": "这个分块已经收到，继续后续分块",
            })
        else:
            note_transfer_event(
                "chunk-error",
                route=route,
                upload_id=upload_id,
                filename=original_name,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                total_size=total_size,
                received_size=current_size,
                error="upload progress mismatch",
            )
            return jsonify({
                "ok": False,
                "message": "上传进度不一致，请取消后重新上传",
                "received_size": current_size,
            }), 409

        complete = complete or (current_size == total_size and chunk_index == total_chunks - 1)
        if complete and not result:
            target = allocate_uploaded_video_target(original_name)
            part_path.replace(target)
            result = uploaded_video_result(target)
            UPLOAD_COMPLETED[upload_id] = {
                "completed_at": time.time(),
                "payload": result,
            }
        else:
            result = {}

    note_transfer_event(
        "chunk-complete" if complete else "chunk-progress",
        route=route,
        upload_id=upload_id,
        filename=original_name,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        total_size=total_size,
        received_size=current_size,
        message="chunk upload complete" if complete else "chunk received",
    )

    if complete and used_public_window:
        try:
            close_public_upload_window(reason="upload-complete", changed_by="system", release_auto=True)
        except Exception as exc:
            log_debug(f"failed to close public upload window after chunk upload: {exc}")

    return jsonify({
        "ok": True,
        "complete": complete,
        "received_size": current_size,
        "chunk_index": chunk_index,
        **result,
    })


@APP.route("/api/upload-chunk/cancel", methods=["POST"])
@protected
def api_upload_chunk_cancel():
    payload = request.get_json(silent=True) or {}
    upload_id = str(payload.get("upload_id") or "")
    try:
        part_path = partial_upload_path(upload_id)
        with UPLOAD_PART_LOCK:
            if part_path.exists():
                part_path.unlink()
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    note_transfer_event("chunk-error", upload_id=upload_id, error="upload canceled")
    return jsonify({"ok": True, "message": "已清理未完成上传"})


@APP.route("/api/upload-probe", methods=["POST"])
@protected
def api_upload_probe():
    started_at = time.perf_counter()
    payload = request.get_data(cache=False)
    size = len(payload or b"")
    if size <= 0:
        note_transfer_event("probe-error", error="empty probe payload")
        return jsonify({"ok": False, "message": "没有收到测速数据"}), 400
    if size > 512 * 1024:
        note_transfer_event("probe-error", size=size, error="probe payload too large")
        return jsonify({"ok": False, "message": "测速数据过大"}), 400
    elapsed = max(0.001, time.perf_counter() - started_at)
    elapsed_ms = round(elapsed * 1000, 1)
    note_transfer_event("probe", size=size, elapsed_ms=elapsed_ms)
    return jsonify({
        "ok": True,
        "size": size,
        "elapsed_ms": elapsed_ms,
    })


@APP.route("/api/delete-video", methods=["POST"])
@protected
def api_delete_video():
    payload = request.get_json(silent=True) or {}

    try:
        path = resolve_uploaded_video_path(payload.get("video_path", ""))
        path.unlink()
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    return jsonify({
        "ok": True,
        "message": "已删除选中视频",
        "deleted_path": str(path),
        "videos": list_uploaded_videos(),
    })

