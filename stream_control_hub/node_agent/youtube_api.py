"""YouTube OAuth API routes for the VPS stream node agent."""

from __future__ import annotations

from flask import jsonify, request

from .chat import update_chat_runtime
from .runtime import APP, protected
from .youtube import (
    YOUTUBE_TOKEN_FILE,
    build_oauth_flow,
    save_youtube_client_config,
    youtube_auth_status,
)

@APP.route("/api/youtube-auth/client", methods=["POST"])
@protected
def api_youtube_auth_client():
    payload = request.get_json(silent=True) or {}
    raw_json = str(payload.get("client_json", "")).strip()
    if not raw_json:
        return jsonify({"ok": False, "message": "请先粘贴 Google OAuth 客户端 JSON"}), 400
    try:
        save_youtube_client_config(raw_json)
    except Exception as exc:
        return jsonify({"ok": False, "message": f"保存失败：{exc}"}), 400
    return jsonify({
        "ok": True,
        "message": "Google OAuth 客户端 JSON 已保存",
        "youtube_auth": youtube_auth_status(request.host_url.rstrip("/")),
    })


@APP.route("/api/youtube-auth/start")
@protected
def api_youtube_auth_start():
    try:
        flow = build_oauth_flow(request.host_url.rstrip("/"))
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
    except Exception as exc:
        return APP.response_class(
            f"<h1>启动 Google 授权失败</h1><pre>{exc}</pre>",
            mimetype="text/html",
            status=400,
        )
    request.environ["youtube_oauth_state"] = state
    return jsonify({
        "ok": True,
        "auth_url": auth_url,
    })


@APP.route("/oauth2/callback")
@protected
def youtube_oauth_callback():
    try:
        flow = build_oauth_flow(request.host_url.rstrip("/"))
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        YOUTUBE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        update_chat_runtime(status="authorized", last_error="")
        html = """
<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>授权完成</title>
<style>body{font-family:Segoe UI,Microsoft YaHei,sans-serif;background:#0b1220;color:#e2e8f0;padding:40px} .box{max-width:720px;margin:0 auto;background:#111827;border:1px solid rgba(148,163,184,.18);border-radius:20px;padding:28px}</style>
</head><body><div class="box"><h1>Google 授权已完成</h1><p>服务器已经拿到 YouTube 聊天权限。你现在可以回到监控面板，刷新页面后启用聊天计划，后面就不需要本地电脑一直开着了。</p><p>这个页面现在可以关闭。</p></div></body></html>
"""
        return APP.response_class(html, mimetype="text/html")
    except Exception as exc:
        return APP.response_class(
            f"<h1>Google 授权回调失败</h1><pre>{exc}</pre>",
            mimetype="text/html",
            status=400,
        )


@APP.route("/api/youtube-auth/clear", methods=["POST"])
@protected
def api_youtube_auth_clear():
    for path in (YOUTUBE_TOKEN_FILE,):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
    update_chat_runtime(status="idle", last_error="", last_sent_at=0.0, last_message="")
    return jsonify({
        "ok": True,
        "message": "已清除 YouTube 聊天授权",
        "youtube_auth": youtube_auth_status(request.host_url.rstrip("/")),
    })

