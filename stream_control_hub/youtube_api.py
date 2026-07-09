from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
GOOGLE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"


def youtube_health_recommendation(
    health: dict[str, Any],
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = current or {}
    status = str(health.get("health_status") or health.get("status") or "").strip().lower()
    stream_status = str(health.get("stream_status") or "").strip().lower()
    issues = health.get("configuration_issues") or []
    issue_text = " ".join(
        " ".join(str(issue.get(key) or "") for key in ("type", "severity", "reason", "description"))
        if isinstance(issue, dict)
        else str(issue)
        for issue in issues
    ).lower()
    video_bitrate = max(800, min(20000, int(current.get("video_bitrate") or 4500)))
    audio_bitrate = max(64, min(320, int(current.get("audio_bitrate") or 192)))
    fps = max(15, min(60, int(current.get("fps") or 30)))
    resolution = str(current.get("resolution") or "1280x720")
    keyframe_seconds = max(1, min(4, int(current.get("keyframe_seconds") or 2)))
    preset = str(current.get("preset") or "veryfast") or "veryfast"

    actions: list[str] = []
    warnings: list[str] = []
    severity = "ok"

    def reduce_bitrate(percent: float, reason: str) -> None:
        nonlocal video_bitrate, severity
        video_bitrate = max(800, int(video_bitrate * (1.0 - percent)))
        severity = "warning"
        actions.append(reason)

    def increase_bitrate(percent: float, reason: str) -> None:
        nonlocal video_bitrate
        video_bitrate = min(20000, int(video_bitrate * (1.0 + percent)))
        actions.append(reason)

    if stream_status and stream_status not in {"active", "ready"}:
        severity = "warning"
        warnings.append(f"YouTube stream status is {stream_status}; YouTube may not be receiving the stream yet.")
    if status in {"bad", "no_data"}:
        severity = "critical" if status == "bad" else "warning"
    elif status in {"ok", "good"}:
        severity = "ok"
    elif status:
        severity = "warning"

    high_tokens = ("bitrateishigh", "bitrate is high", "high bitrate", "too high")
    low_tokens = ("bitrateislow", "bitrate is low", "low bitrate", "too low")
    frame_tokens = ("framerate", "frame rate", "fps")
    keyframe_tokens = ("keyframe", "key frame", "gop")
    resolution_tokens = ("resolution",)

    if any(token in issue_text for token in high_tokens):
        reduce_bitrate(0.20, "YouTube reports bitrate is too high; reduce video bitrate by about 20%.")
    if any(token in issue_text for token in low_tokens):
        increase_bitrate(0.15, "YouTube reports bitrate is too low; increase video bitrate by about 15% if the Agent is stable.")
    if any(token in issue_text for token in frame_tokens):
        fps = 30 if fps > 30 else fps
        actions.append("YouTube reports frame-rate mismatch; keep FPS at 30 unless the stream was created for 60fps.")
    if any(token in issue_text for token in keyframe_tokens):
        keyframe_seconds = 2
        actions.append("YouTube reports keyframe/GOP issue; use 2 second keyframes.")
    if any(token in issue_text for token in resolution_tokens):
        if resolution in {"1920x1080", "2560x1440", "3840x2160"}:
            resolution = "1280x720"
            fps = min(fps, 30)
            reduce_bitrate(0.10, "YouTube reports resolution mismatch; fall back to 720p/30fps.")
        else:
            actions.append("YouTube reports resolution mismatch; match the YouTube stream resolution and the encoder output.")
    if not actions and not warnings:
        actions.append("YouTube health is acceptable; keep the current encoder settings.")

    return {
        "ok": True,
        "severity": severity,
        "recommendation": {
            "copy_mode": False,
            "preset": preset,
            "video_bitrate": video_bitrate,
            "audio_bitrate": audio_bitrate,
            "fps": fps,
            "resolution": resolution,
            "keyframe_seconds": keyframe_seconds,
            "strategy": "youtube_health",
        },
        "analysis": {
            "youtube_health_status": status,
            "youtube_stream_status": stream_status,
            "configuration_issues": issues,
            "reasons": actions,
            "warnings": warnings,
        },
    }


class YouTubeAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502, reason: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


def _response_error(response: requests.Response, fallback: str) -> YouTubeAPIError:
    reason = ""
    message = fallback
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or fallback)
        errors = error.get("errors") or []
        if errors and isinstance(errors[0], dict):
            reason = str(errors[0].get("reason") or "")
    elif isinstance(error, str):
        reason = error
        message = str(payload.get("error_description") or error)
    detail = f"{reason} {message}".lower()
    if "org_internal" in detail:
        message = (
            "Google OAuth app is limited to internal organization users. "
            "In Google Cloud Console, change the OAuth consent screen user type to External "
            "or add this Google account as a test user, then authorize YouTube again."
        )
    return YouTubeAPIError(message, status_code=response.status_code or 502, reason=reason)


class YouTubeAPIClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str = "",
        credential_path: Path,
        timeout: int = 20,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.credential_path = credential_path
        self.timeout = timeout
        self._lock = threading.RLock()
        self._device_sessions: dict[str, dict[str, Any]] = {}
        self._access_token = ""
        self._access_token_expires_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    @property
    def authorized(self) -> bool:
        return bool(self._load_credentials().get("refresh_token"))

    def _load_credentials(self) -> dict[str, Any]:
        if not self.credential_path.exists():
            return {}
        try:
            payload = json.loads(self.credential_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_credentials(self, payload: dict[str, Any]) -> None:
        self.credential_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.credential_path.with_name(f".{self.credential_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self.credential_path)
            self.credential_path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def local_status(self) -> dict[str, Any]:
        credentials = self._load_credentials()
        return {
            "configured": self.configured,
            "authorized": bool(credentials.get("refresh_token")),
            "scope": str(credentials.get("scope") or ""),
            "authorized_at": str(credentials.get("authorized_at") or ""),
        }

    def start_device_authorization(self) -> dict[str, Any]:
        if not self.configured:
            raise YouTubeAPIError("YOUTUBE_CLIENT_ID is not configured", status_code=409)
        response = requests.post(
            GOOGLE_DEVICE_CODE_URL,
            data={"client_id": self.client_id, "scope": YOUTUBE_SCOPE},
            timeout=self.timeout,
        )
        if not response.ok:
            raise _response_error(response, "YouTube device authorization could not be started")
        payload = response.json()
        device_code = str(payload.get("device_code") or "")
        user_code = str(payload.get("user_code") or "")
        verification_url = str(payload.get("verification_url") or payload.get("verification_uri") or "")
        if not device_code or not user_code or not verification_url:
            raise YouTubeAPIError("Google returned an incomplete device authorization response")
        session_id = secrets.token_urlsafe(24)
        expires_in = max(60, int(payload.get("expires_in") or 1800))
        interval = max(5, int(payload.get("interval") or 5))
        with self._lock:
            self._device_sessions[session_id] = {
                "device_code": device_code,
                "expires_at": time.time() + expires_in,
                "interval": interval,
                "next_poll_at": 0.0,
            }
        return {
            "session_id": session_id,
            "user_code": user_code,
            "verification_url": verification_url,
            "expires_in": expires_in,
            "interval": interval,
        }

    def poll_device_authorization(self, session_id: str) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            session = self._device_sessions.get(session_id)
            if not session:
                raise YouTubeAPIError("YouTube authorization session not found", status_code=404)
            if now >= float(session.get("expires_at") or 0):
                self._device_sessions.pop(session_id, None)
                raise YouTubeAPIError("YouTube authorization code expired", status_code=410)
            next_poll_at = float(session.get("next_poll_at") or 0)
            if now < next_poll_at:
                return {"authorized": False, "pending": True, "retry_after": max(1, int(next_poll_at - now + 0.99))}
            interval = int(session.get("interval") or 5)
            session["next_poll_at"] = now + interval
            device_code = str(session.get("device_code") or "")

        token_payload = {
            "client_id": self.client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
        if self.client_secret:
            token_payload["client_secret"] = self.client_secret
        response = requests.post(GOOGLE_TOKEN_URL, data=token_payload, timeout=self.timeout)
        if not response.ok:
            error = _response_error(response, "YouTube authorization failed")
            if error.reason in {"authorization_pending", "slow_down"}:
                with self._lock:
                    if error.reason == "slow_down" and session_id in self._device_sessions:
                        self._device_sessions[session_id]["interval"] = interval + 5
                    retry = int(self._device_sessions.get(session_id, {}).get("interval") or interval)
                return {"authorized": False, "pending": True, "retry_after": retry}
            with self._lock:
                self._device_sessions.pop(session_id, None)
            raise error

        token = response.json()
        refresh_token = str(token.get("refresh_token") or "")
        if not refresh_token:
            raise YouTubeAPIError("Google did not return a refresh token")
        credentials = {
            "refresh_token": refresh_token,
            "scope": str(token.get("scope") or YOUTUBE_SCOPE),
            "authorized_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_credentials(credentials)
        with self._lock:
            self._access_token = str(token.get("access_token") or "")
            self._access_token_expires_at = time.time() + max(60, int(token.get("expires_in") or 3600))
            self._device_sessions.pop(session_id, None)
        return {"authorized": True, "pending": False}

    def _access_token_value(self) -> str:
        with self._lock:
            if self._access_token and time.time() < self._access_token_expires_at - 60:
                return self._access_token
            refresh_token = str(self._load_credentials().get("refresh_token") or "")
            if not refresh_token:
                raise YouTubeAPIError("YouTube account is not authorized on this Agent", status_code=409)
            token_payload = {
                "client_id": self.client_id,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
            if self.client_secret:
                token_payload["client_secret"] = self.client_secret
            response = requests.post(GOOGLE_TOKEN_URL, data=token_payload, timeout=self.timeout)
            if not response.ok:
                raise _response_error(response, "YouTube access token refresh failed")
            payload = response.json()
            access_token = str(payload.get("access_token") or "")
            if not access_token:
                raise YouTubeAPIError("Google did not return an access token")
            self._access_token = access_token
            self._access_token_expires_at = time.time() + max(60, int(payload.get("expires_in") or 3600))
            return access_token

    def _request(
        self,
        method: str,
        resource: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        token = self._access_token_value()
        response = requests.request(
            method,
            f"{YOUTUBE_API_BASE}/{resource.lstrip('/')}",
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout,
        )
        if response.status_code == 401 and retry_auth:
            with self._lock:
                self._access_token = ""
                self._access_token_expires_at = 0
            return self._request(method, resource, params=params, body=body, retry_auth=False)
        if not response.ok:
            raise _response_error(response, f"YouTube API request failed: {resource}")
        if not response.content:
            return {}
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def channel(self) -> dict[str, Any]:
        payload = self._request("GET", "channels", params={"part": "id,snippet", "mine": "true"})
        items = payload.get("items") or []
        if not items:
            raise YouTubeAPIError("The authorized Google account has no YouTube channel", status_code=409)
        item = items[0]
        snippet = item.get("snippet") or {}
        return {"id": str(item.get("id") or ""), "title": str(snippet.get("title") or "")}

    def list_streams(self) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "liveStreams",
            params={"part": "id,snippet,cdn,contentDetails,status", "mine": "true", "maxResults": 50},
        )
        result = []
        for item in payload.get("items") or []:
            snippet = item.get("snippet") or {}
            cdn = item.get("cdn") or {}
            status = item.get("status") or {}
            health = status.get("healthStatus") or {}
            result.append({
                "id": str(item.get("id") or ""),
                "title": str(snippet.get("title") or ""),
                "description": str(snippet.get("description") or ""),
                "published_at": str(snippet.get("publishedAt") or ""),
                "stream_status": str(status.get("streamStatus") or ""),
                "health_status": str(health.get("status") or ""),
                "configuration_issues": health.get("configurationIssues") or [],
                "resolution": str(cdn.get("resolution") or ""),
                "frame_rate": str(cdn.get("frameRate") or ""),
                "is_reusable": bool((item.get("contentDetails") or {}).get("isReusable")),
            })
        return result

    def list_broadcasts(self) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "liveBroadcasts",
            params={
                "part": "id,snippet,status,contentDetails,monetizationDetails",
                "broadcastStatus": "all",
                "maxResults": 50,
            },
        )
        result = []
        for item in payload.get("items") or []:
            snippet = item.get("snippet") or {}
            status = item.get("status") or {}
            details = item.get("contentDetails") or {}
            monitor = details.get("monitorStream") or {}
            availability = details.get("availabilityConfig") or {}
            monetization = item.get("monetizationDetails") or {}
            cuepoint = monetization.get("cuepointSchedule") or {}
            broadcast_id = str(item.get("id") or "")
            result.append({
                "id": broadcast_id,
                "title": str(snippet.get("title") or ""),
                "description": str(snippet.get("description") or ""),
                "published_at": str(snippet.get("publishedAt") or ""),
                "channel_id": str(snippet.get("channelId") or ""),
                "scheduled_start_time": str(snippet.get("scheduledStartTime") or ""),
                "scheduled_end_time": str(snippet.get("scheduledEndTime") or ""),
                "actual_start_time": str(snippet.get("actualStartTime") or ""),
                "actual_end_time": str(snippet.get("actualEndTime") or ""),
                "live_chat_id": str(snippet.get("liveChatId") or ""),
                "life_cycle_status": str(status.get("lifeCycleStatus") or ""),
                "privacy_status": str(status.get("privacyStatus") or ""),
                "recording_status": str(status.get("recordingStatus") or ""),
                "made_for_kids": status.get("madeForKids"),
                "self_declared_made_for_kids": status.get("selfDeclaredMadeForKids"),
                "bound_stream_id": str(details.get("boundStreamId") or ""),
                "bound_stream_last_update_time": str(details.get("boundStreamLastUpdateTimeMs") or ""),
                "enable_monitor_stream": monitor.get("enableMonitorStream"),
                "broadcast_stream_delay_ms": monitor.get("broadcastStreamDelayMs"),
                "enable_embed": details.get("enableEmbed"),
                "enable_dvr": details.get("enableDvr"),
                "record_from_start": details.get("recordFromStart"),
                "enable_closed_captions": details.get("enableClosedCaptions"),
                "closed_captions_type": str(details.get("closedCaptionsType") or ""),
                "projection": str(details.get("projection") or ""),
                "latency_preference": str(details.get("latencyPreference") or ""),
                "enable_auto_start": details.get("enableAutoStart"),
                "enable_auto_stop": details.get("enableAutoStop"),
                "availability_config": availability,
                "ads_monetization_status": str(monetization.get("adsMonetizationStatus") or ""),
                "eligible_for_ads_monetization": monetization.get("eligibleForAdsMonetization"),
                "cuepoint_schedule_enabled": cuepoint.get("enabled"),
                "watch_url": f"https://www.youtube.com/watch?v={broadcast_id}" if broadcast_id else "",
            })
        return result

    def prepare_broadcast(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "").strip()
        if not 1 <= len(title) <= 100:
            raise YouTubeAPIError("YouTube broadcast title must be 1 to 100 characters", status_code=400)
        privacy_status = str(payload.get("privacy_status") or "private").strip().lower()
        if privacy_status not in {"private", "unlisted", "public"}:
            raise YouTubeAPIError("privacy_status must be private, unlisted, or public", status_code=400)
        scheduled_start_time = str(payload.get("scheduled_start_time") or "").strip()
        if not scheduled_start_time:
            scheduled_start_time = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

        stream_id = str(payload.get("stream_id") or "").strip()
        if not stream_id:
            stream_title = str(payload.get("stream_title") or f"{title} encoder").strip()[:128]
            resolution = str(payload.get("resolution") or "720p").strip().lower()
            frame_rate = str(payload.get("frame_rate") or "30fps").strip().lower()
            if resolution not in {"240p", "360p", "480p", "720p", "1080p", "1440p", "2160p", "variable"}:
                raise YouTubeAPIError("unsupported YouTube stream resolution", status_code=400)
            if frame_rate not in {"30fps", "60fps", "variable"}:
                raise YouTubeAPIError("unsupported YouTube stream frame_rate", status_code=400)
            stream = self._request(
                "POST",
                "liveStreams",
                params={"part": "id,snippet,cdn,contentDetails,status"},
                body={
                    "snippet": {"title": stream_title, "description": str(payload.get("description") or "")[:10000]},
                    "cdn": {"ingestionType": "rtmp", "resolution": resolution, "frameRate": frame_rate},
                    "contentDetails": {"isReusable": True},
                },
            )
            stream_id = str(stream.get("id") or "")
            if not stream_id:
                raise YouTubeAPIError("YouTube did not return a stream ID")

        broadcast_status: dict[str, Any] = {"privacyStatus": privacy_status}
        if "made_for_kids" in payload:
            broadcast_status["selfDeclaredMadeForKids"] = bool(payload.get("made_for_kids"))
        broadcast = self._request(
            "POST",
            "liveBroadcasts",
            params={"part": "id,snippet,status,contentDetails"},
            body={
                "snippet": {
                    "title": title,
                    "description": str(payload.get("description") or "")[:5000],
                    "scheduledStartTime": scheduled_start_time,
                },
                "status": broadcast_status,
                "contentDetails": {
                    "enableAutoStart": bool(payload.get("enable_auto_start", True)),
                    "enableAutoStop": bool(payload.get("enable_auto_stop", True)),
                    "enableDvr": bool(payload.get("enable_dvr", True)),
                    "recordFromStart": True,
                },
            },
        )
        broadcast_id = str(broadcast.get("id") or "")
        if not broadcast_id:
            raise YouTubeAPIError("YouTube did not return a broadcast ID")
        self._request(
            "POST",
            "liveBroadcasts/bind",
            params={"id": broadcast_id, "streamId": stream_id, "part": "id,snippet,status,contentDetails"},
        )
        return {
            "broadcast_id": broadcast_id,
            "stream_id": stream_id,
            "title": title,
            "privacy_status": privacy_status,
            "scheduled_start_time": scheduled_start_time,
            "watch_url": f"https://www.youtube.com/watch?v={broadcast_id}",
        }

    def ingestion_target(self, stream_id: str) -> str:
        stream_id = stream_id.strip()
        if not stream_id:
            raise YouTubeAPIError("missing YouTube stream ID", status_code=400)
        payload = self._request("GET", "liveStreams", params={"part": "id,cdn", "id": stream_id})
        items = payload.get("items") or []
        if not items:
            raise YouTubeAPIError("YouTube stream was not found", status_code=404)
        ingestion = (items[0].get("cdn") or {}).get("ingestionInfo") or {}
        address = str(ingestion.get("ingestionAddress") or "").strip().rstrip("/")
        stream_name = str(ingestion.get("streamName") or "").strip()
        if not address or not stream_name:
            raise YouTubeAPIError("YouTube stream has no RTMP ingestion target", status_code=409)
        return f"{address}/{stream_name}"

    def stream_health(self, stream_id: str, current: dict[str, Any] | None = None) -> dict[str, Any]:
        stream_id = stream_id.strip()
        if not stream_id:
            raise YouTubeAPIError("missing YouTube stream ID", status_code=400)
        payload = self._request(
            "GET",
            "liveStreams",
            params={"part": "id,snippet,cdn,contentDetails,status", "id": stream_id},
        )
        items = payload.get("items") or []
        if not items:
            raise YouTubeAPIError("YouTube stream was not found", status_code=404)
        item = items[0]
        snippet = item.get("snippet") or {}
        cdn = item.get("cdn") or {}
        status = item.get("status") or {}
        health_status = status.get("healthStatus") or {}
        health = {
            "id": str(item.get("id") or ""),
            "title": str(snippet.get("title") or ""),
            "stream_status": str(status.get("streamStatus") or ""),
            "health_status": str(health_status.get("status") or ""),
            "configuration_issues": health_status.get("configurationIssues") or [],
            "resolution": str(cdn.get("resolution") or ""),
            "frame_rate": str(cdn.get("frameRate") or ""),
        }
        return {
            "ok": True,
            "health": health,
            **youtube_health_recommendation(health, current),
        }

    def revoke(self) -> None:
        credentials = self._load_credentials()
        refresh_token = str(credentials.get("refresh_token") or "")
        if refresh_token:
            response = requests.post(GOOGLE_REVOKE_URL, data={"token": refresh_token}, timeout=self.timeout)
            if not response.ok:
                raise _response_error(response, "YouTube authorization could not be revoked")
        self.credential_path.unlink(missing_ok=True)
        with self._lock:
            self._access_token = ""
            self._access_token_expires_at = 0
            self._device_sessions.clear()
