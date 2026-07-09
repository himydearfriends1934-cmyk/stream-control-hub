import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            nodes_file.write_text(json.dumps([node]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
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

        self.assertEqual(response.status_code, 200)
        forwarded = post.call_args.args[2]
        self.assertEqual(forwarded["youtube_stream_id"], "stream-1")
        self.assertEqual(forwarded["youtube_ingestion_url"], "rtmp://example.test/live2/private-stream-name")
        self.assertEqual(forwarded["stream_key"], "")

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

    def test_youtube_health_recommendation_reduces_high_bitrate(self):
        result = youtube_health_recommendation(
            {
                "stream_status": "active",
                "health_status": "bad",
                "configuration_issues": [{"type": "videoBitrateIsHigh", "description": "bitrate is high"}],
            },
            {"video_bitrate": 6000, "fps": 60, "resolution": "1920x1080"},
        )

        self.assertEqual(result["severity"], "warning")
        self.assertLess(result["recommendation"]["video_bitrate"], 6000)


if __name__ == "__main__":
    unittest.main()
