from __future__ import annotations

import json
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

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
    issue_records = []
    for issue in issues:
        if isinstance(issue, dict):
            issue_type = re.sub(r"[^a-z0-9]", "", str(issue.get("type") or "").lower())
            text = " ".join(str(issue.get(key) or "") for key in ("type", "severity", "reason", "description")).lower()
            issue_level = str(issue.get("severity") or "").strip().lower()
        else:
            issue_type = ""
            text = str(issue).lower()
            issue_level = ""
        issue_records.append({"type": issue_type, "text": text, "severity": issue_level})
    issue_text = " ".join(
        str(record["text"]) for record in issue_records
    )
    issue_types = {str(record["type"]) for record in issue_records if record["type"]}
    video_bitrate = max(800, min(40000, int(current.get("video_bitrate") or 4000)))
    audio_bitrate = max(64, min(384, int(current.get("audio_bitrate") or 128)))
    fps = max(15, min(60, int(current.get("fps") or 30)))
    resolution = str(current.get("resolution") or "1280x720")
    keyframe_seconds = max(1, min(4, int(current.get("keyframe_seconds") or 2)))
    preset = str(current.get("preset") or "veryfast") or "veryfast"
    copy_mode = bool(current.get("copy_mode"))

    actions: list[str] = []
    warnings: list[str] = []
    severity = "ok"

    def reduce_bitrate(percent: float, reason: str) -> None:
        nonlocal video_bitrate, severity, copy_mode
        video_bitrate = max(800, int(video_bitrate * (1.0 - percent)))
        copy_mode = False
        if severity != "critical":
            severity = "warning"
        actions.append(reason)

    def increase_bitrate(percent: float, reason: str) -> None:
        nonlocal video_bitrate, severity, copy_mode
        video_bitrate = min(40000, int(video_bitrate * (1.0 + percent)))
        copy_mode = False
        if severity != "critical":
            severity = "warning"
        actions.append(reason)

    def expected_bitrate(issue_type_names: set[str]) -> int | None:
        values = []
        for record in issue_records:
            if record["type"] not in issue_type_names:
                continue
            values.extend(
                float(match.group(1))
                for match in re.finditer(r"recommend[^.]{0,160}?bitrate(?:\s+of|\s*:)?\s*([0-9]+(?:\.[0-9]+)?)\s*kbps", str(record["text"]))
            )
        return int(min(values)) if values else None

    def mark_action(reason: str) -> None:
        nonlocal severity, copy_mode
        copy_mode = False
        if severity != "critical":
            severity = "warning"
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
    issue_levels = {str(record["severity"]) for record in issue_records}
    if "error" in issue_levels:
        severity = "critical"
    elif "warning" in issue_levels and severity == "ok":
        severity = "warning"

    video_high_types = {"bitratehigh"}
    video_low_types = {"bitratelow"}
    audio_high_types = {"audiobitratehigh"}
    audio_low_types = {"audiobitratelow"}
    frame_types = {"frameratehigh", "frameratemismatch"}
    keyframe_types = {"gopmismatch", "gopsizelong", "gopsizeover", "gopsizeshort", "opengop"}
    resolution_types = {"resolutionmismatch", "videoresolutionsuboptimal", "videoresolutionunsupported"}
    transcode_types = {
        "audiocodec", "audiosamplerate", "audiostereomismatch", "audiotoomanychannels",
        "badcontainer", "videocodec", "videoprofilemismatch",
    }
    starvation_issue = "videoingestionstarved" in issue_types or any(
        token in issue_text for token in ("ingestion starved", "not receiving enough video", "video output low")
    )
    high_bitrate_issue = bool(issue_types & video_high_types) or any(
        token in issue_text for token in ("bitrateishigh", "bitrate is high", "high bitrate", "too high")
    )
    low_bitrate_issue = bool(issue_types & video_low_types) or any(
        token in issue_text for token in ("bitrateislow", "bitrate is low", "low bitrate", "too low")
    )
    recommended_bitrate = expected_bitrate(video_high_types)
    if high_bitrate_issue and recommended_bitrate:
        recommended_bitrate = max(800, min(40000, recommended_bitrate))
        if recommended_bitrate < video_bitrate:
            video_bitrate = recommended_bitrate
            copy_mode = False
            if severity != "critical":
                severity = "warning"
            actions.append(f"YouTube recommends {recommended_bitrate} Kbps; use that video bitrate.")
        else:
            reduce_bitrate(0.20, "YouTube reports bitrate is too high; reduce video bitrate by about 20%.")
    elif high_bitrate_issue:
        reduce_bitrate(0.20, "YouTube reports bitrate is too high; reduce video bitrate by about 20%.")
    recommended_low_bitrate = expected_bitrate(video_low_types)
    if low_bitrate_issue and recommended_low_bitrate and recommended_low_bitrate > video_bitrate:
        video_bitrate = max(800, min(40000, recommended_low_bitrate))
        mark_action(f"YouTube recommends {video_bitrate} Kbps; use that video bitrate.")
    elif low_bitrate_issue:
        increase_bitrate(0.15, "YouTube reports bitrate is too low; increase video bitrate by about 15% if the Agent is stable.")
    recommended_audio_bitrate = expected_bitrate(audio_high_types)
    if issue_types & audio_high_types:
        if recommended_audio_bitrate:
            audio_bitrate = max(64, min(384, recommended_audio_bitrate))
            mark_action(f"YouTube recommends {audio_bitrate} Kbps audio; use that audio bitrate.")
        else:
            audio_bitrate = max(64, int(audio_bitrate * 0.8))
            mark_action("YouTube reports audio bitrate is too high; reduce audio bitrate by about 20%.")
    recommended_low_audio_bitrate = expected_bitrate(audio_low_types)
    if issue_types & audio_low_types:
        audio_bitrate = (
            max(64, min(384, recommended_low_audio_bitrate))
            if recommended_low_audio_bitrate and recommended_low_audio_bitrate > audio_bitrate
            else min(384, int(audio_bitrate * 1.15))
        )
        mark_action(f"YouTube reports audio bitrate is too low; use {audio_bitrate} Kbps audio.")
    if starvation_issue:
        if not high_bitrate_issue:
            reduce_bitrate(0.20, "YouTube reports video ingestion starvation; reduce bitrate to lower encoder and network load.")
        else:
            actions.append("YouTube also reports video ingestion starvation; the lower bitrate should reduce encoder and network load.")
        if preset not in {"ultrafast", "superfast"}:
            preset = "superfast"
            mark_action("Use the faster superfast encoder preset while ingestion is starved.")
    if issue_types & frame_types or any(token in issue_text for token in ("frame rate", "framerate", "fps")):
        fps = min(fps, 30)
        mark_action("YouTube reports a frame-rate issue; use 30 FPS.")
    if issue_types & keyframe_types or any(token in issue_text for token in ("keyframe", "key frame", "gop")):
        keyframe_seconds = 2
        mark_action("YouTube reports a keyframe/GOP issue; use 2-second keyframes.")
    if issue_types & resolution_types or "resolution" in issue_text:
        resolution = "1280x720"
        fps = min(fps, 30)
        video_bitrate = min(video_bitrate, 4000)
        mark_action("YouTube reports a resolution issue; use 1280x720 at no more than 30 FPS.")
    if issue_types & transcode_types:
        preset = "veryfast" if preset in {"copy", ""} else preset
        mark_action("YouTube reports a codec, container, profile, or audio-format issue; force H.264/AAC transcoding.")
    handled_types = video_high_types | video_low_types | audio_high_types | audio_low_types | frame_types | keyframe_types | resolution_types | transcode_types | {"videoingestionstarved"}
    unsupported_types = sorted(issue_types - handled_types)
    if unsupported_types:
        warnings.append(
            "YouTube issue(s) require source or primary/backup stream changes and were not auto-applied: "
            + ", ".join(unsupported_types)
        )
    if not actions and not warnings:
        actions.append("YouTube health is acceptable; keep the current encoder settings.")

    return {
        "ok": True,
        "severity": severity,
        "recommendation": {
            "copy_mode": copy_mode,
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
            "recommended_video_bitrate": recommended_bitrate or recommended_low_bitrate,
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
        quota_recorder: Callable[[str, str, int], None] | None = None,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.credential_path = credential_path
        self.timeout = timeout
        self.quota_recorder = quota_recorder
        self._lock = threading.RLock()
        self._device_sessions: dict[str, dict[str, Any]] = {}
        self._access_token = ""
        self._access_token_expires_at = 0.0

    def _quota_cost(self, method: str, resource: str) -> int:
        method = method.upper()
        resource = resource.lstrip("/")
        if method == "GET":
            return 1
        if resource in {"liveStreams", "liveBroadcasts", "liveBroadcasts/bind"}:
            return 50
        return 1

    def _record_quota(self, method: str, resource: str) -> None:
        if not self.quota_recorder:
            return
        try:
            self.quota_recorder(method.upper(), resource.lstrip("/"), self._quota_cost(method, resource))
        except Exception:
            pass

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

    def delegated_access_token(self) -> dict[str, Any]:
        access_token = self._access_token_value()
        with self._lock:
            expires_in = max(1, int(self._access_token_expires_at - time.time()))
            scope = str(self._load_credentials().get("scope") or YOUTUBE_SCOPE)
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "scope": scope,
        }

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
        self._record_quota(method, resource)
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

    def _paged_items(
        self,
        resource: str,
        *,
        params: dict[str, Any],
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_params = dict(params)
        for _ in range(max(1, max_pages)):
            payload = self._request("GET", resource, params=page_params)
            items.extend(item for item in payload.get("items") or [] if isinstance(item, dict))
            token = str(payload.get("nextPageToken") or "").strip()
            if not token:
                break
            page_params["pageToken"] = token
        return items

    def channel(self) -> dict[str, Any]:
        payload = self._request("GET", "channels", params={"part": "id,snippet", "mine": "true"})
        items = payload.get("items") or []
        if not items:
            raise YouTubeAPIError("The authorized Google account has no YouTube channel", status_code=409)
        item = items[0]
        snippet = item.get("snippet") or {}
        return {"id": str(item.get("id") or ""), "title": str(snippet.get("title") or "")}

    def list_streams(self) -> list[dict[str, Any]]:
        items = self._paged_items(
            "liveStreams",
            params={"part": "id,snippet,cdn,contentDetails,status", "mine": "true", "maxResults": 50},
        )
        result = []
        for item in items:
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
        items = self._paged_items(
            "liveBroadcasts",
            params={
                "part": "id,snippet,status,contentDetails,monetizationDetails",
                "broadcastStatus": "all",
                "maxResults": 50,
            },
        )
        result = []
        for item in items:
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
