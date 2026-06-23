"""Streaming API routes for the VPS stream node agent."""

from __future__ import annotations

from flask import jsonify, request

from .runtime import (
    APP,
    protected,
    start_stream_process,
    stop_stream_processes,
)
from .streaming import (
    load_stream_config,
    load_stream_tuning,
    recommend_stream_settings,
    save_stream_tuning,
    stream_relay_status,
    update_adaptive_state,
)


@APP.route("/api/stop-stream", methods=["POST"])
@protected
def api_stop_stream():
    result = stop_stream_processes()
    return jsonify({
        "ok": True,
        "message": "已尝试停止匹配到的 FFmpeg 推流进程",
        **result,
    })


@APP.route("/api/start-stream", methods=["POST"])
@protected
def api_start_stream():
    payload = request.get_json(silent=True) or {}
    try:
        result = start_stream_process(payload)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({
        "ok": True,
        "message": "已启动新的 FFmpeg 推流进程",
        **result,
    })


@APP.route("/api/stream/recommend", methods=["POST"])
@protected
def api_stream_recommend():
    payload = request.get_json(silent=True) or {}
    try:
        result = recommend_stream_settings(payload)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({
        "ok": True,
        "message": "已生成智能推流参数建议",
        **result,
    })


@APP.route("/api/stream/tuning", methods=["GET", "POST"])
@protected
def api_stream_tuning():
    if request.method == "GET":
        tuning = load_stream_tuning()
    else:
        payload = request.get_json(silent=True) or {}
        try:
            tuning = save_stream_tuning(payload)
        except Exception as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        update_adaptive_state(enabled=bool(tuning["adaptive_enabled"]))
    return jsonify({
        "ok": True,
        "tuning": tuning,
        "relay": stream_relay_status(load_stream_config() or {}),
    })
