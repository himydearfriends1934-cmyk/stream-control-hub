"""YouTube integration operations exposed by the VPS node agent."""

from __future__ import annotations

import json
import os

from .settings import YOUTUBE_CLIENT_FILE, YOUTUBE_SCOPES, YOUTUBE_TOKEN_FILE

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from googleapiclient.discovery import build
except Exception:
    Credentials = None
    Flow = None
    GoogleAuthRequest = None
    build = None

def infer_redirect_uri(base_url: str | None = None) -> str:
    if os.environ.get("YOUTUBE_OAUTH_REDIRECT_URI"):
        return os.environ["YOUTUBE_OAUTH_REDIRECT_URI"]
    base = (base_url or "").rstrip("/")
    return f"{base}/oauth2/callback" if base else "http://127.0.0.1:8787/oauth2/callback"


def youtube_auth_status(base_url: str | None = None) -> dict:
    token_info = {}
    authorized = False
    if YOUTUBE_TOKEN_FILE.exists():
        try:
            token_info = json.loads(YOUTUBE_TOKEN_FILE.read_text(encoding="utf-8"))
            authorized = bool(token_info.get("refresh_token") or token_info.get("token"))
        except Exception:
            token_info = {}
    return {
        "authorized": authorized,
        "has_client_config": YOUTUBE_CLIENT_FILE.exists(),
        "redirect_uri": infer_redirect_uri(base_url),
        "account_hint": token_info.get("account") or token_info.get("client_id", ""),
        "dependency_ready": all(x is not None for x in (Credentials, Flow, GoogleAuthRequest, build)),
    }


def load_youtube_client_config() -> dict:
    if not YOUTUBE_CLIENT_FILE.exists():
        raise RuntimeError("还没有保存 Google OAuth 客户端 JSON")
    data = json.loads(YOUTUBE_CLIENT_FILE.read_text(encoding="utf-8"))
    if "web" in data or "installed" in data:
        return data
    raise RuntimeError("Google OAuth 客户端 JSON 格式不正确，需要包含 web 或 installed")


def save_youtube_client_config(raw_json: str) -> dict:
    data = json.loads(raw_json)
    if "web" not in data and "installed" not in data:
        raise RuntimeError("Google OAuth 客户端 JSON 必须包含 web 或 installed")
    YOUTUBE_CLIENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    YOUTUBE_CLIENT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def load_youtube_credentials() -> Credentials | None:
    if Credentials is None or not YOUTUBE_TOKEN_FILE.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(YOUTUBE_TOKEN_FILE), YOUTUBE_SCOPES)
    except Exception:
        return None
    if creds and creds.expired and creds.refresh_token and GoogleAuthRequest is not None:
        creds.refresh(GoogleAuthRequest())
        YOUTUBE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_oauth_flow(base_url: str) -> Flow:
    if Flow is None:
        raise RuntimeError("Google OAuth 依赖还没有安装")
    flow = Flow.from_client_config(load_youtube_client_config(), scopes=YOUTUBE_SCOPES)
    flow.redirect_uri = infer_redirect_uri(base_url)
    return flow


def youtube_service():
    if build is None:
        raise RuntimeError("Google API 依赖还没有安装")
    creds = load_youtube_credentials()
    if not creds or not creds.valid:
        raise RuntimeError("YouTube 聊天还没有完成 Google 授权")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def get_active_live_chat_id() -> str | None:
    service = youtube_service()
    response = service.liveBroadcasts().list(
        part="snippet,status",
        broadcastStatus="active",
        mine=True,
        maxResults=10,
    ).execute()
    for item in response.get("items", []):
        live_chat_id = (item.get("snippet") or {}).get("liveChatId")
        if live_chat_id:
            return live_chat_id
    return None


def send_youtube_chat_message(message: str) -> dict:
    service = youtube_service()
    live_chat_id = get_active_live_chat_id()
    if not live_chat_id:
        raise RuntimeError("当前没有检测到正在直播的 YouTube 聊天室")
    body = {
        "snippet": {
            "liveChatId": live_chat_id,
            "type": "textMessageEvent",
            "textMessageDetails": {"messageText": message},
        }
    }
    response = service.liveChatMessages().insert(part="snippet", body=body).execute()
    return {
        "message_id": response.get("id", ""),
        "live_chat_id": live_chat_id,
        "message": message,
    }


__all__ = [
    "YOUTUBE_TOKEN_FILE",
    "build_oauth_flow",
    "get_active_live_chat_id",
    "save_youtube_client_config",
    "send_youtube_chat_message",
    "youtube_auth_status",
]
