import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from stream_control_hub.youtube_api import YouTubeAPIClient, youtube_health_recommendation


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.content = json.dumps(payload).encode("utf-8") if payload is not None else b""

    def json(self):
        return self.payload


class YouTubeAPIClientTests(unittest.TestCase):
    def test_device_authorization_stores_only_agent_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            credential_path = Path(tmp) / "youtube_credentials.json"
            client = YouTubeAPIClient(
                client_id="device-client-id",
                client_secret="device-client-secret",
                credential_path=credential_path,
            )
            responses = [
                FakeResponse({
                    "device_code": "private-device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_url": "https://www.google.com/device",
                    "expires_in": 1800,
                    "interval": 5,
                }),
                FakeResponse({
                    "access_token": "private-access-token",
                    "refresh_token": "private-refresh-token",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/youtube",
                }),
            ]
            with patch("stream_control_hub.youtube_api.requests.post", side_effect=responses):
                started = client.start_device_authorization()
                completed = client.poll_device_authorization(started["session_id"])
            saved = json.loads(credential_path.read_text(encoding="utf-8"))
            credential_mode = stat.S_IMODE(credential_path.stat().st_mode)
            serialized = json.dumps({"started": started, "completed": completed})

        self.assertNotIn("private-device-code", serialized)
        self.assertNotIn("private-refresh-token", serialized)
        self.assertNotIn("private-access-token", serialized)
        self.assertEqual(saved["refresh_token"], "private-refresh-token")
        if os.name != "nt":
            self.assertEqual(credential_mode, 0o600)

    def test_device_authorization_explains_internal_oauth_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = YouTubeAPIClient(client_id="client-id", credential_path=Path(tmp) / "credentials.json")
            response = FakeResponse(
                {
                    "error": "org_internal",
                    "error_description": "This app is restricted to users within its organization.",
                },
                status_code=403,
            )
            with patch("stream_control_hub.youtube_api.requests.post", return_value=response):
                with self.assertRaises(Exception) as raised:
                    client.start_device_authorization()

        message = str(raised.exception)
        self.assertIn("limited to internal organization users", message)
        self.assertIn("OAuth consent screen", message)

    def test_delegated_access_token_does_not_expose_refresh_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            credential_path = Path(tmp) / "credentials.json"
            credential_path.write_text(json.dumps({
                "refresh_token": "private-refresh-token",
                "scope": "https://www.googleapis.com/auth/youtube",
            }), encoding="utf-8")
            client = YouTubeAPIClient(client_id="client-id", credential_path=credential_path)
            client._access_token_expires_at = 5000
            with patch.object(client, "_access_token_value", return_value="short-lived-access-token"), patch(
                "stream_control_hub.youtube_api.time.time", return_value=1401
            ):
                delegated = client.delegated_access_token()

        self.assertEqual(delegated["access_token"], "short-lived-access-token")
        self.assertEqual(delegated["expires_in"], 3599)
        self.assertNotIn("refresh_token", delegated)

    def test_stream_list_redacts_ingestion_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = YouTubeAPIClient(client_id="client-id", credential_path=Path(tmp) / "credentials.json")
            response = FakeResponse({
                "items": [{
                    "id": "stream-1",
                    "snippet": {"title": "Reusable stream"},
                    "cdn": {
                        "resolution": "720p",
                        "frameRate": "30fps",
                        "ingestionInfo": {
                            "ingestionAddress": "rtmp://example.test/live2",
                            "streamName": "private-stream-name",
                        },
                    },
                    "contentDetails": {"isReusable": True},
                    "status": {"streamStatus": "ready", "healthStatus": {"status": "good"}},
                }]
            })
            with patch.object(client, "_access_token_value", return_value="access-token"), patch(
                "stream_control_hub.youtube_api.requests.request", return_value=response
            ):
                streams = client.list_streams()

        self.assertEqual(streams[0]["id"], "stream-1")
        self.assertNotIn("private-stream-name", json.dumps(streams))

    def test_client_records_youtube_api_quota_units(self):
        recorded = []
        with tempfile.TemporaryDirectory() as tmp:
            client = YouTubeAPIClient(
                client_id="client-id",
                credential_path=Path(tmp) / "credentials.json",
                quota_recorder=lambda method, resource, units: recorded.append((method, resource, units)),
            )
            response = FakeResponse({"items": []})
            with patch.object(client, "_access_token_value", return_value="access-token"), patch(
                "stream_control_hub.youtube_api.requests.request", return_value=response
            ):
                client.list_streams()

        self.assertEqual(recorded, [("GET", "liveStreams", 1)])

    def test_stream_list_reads_paginated_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = YouTubeAPIClient(client_id="client-id", credential_path=Path(tmp) / "credentials.json")
            responses = [
                FakeResponse({
                    "nextPageToken": "page-2",
                    "items": [{"id": "stream-1", "snippet": {"title": "First"}}],
                }),
                FakeResponse({
                    "items": [{"id": "stream-2", "snippet": {"title": "Second"}}],
                }),
            ]
            with patch.object(client, "_access_token_value", return_value="access-token"), patch(
                "stream_control_hub.youtube_api.requests.request", side_effect=responses
            ) as request:
                streams = client.list_streams()

        self.assertEqual([item["id"] for item in streams], ["stream-1", "stream-2"])
        self.assertEqual(request.call_args_list[1].kwargs["params"]["pageToken"], "page-2")

    def test_prepare_creates_stream_broadcast_and_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = YouTubeAPIClient(client_id="client-id", credential_path=Path(tmp) / "credentials.json")
            with patch.object(
                client,
                "_request",
                side_effect=[{"id": "stream-1"}, {"id": "broadcast-1"}, {"id": "broadcast-1"}],
            ) as request:
                result = client.prepare_broadcast({"title": "Scheduled show"})

        self.assertEqual(result["stream_id"], "stream-1")
        self.assertEqual(result["broadcast_id"], "broadcast-1")
        self.assertEqual([call.args[1] for call in request.call_args_list], [
            "liveStreams",
            "liveBroadcasts",
            "liveBroadcasts/bind",
        ])
        self.assertEqual(request.call_args_list[2].kwargs["params"]["streamId"], "stream-1")
        self.assertNotIn("selfDeclaredMadeForKids", request.call_args_list[1].kwargs["body"]["status"])

    def test_broadcast_list_returns_studio_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = YouTubeAPIClient(client_id="client-id", credential_path=Path(tmp) / "credentials.json")
            with patch.object(
                client,
                "_request",
                return_value={
                    "items": [{
                        "id": "broadcast-1",
                        "snippet": {
                            "title": "Live show",
                            "description": "Full description",
                            "publishedAt": "2026-07-09T10:00:00Z",
                            "scheduledStartTime": "2026-07-09T11:00:00Z",
                            "scheduledEndTime": "2026-07-09T12:00:00Z",
                            "liveChatId": "chat-1",
                        },
                        "status": {
                            "lifeCycleStatus": "ready",
                            "privacyStatus": "private",
                            "recordingStatus": "notRecording",
                            "madeForKids": False,
                        },
                        "contentDetails": {
                            "boundStreamId": "stream-1",
                            "enableDvr": True,
                            "recordFromStart": True,
                            "enableAutoStart": True,
                            "enableAutoStop": False,
                            "latencyPreference": "low",
                            "monitorStream": {"broadcastStreamDelayMs": 0},
                        },
                        "monetizationDetails": {
                            "adsMonetizationStatus": "disabled",
                            "eligibleForAdsMonetization": False,
                        },
                    }]
                },
            ) as request:
                broadcasts = client.list_broadcasts()

        self.assertEqual(
            request.call_args.kwargs["params"]["part"],
            "id,snippet,status,contentDetails,monetizationDetails",
        )
        self.assertEqual(broadcasts[0]["description"], "Full description")
        self.assertEqual(broadcasts[0]["recording_status"], "notRecording")
        self.assertTrue(broadcasts[0]["enable_dvr"])
        self.assertTrue(broadcasts[0]["record_from_start"])
        self.assertEqual(broadcasts[0]["latency_preference"], "low")
        self.assertEqual(broadcasts[0]["broadcast_stream_delay_ms"], 0)
        self.assertEqual(broadcasts[0]["ads_monetization_status"], "disabled")

    def test_agent_resolves_youtube_target_without_hub_stream_key(self):
        from stream_control_hub import headless_agent

        with patch.object(
            headless_agent.YOUTUBE_CLIENT,
            "ingestion_target",
            return_value="rtmp://example.test/live2/private-stream-name",
        ) as target:
            output = headless_agent.stream_output_url({
                "stream_output_mode": "youtube_api",
                "youtube_stream_id": "stream-1",
                "stream_key": "",
            })

        self.assertEqual(output, "rtmp://example.test/live2/private-stream-name")
        target.assert_called_once_with("stream-1")

    def test_hub_forwards_youtube_stream_id_without_credentials(self):
        from stream_control_hub import app

        payload = app.stream_payload_for_node({
            "video_path": "video.mp4",
            "stream_output_mode": "youtube_api",
            "youtube_stream_id": "stream-1",
        })

        self.assertEqual(payload["youtube_stream_id"], "stream-1")
        self.assertEqual(payload["stream_key"], "")
        self.assertEqual(payload["youtube_ingestion_url"], "")

    def test_hub_splits_full_rtmp_url_pasted_as_stream_key(self):
        from stream_control_hub import app

        payload = app.stream_payload_for_node({
            "video_path": "video.mp4",
            "stream_key": "rtmp://a.rtmp.youtube.com/live2/private-key",
        })

        self.assertEqual(payload["stream_url"], "rtmp://a.rtmp.youtube.com/live2")
        self.assertEqual(payload["stream_key"], "private-key")

    def test_hub_starts_youtube_api_mode_without_stream_key(self):
        from stream_control_hub import app

        node = {"id": "node-a", "base_url": "http://100.64.0.10:8787", "enabled": True}
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            settings_file = Path(tmp) / "hub-settings.json"
            nodes_file.write_text(json.dumps([node]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app,
                "HUB_SETTINGS_FILE",
                settings_file,
            ), patch.object(
                app.YOUTUBE_CLIENT,
                "ingestion_target",
                return_value="rtmp://example.test/live2/private-stream-name",
            ), patch.object(
                app,
                "post_node_json",
                return_value={"ok": True, "result": {"started_pid": 4101}},
            ) as post:
                response = app.APP.test_client().post(
                    "/api/nodes/stream/start",
                    json={
                        "node_id": "node-a",
                        "video_path": "video.mp4",
                        "stream_output_mode": "youtube_api",
                        "youtube_stream_id": "stream-1",
                    },
                )
            settings = json.loads(settings_file.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        forwarded = post.call_args.args[2]
        self.assertEqual(forwarded["youtube_stream_id"], "stream-1")
        self.assertEqual(forwarded["youtube_ingestion_url"], "rtmp://example.test/live2/private-stream-name")
        self.assertEqual(forwarded["stream_key"], "")
        self.assertEqual(settings["node_youtube_profiles"]["node-a"], "default")
        self.assertEqual(settings["node_stream_locks"]["node-a"]["youtube_stream_id"], "stream-1")
        self.assertEqual(settings["node_stream_locks"]["node-a"]["video_path"], "video.mp4")

    def test_agent_saves_youtube_config_and_reloads_client(self):
        from stream_control_hub import headless_agent

        original_client = headless_agent.YOUTUBE_CLIENT
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".agent.env"
            credential_file = Path(tmp) / "youtube_credentials.json"
            try:
                with patch.object(headless_agent, "AGENT_ENV_FILE", env_file), patch.object(
                    headless_agent, "YOUTUBE_CREDENTIAL_FILE", credential_file
                ), patch.object(headless_agent, "CONTROL_TOKEN", ""):
                    response = headless_agent.APP.test_client().post(
                        "/api/youtube/config",
                        json={"client_id": "client-id", "client_secret": "client-secret"},
                    )
                    configured_client_id = headless_agent.YOUTUBE_CLIENT.client_id
            finally:
                headless_agent.YOUTUBE_CLIENT = original_client

            env_text = env_file.read_text(encoding="utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["configured"])
        self.assertIn("YOUTUBE_CLIENT_ID=client-id", env_text)
        self.assertIn("YOUTUBE_CLIENT_SECRET=client-secret", env_text)
        self.assertEqual(configured_client_id, "client-id")

    def test_hub_saves_youtube_config_locally(self):
        from stream_control_hub import app

        node = {"id": "node-a", "base_url": "http://100.64.0.10:8787", "enabled": True}
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            env_file = Path(tmp) / ".env"
            nodes_file.write_text(json.dumps([node]), encoding="utf-8")
            original_client = app.YOUTUBE_CLIENT
            try:
                with patch.object(app, "NODES_FILE", nodes_file), patch.object(app, "HUB_ENV_FILE", env_file):
                    response = app.APP.test_client().post(
                        "/api/nodes/youtube/config",
                        json={"node_id": "node-a", "client_id": "client-id", "client_secret": "client-secret"},
                    )
                    configured_client_id = app.YOUTUBE_CLIENT.client_id
                    env_text = env_file.read_text(encoding="utf-8")
            finally:
                app.YOUTUBE_CLIENT = original_client

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["mode"], "hub")
        self.assertEqual(configured_client_id, "client-id")
        self.assertIn("YOUTUBE_CLIENT_ID=client-id", env_text)

    def test_hub_youtube_config_preserves_existing_secret_when_blank(self):
        from stream_control_hub import app

        node = {"id": "node-a", "base_url": "http://100.64.0.10:8787", "enabled": True}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            nodes_file = tmp_path / "nodes.json"
            nodes_file.write_text(json.dumps([node]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "YOUTUBE_PROFILES_FILE", tmp_path / "youtube_profiles.json"
            ), patch.object(app, "YOUTUBE_USAGE_FILE", tmp_path / "youtube_usage.json"), patch.object(
                app, "YOUTUBE_PROFILE_CREDENTIALS_DIR", tmp_path / "profile_credentials"
            ):
                client = app.APP.test_client()
                first = client.post(
                    "/api/nodes/youtube/config",
                    json={
                        "node_id": "node-a",
                        "profile_id": "account-a",
                        "client_id": "client-a.apps.googleusercontent.com",
                        "client_secret": "keep-secret",
                    },
                )
                second = client.post(
                    "/api/nodes/youtube/config",
                    json={
                        "node_id": "node-a",
                        "profile_id": "account-a",
                        "client_id": "client-a.apps.googleusercontent.com",
                        "client_secret": "",
                    },
                )
                profile = app.youtube_profile_by_id("account-a")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(profile["client_secret"], "keep-secret")

    def test_hub_saves_agent_stream_lock(self):
        from stream_control_hub import app

        node = {"id": "node-a", "base_url": "http://100.64.0.10:8787", "enabled": True}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            nodes_file = tmp_path / "nodes.json"
            settings_file = tmp_path / "hub-settings.json"
            nodes_file.write_text(json.dumps([node]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "HUB_SETTINGS_FILE", settings_file
            ), patch.object(app, "request_node_json", return_value={"ok": True, "agent": {"version": "1.0"}}):
                client = app.APP.test_client()
                saved = client.post(
                    "/api/nodes/stream-lock",
                    json={
                        "node_id": "node-a",
                        "youtube_stream_id": "stream-1",
                        "video_path": "/srv/videos/show.mp4",
                        "library_media_name": "show.mp4",
                        "media_local": True,
                    },
                )
                listed = client.get("/api/nodes")

        self.assertEqual(saved.status_code, 200)
        lock = saved.get_json()["stream_lock"]
        self.assertEqual(lock["youtube_stream_id"], "stream-1")
        self.assertEqual(lock["library_media_name"], "show.mp4")
        self.assertEqual(listed.get_json()[0]["stream_lock"]["video_path"], "/srv/videos/show.mp4")

    def test_hub_refuses_to_revoke_profile_used_by_active_stream(self):
        from stream_control_hub import app

        node = {"id": "node-a", "name": "Node A", "base_url": "http://100.64.0.10:8787", "enabled": True}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            nodes_file = tmp_path / "nodes.json"
            nodes_file.write_text(json.dumps([node]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "YOUTUBE_PROFILES_FILE", tmp_path / "youtube_profiles.json"
            ), patch.object(app, "YOUTUBE_USAGE_FILE", tmp_path / "youtube_usage.json"), patch.object(
                app, "YOUTUBE_PROFILE_CREDENTIALS_DIR", tmp_path / "profile_credentials"
            ), patch.object(
                app,
                "request_node_json",
                return_value={
                    "ok": True,
                    "stream": {"running": True},
                    "stream_config": {
                        "stream_output_mode": "youtube_api",
                        "youtube_profile_id": "account-a",
                        "youtube_stream_id": "stream-1",
                    },
                },
            ), patch.object(app.YouTubeAPIClient, "revoke") as revoke:
                app.save_youtube_profile_config(
                    "account-a",
                    {
                        "name": "Account A",
                        "client_id": "client-a.apps.googleusercontent.com",
                        "client_secret": "secret",
                    },
                )
                response = app.APP.test_client().post(
                    "/api/nodes/youtube/oauth/revoke",
                    json={"node_id": "node-a", "profile_id": "account-a"},
                )

        self.assertEqual(response.status_code, 409)
        self.assertIn("active YouTube API streams", response.get_json()["message"])
        revoke.assert_not_called()

    def test_hub_manages_youtube_profiles(self):
        from stream_control_hub import app

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch.object(app, "YOUTUBE_PROFILES_FILE", tmp_path / "youtube_profiles.json"), patch.object(
                app, "YOUTUBE_USAGE_FILE", tmp_path / "youtube_usage.json"
            ), patch.object(app, "YOUTUBE_PROFILE_CREDENTIALS_DIR", tmp_path / "profile_credentials"):
                client = app.APP.test_client()
                created = client.post(
                    "/api/youtube/profiles",
                    json={
                        "profile_id": "account-a",
                        "name": "Account A",
                        "client_id": "client-a.apps.googleusercontent.com",
                        "auto_tune_enabled": True,
                        "auto_tune_interval_seconds": 600,
                        "auto_tune_cooldown_seconds": 1200,
                        "auto_tune_max_bitrate": 5500,
                    },
                )
                listed = client.get("/api/youtube/profiles")
                deleted = client.post("/api/youtube/profiles/delete", json={"profile_id": "account-a"})

        self.assertEqual(created.status_code, 200)
        profile = created.get_json()["profile"]
        self.assertEqual(profile["id"], "account-a")
        self.assertTrue(profile["auto_tune_enabled"])
        self.assertEqual(profile["auto_tune_interval_seconds"], 600)
        self.assertEqual(profile["auto_tune_cooldown_seconds"], 1200)
        self.assertEqual(profile["auto_tune_max_bitrate"], 5500)
        self.assertIn("account-a", [item["id"] for item in listed.get_json()["profiles"]])
        self.assertEqual(deleted.status_code, 200)

    def test_hub_delegates_only_short_lived_access_token_for_video_upload(self):
        from stream_control_hub import app

        client = MagicMock()
        client.local_status.return_value = {"configured": True, "authorized": True}
        client.delegated_access_token.return_value = {
            "access_token": "short-lived-access-token",
            "token_type": "Bearer",
            "expires_in": 3599,
            "scope": "https://www.googleapis.com/auth/youtube",
        }
        profile = {"id": "account-a", "client_secret": "must-not-leak"}
        with patch.object(app, "youtube_profile_by_id", return_value=profile), patch.object(
            app, "youtube_client_for_id", return_value=client
        ):
            response = app.APP.test_client().post(
                "/api/youtube/profiles/access-token",
                json={
                    "profile_id": "account-a",
                    "purpose": "youtube-video-upload",
                    "requester": "video-loop-manager",
                },
            )

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["profile_id"], "account-a")
        self.assertEqual(data["access_token"], "short-lived-access-token")
        self.assertNotIn("refresh_token", data)
        self.assertNotIn("client_secret", data)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_hub_rejects_access_token_delegation_for_other_purposes(self):
        from stream_control_hub import app

        with patch.object(app, "youtube_client_for_id") as client:
            response = app.APP.test_client().post(
                "/api/youtube/profiles/access-token",
                json={"profile_id": "account-a", "purpose": "other", "requester": "video-loop-manager"},
            )

        self.assertEqual(response.status_code, 400)
        client.assert_not_called()

    def test_youtube_profile_panel_layout_supports_rename_and_scroll(self):
        from stream_control_hub import app

        self.assertIn("const YOUTUBE_PROFILE_VISIBLE_SLOTS = 6", app.HTML)
        self.assertIn('class="youtube-control-strip"', app.HTML)
        self.assertIn("grid-template-columns: minmax(245px, 1.7fr) repeat(4, minmax(112px, 1fr))", app.HTML)
        self.assertIn('class="wizard-field youtube-control-item"', app.HTML)
        self.assertIn("API Usage Today（total 10000）", app.HTML)
        self.assertIn("Auto Tune State", app.HTML)
        self.assertIn("Auto Tune Time (s)", app.HTML)
        self.assertIn("Cooldown (s)", app.HTML)
        self.assertIn("Max Kbps", app.HTML)
        self.assertIn('class="youtube-more-actions"', app.HTML)
        self.assertIn('<summary>更多</summary>', app.HTML)
        self.assertIn('class="youtube-more-menu"', app.HTML)
        self.assertIn('<button id="youtubeWizardBtn">YouTube API</button>', app.HTML)
        self.assertNotIn('<button class="primary" id="youtubeWizardBtn">打开 YouTube 向导</button>', app.HTML)
        self.assertNotIn('<button id="youtubeImportJsonBtn">上传 API JSON</button>', app.HTML)
        self.assertIn('<button id="youtubePrepareBtn">创建/锁定直播目标</button>', app.HTML)
        self.assertIn('<button class="primary" id="youtubeSmartStartBtn">Smart Start</button>', app.HTML)
        self.assertNotIn('<button id="youtubePrepareBtn">创建并绑定直播</button>', app.HTML)
        self.assertNotIn('<button id="youtubePrepareBtn">准备直播目标</button>', app.HTML)
        self.assertIn('class="node-live-locks"', app.HTML)
        self.assertIn("data-node-stream-select", app.HTML)
        self.assertIn("data-node-video-select", app.HTML)
        self.assertIn('refs.youtubeSmartStartBtn.addEventListener("click", smartStartFromYouTubeWizard)', app.HTML)
        self.assertIn('class="wizard-field youtube-profile-row"', app.HTML)
        self.assertIn('class="youtube-profile-actions"', app.HTML)
        self.assertIn('class="wizard-field youtube-agent-row"', app.HTML)
        self.assertIn(".profile-chip.active", app.HTML)
        self.assertIn("border-color: #ff3b4f", app.HTML)
        self.assertIn(".youtube-agent-card.active", app.HTML)
        self.assertIn("let youtubeProfileClickTimer = null", app.HTML)
        self.assertIn("function scheduleYouTubeProfileSelect", app.HTML)
        self.assertIn("if (event.detail > 1) return;", app.HTML)
        self.assertIn("clearYouTubeProfileClickTimer();", app.HTML)
        self.assertIn('refs.youtubeProfileQuickBar.addEventListener("dblclick"', app.HTML)
        self.assertIn('data-youtube-profile-edit="${escapeHtml(profile.id)}"', app.HTML)
        self.assertIn('id="youtubeProfileNameInput" type="hidden"', app.HTML)
        self.assertIn('id="youtubeNodeInput" type="hidden"', app.HTML)
        self.assertIn("function saveYouTubeProfileName", app.HTML)
        self.assertIn("overflow-x: auto", app.HTML)
        self.assertNotIn('class="youtube-profile-name-line', app.HTML)
        self.assertNotIn('id="youtubeNodeInput" type="text"', app.HTML)

    def test_youtube_health_recommendation_reduces_high_bitrate(self):
        result = youtube_health_recommendation(
            {
                "stream_status": "active",
                "health_status": "bad",
                "configuration_issues": [{"type": "videoBitrateIsHigh", "description": "bitrate is high"}],
            },
            {"video_bitrate": 6000, "fps": 60, "resolution": "1920x1080"},
        )

        self.assertEqual(result["severity"], "critical")
        self.assertLess(result["recommendation"]["video_bitrate"], 6000)

    def test_youtube_health_handles_kiana_bitrate_and_starvation_issues(self):
        result = youtube_health_recommendation(
            {
                "stream_status": "active",
                "health_status": "good",
                "configuration_issues": [
                    {
                        "type": "bitrateHigh",
                        "severity": "info",
                        "reason": "Check video settings",
                        "description": "The stream's current bitrate (5256.74 Kbps) is higher than the recommended bitrate. We recommend that you use a stream bitrate of 2500 Kbps.",
                    },
                    {
                        "type": "videoIngestionStarved",
                        "severity": "error",
                        "reason": "Video output low",
                        "description": "YouTube is not receiving enough video to maintain smooth streaming.",
                    },
                ],
            },
            {"video_bitrate": 4500, "fps": 30, "resolution": "1280x720"},
        )

        self.assertEqual(result["severity"], "critical")
        self.assertEqual(result["recommendation"]["video_bitrate"], 2500)
        self.assertEqual(result["recommendation"]["preset"], "superfast")
        self.assertTrue(any("ingestion starvation" in reason for reason in result["analysis"]["reasons"]))

    def test_youtube_health_keeps_critical_severity_when_reducing_bitrate(self):
        result = youtube_health_recommendation(
            {
                "stream_status": "active",
                "health_status": "bad",
                "configuration_issues": [{"type": "videoIngestionStarved", "description": "Video output low"}],
            },
            {"video_bitrate": 4500},
        )

        self.assertEqual(result["severity"], "critical")
        self.assertLess(result["recommendation"]["video_bitrate"], 4500)

    def test_youtube_health_maps_structured_encoder_issue_categories(self):
        cases = [
            ("audioBitrateHigh", {"description": "We recommend that you use an audio stream bitrate of 128 Kbps."}, {"audio_bitrate": 192}, "audio_bitrate", 128),
            ("frameRateHigh", {}, {"fps": 60}, "fps", 30),
            ("gopSizeLong", {}, {"keyframe_seconds": 4}, "keyframe_seconds", 2),
            ("videoResolutionUnsupported", {}, {"resolution": "1920x1080", "fps": 60}, "resolution", "1280x720"),
            ("videoCodec", {}, {"copy_mode": True}, "copy_mode", False),
        ]
        for issue_type, issue_data, current, parameter, expected in cases:
            with self.subTest(issue_type=issue_type):
                result = youtube_health_recommendation(
                    {
                        "stream_status": "active",
                        "health_status": "good",
                        "configuration_issues": [{"type": issue_type, "severity": "warning", **issue_data}],
                    },
                    current,
                )
                self.assertIn(result["severity"], {"warning", "critical"})
                self.assertEqual(result["recommendation"][parameter], expected)

    def test_youtube_health_records_unknown_issue_without_guessing_parameter_change(self):
        current = {
            "copy_mode": False,
            "video_bitrate": 4500,
            "audio_bitrate": 192,
            "fps": 30,
            "resolution": "1280x720",
            "keyframe_seconds": 2,
            "preset": "veryfast",
        }
        result = youtube_health_recommendation(
            {
                "stream_status": "active",
                "health_status": "good",
                "configuration_issues": [{"type": "futureUnknownIssue", "severity": "warning"}],
            },
            current,
        )

        for key, value in current.items():
            self.assertEqual(result["recommendation"][key], value)
        self.assertTrue(any("futureunknownissue" in warning for warning in result["analysis"]["warnings"]))

    def test_autotune_history_records_api_problem_before_change_and_verified_after(self):
        from stream_control_hub import app

        stream_config = {
            "stream_output_mode": "youtube_api",
            "youtube_stream_id": "stream-a",
            "youtube_profile_id": "account-a",
            "youtube_ingestion_url": "rtmp://example.invalid/live",
            "resolution": "1920x1080",
            "fps": 30,
            "video_bitrate": 6000,
            "audio_bitrate": 192,
            "preset": "veryfast",
            "keyframe_seconds": 2,
        }
        health = {
            "ok": True,
            "severity": "critical",
            "health": {"configuration_issues": [{"type": "videoBitrateIsHigh", "description": "bitrate is high"}]},
            "analysis": {"reasons": ["Reduce bitrate"], "warnings": []},
            "recommendation": {**stream_config, "video_bitrate": 4800},
        }
        client = MagicMock()
        client.local_status.return_value = {"authorized": True}
        client.stream_health.return_value = health

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "youtube_autotune_state.json"
            state_file.write_text(json.dumps({
                "entries": {
                    "node-a:account-a:stream-a": {"consecutive_issues": 0, "last_check": 0, "last_adjusted": 0}
                }
            }), encoding="utf-8")
            status = MagicMock(side_effect=[
                {"ok": True, "stream": {"running": True}, "stream_config": stream_config},
                {"ok": True, "stream_config": {**stream_config, "video_bitrate": 4800}},
            ])
            with patch.object(app, "YOUTUBE_AUTOTUNE_STATE_FILE", state_file), patch.object(
                app, "load_youtube_profiles_config", return_value={
                    "active_profile_id": "account-a",
                    "profiles": [{
                        "id": "account-a",
                        "auto_tune_enabled": True,
                        "auto_tune_interval_seconds": 300,
                        "auto_tune_cooldown_seconds": 900,
                        "auto_tune_min_bitrate": 800,
                        "auto_tune_max_bitrate": 6000,
                    }],
                }
            ), patch.object(app, "load_nodes", return_value=[{"id": "node-a", "name": "Node A", "enabled": True}]), patch.object(
                app, "request_node_json", status
            ), patch.object(app, "youtube_client_for_id", return_value=client), patch.object(
                app, "post_node_json", return_value={"ok": True, "message": "stream restarted"}
            ), patch.object(app.time, "time", return_value=10_000):
                result = app.youtube_autotune_tick()

            saved = json.loads(state_file.read_text(encoding="utf-8"))

        self.assertEqual(result["adjusted"], 1)
        event = saved["history"][0]
        self.assertEqual(event["outcome"], "adjusted")
        self.assertEqual(event["before"]["video_bitrate"], 6000)
        self.assertEqual(event["changes"]["video_bitrate"], 4800)
        self.assertEqual(event["after"]["video_bitrate"], 4800)
        self.assertTrue(any("videoBitrateIsHigh" in item for item in event["api_problems"]))
        self.assertIn("Reduce bitrate", event["recommendation_reasons"])
        self.assertNotIn("youtube_ingestion_url", json.dumps(event))

    def test_autotune_restores_recommended_bitrate_on_third_persistent_adjustment(self):
        from stream_control_hub import app

        def stream_config(bitrate):
            return {
                "stream_output_mode": "youtube_api",
                "youtube_stream_id": "stream-a",
                "youtube_profile_id": "account-a",
                "youtube_ingestion_url": "rtmp://example.invalid/live",
                "resolution": "720x1280",
                "fps": 30,
                "video_bitrate": bitrate,
                "audio_bitrate": 192,
                "preset": "superfast",
                "keyframe_seconds": 2,
            }

        def starvation_health(current_bitrate, next_bitrate):
            return {
                "ok": True,
                "severity": "critical",
                "analysis": {
                    "configuration_issues": [{"type": "videoIngestionStarved", "severity": "error"}],
                    "recommended_video_bitrate": 2500,
                    "reasons": ["Reduce encoder load"],
                    "warnings": [],
                },
                "recommendation": {**stream_config(current_bitrate), "video_bitrate": next_bitrate},
            }

        client = MagicMock()
        client.local_status.return_value = {"authorized": True}
        client.stream_health.side_effect = [
            starvation_health(4500, 3600),
            starvation_health(3600, 2880),
            starvation_health(2880, 2304),
            starvation_health(2500, 2000),
        ]
        statuses = MagicMock(side_effect=[
            {"ok": True, "stream": {"running": True}, "stream_config": stream_config(4500)},
            {"ok": True, "stream_config": stream_config(3600)},
            {"ok": True, "stream": {"running": True}, "stream_config": stream_config(3600)},
            {"ok": True, "stream_config": stream_config(2880)},
            {"ok": True, "stream": {"running": True}, "stream_config": stream_config(2880)},
            {"ok": True, "stream_config": stream_config(2500)},
            {"ok": True, "stream": {"running": True}, "stream_config": stream_config(2500)},
        ])
        post = MagicMock(return_value={"ok": True, "message": "stream restarted"})

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "youtube_autotune_state.json"
            state_file.write_text(json.dumps({"entries": {}}), encoding="utf-8")
            with patch.object(app, "YOUTUBE_AUTOTUNE_STATE_FILE", state_file), patch.object(
                app, "load_youtube_profiles_config", return_value={
                    "active_profile_id": "account-a",
                    "profiles": [{
                        "id": "account-a",
                        "auto_tune_enabled": True,
                        "auto_tune_interval_seconds": 60,
                        "auto_tune_cooldown_seconds": 60,
                        "auto_tune_min_bitrate": 800,
                        "auto_tune_max_bitrate": 6000,
                    }],
                }
            ), patch.object(app, "load_nodes", return_value=[{"id": "node-a", "name": "Node A", "enabled": True}]), patch.object(
                app, "request_node_json", statuses
            ), patch.object(app, "youtube_client_for_id", return_value=client), patch.object(
                app, "post_node_json", post
            ):
                for timestamp in (10_000, 10_100, 10_200, 10_300):
                    with patch.object(app.time, "time", return_value=timestamp):
                        app.youtube_autotune_tick()
            saved = json.loads(state_file.read_text(encoding="utf-8"))

        self.assertEqual([call.args[2]["video_bitrate"] for call in post.call_args_list], [3600, 2880, 2500])
        entry = saved["entries"]["node-a:account-a:stream-a"]
        self.assertEqual(entry["episode_adjustments"], 3)
        self.assertTrue(entry["recovery_applied"])
        self.assertEqual(saved["history"][0]["outcome"], "observing")
        self.assertEqual(saved["history"][1]["changes"]["video_bitrate"], 2500)

    def test_autotune_vertical_720p_uses_same_recovery_bitrate_as_landscape(self):
        from stream_control_hub import app

        self.assertEqual(app.youtube_autotune_resolution_bitrate({"resolution": "720x1280", "fps": 30}), 2500)
        self.assertEqual(app.youtube_autotune_resolution_bitrate({"resolution": "1280x720", "fps": 30}), 2500)

    def test_autotune_uses_agent_runtime_for_starvation_decisions(self):
        from stream_control_hub import app

        config = {
            "video_bitrate": 4500,
            "audio_bitrate": 192,
            "resolution": "1280x720",
            "fps": 30,
            "preset": "superfast",
        }
        health = {
            "severity": "critical",
            "analysis": {
                "configuration_issues": [{"type": "videoIngestionStarved", "severity": "error"}],
            },
        }
        recommendation = {**config, "video_bitrate": 3600}

        overloaded, runtime, _ = app.youtube_autotune_apply_runtime(
            health,
            recommendation,
            {"cpu_count": 1, "stream": {"runtime": {"speed": 0.91, "system_cpu_percent": 88, "ffmpeg_cpu_percent": 96}}},
            config,
            {},
        )
        self.assertEqual(runtime["classification"], "encoder_overloaded")
        self.assertEqual(overloaded["video_bitrate"], 4500)
        self.assertEqual(overloaded["preset"], "ultrafast")

        healthy_config = {**config, "video_bitrate": 1179}
        healthy, runtime, _ = app.youtube_autotune_apply_runtime(
            health,
            {**healthy_config, "video_bitrate": 943},
            {
                "cpu_count": 1,
                "stream": {"runtime": {
                    "speed": 1.0,
                    "system_cpu_percent": 42,
                    "ffmpeg_cpu_percent": 70,
                    "upload_kbps": 1400,
                }},
            },
            healthy_config,
            {"youtube_recommended_bitrate": 2500},
        )
        self.assertEqual(runtime["classification"], "healthy")
        self.assertEqual(healthy["video_bitrate"], 2500)

        network_limited, runtime, _ = app.youtube_autotune_apply_runtime(
            health,
            recommendation,
            {
                "cpu_count": 1,
                "stream": {"runtime": {
                    "speed": 1.0,
                    "system_cpu_percent": 40,
                    "ffmpeg_cpu_percent": 60,
                    "upload_kbps": 500,
                }},
            },
            config,
            {},
        )
        self.assertEqual(runtime["classification"], "network_starved")
        self.assertEqual(network_limited["video_bitrate"], 4500)
        self.assertEqual(network_limited["preset"], "superfast")

    def test_agent_parses_latest_ffmpeg_speed_and_runtime_load(self):
        from stream_control_hub import headless_agent

        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "ffmpeg.log"
            log_file.write_text(
                "frame=100 speed=0.82x\rframe=200 speed=0.99x\r",
                encoding="utf-8",
            )
            state = {
                "stream_log_offset": 0,
                "stream_config": {"video_bitrate": 2500, "audio_bitrate": 128},
            }
            with patch.object(headless_agent, "DATA_DIR", Path(tmp)), patch.object(
                headless_agent.os, "cpu_count", return_value=1
            ):
                runtime = headless_agent.ffmpeg_runtime_status(
                    state,
                    [{"pid": 123, "cpu_percent": 87.5}],
                    {"current_upload_bps": 320_000},
                    64.0,
                )

        self.assertEqual(runtime["speed"], 0.99)
        self.assertTrue(runtime["speed_available"])
        self.assertEqual(runtime["ffmpeg_cpu_percent"], 87.5)
        self.assertEqual(runtime["upload_kbps"], 2560.0)


if __name__ == "__main__":
    unittest.main()
