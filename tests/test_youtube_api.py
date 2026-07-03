import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stream_control_hub.youtube_api import YouTubeAPIClient


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

    def test_hub_forwards_only_youtube_stream_id(self):
        from stream_control_hub import app

        payload = app.stream_payload_for_node({
            "video_path": "video.mp4",
            "stream_output_mode": "youtube_api",
            "youtube_stream_id": "stream-1",
        })

        self.assertEqual(payload["youtube_stream_id"], "stream-1")
        self.assertEqual(payload["stream_key"], "")

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

    def test_hub_forwards_youtube_config_to_agent(self):
        from stream_control_hub import app

        node = {"id": "node-a", "base_url": "http://100.64.0.10:8787", "enabled": True}
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps([node]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app,
                "post_node_json",
                return_value={"ok": True, "configured": True, "authorized": False},
            ) as post:
                response = app.APP.test_client().post(
                    "/api/nodes/youtube/config",
                    json={"node_id": "node-a", "client_id": "client-id", "client_secret": "client-secret"},
                )

        self.assertEqual(response.status_code, 200)
        post.assert_called_once_with(
            node,
            "/api/youtube/config",
            {"client_id": "client-id", "client_secret": "client-secret"},
            timeout=30,
        )


if __name__ == "__main__":
    unittest.main()
