import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from io import BytesIO

import requests


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class PublicOriginDiscoveryTests(unittest.TestCase):
    def test_public_origin_accepts_only_global_ipv4(self):
        from stream_control_hub import headless_agent

        self.assertEqual(
            headless_agent.public_origin_from_ip("165.99.42.174"),
            "http://165.99.42.174:8787",
        )
        self.assertEqual(headless_agent.public_origin_from_ip("10.0.145.3"), "")
        self.assertEqual(headless_agent.public_origin_from_ip("<html>not an ip</html>"), "")
        self.assertEqual(headless_agent.normalize_public_origin("http://165.99.42.174:bad"), "")

    def test_discovery_falls_back_to_ifconfig_me(self):
        from stream_control_hub import headless_agent

        responses = [requests.RequestException("first provider failed"), FakeResponse("165.99.42.174\n")]
        with patch.object(headless_agent, "PUBLIC_ORIGIN", ""), patch.dict(
            headless_agent.PUBLIC_ORIGIN_CACHE, {"value": "", "checked_at": 0.0}, clear=True
        ), patch.object(headless_agent.requests, "get", side_effect=responses) as request_get:
            origin = headless_agent.discover_public_origin(force=True)

        self.assertEqual(origin, "http://165.99.42.174:8787")
        self.assertEqual(request_get.call_count, 2)

    def test_stale_upload_state_is_pruned_only_when_part_file_is_missing(self):
        from stream_control_hub import headless_agent

        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp) / "media"
            media_dir.mkdir()
            state = {
                "active_uploads": {
                    "missing-upload": {"filename": "missing.mp4", "updated_at": 100},
                    "resumable-upload": {"filename": "kept.mp4", "updated_at": 100},
                }
            }
            (media_dir / ".resumable-upload.kept.mp4.part").write_bytes(b"partial")
            with patch.object(headless_agent, "MEDIA_DIR", media_dir), patch.object(
                headless_agent, "UPLOAD_STALE_STATE_SECONDS", 3600
            ):
                removed = headless_agent.prune_stale_upload_state(state, now=7200)

        self.assertEqual(removed, 1)
        self.assertNotIn("missing-upload", state["active_uploads"])
        self.assertIn("resumable-upload", state["active_uploads"])


class HubPublicUploadRouteTests(unittest.TestCase):
    def test_upload_target_prefers_agent_discovered_public_origin(self):
        from stream_control_hub import app

        node = {
            "id": "LIGHTCONE-NEW",
            "name": "LIGHTCONE-NEW",
            "base_url": "http://100.118.47.126:8787",
            "upload_base_url": "http://165.99.42.175:8787",
            "enabled": True,
            "token": "agent-token",
        }
        public_status = {
            "ok": True,
            "supported": True,
            "public_origin": "http://165.99.42.174:8787",
            "restrict_public_to_upload": True,
            "ticket_required": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps([node]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app,
                "request_node_upload_ticket",
                return_value={"ok": True, "ticket": "short-lived-ticket", "expires_in": 3600},
            ), patch.object(app, "request_node_json", return_value=public_status), patch.object(
                app, "probe_upload_route", return_value={"ok": True, "rate_label": "1 MB/s"}
            ):
                response = app.APP.test_client().post(
                    "/api/nodes/upload-target",
                    json={
                        "node_id": "LIGHTCONE-NEW",
                        "upload_id": "upload-1",
                        "filename": "video.mp4",
                        "total_size": 1024,
                    },
                )

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["candidates"][0]["url"], "http://165.99.42.174:8787")
        self.assertEqual(data["candidates"][0]["label"], "公网直连")
        self.assertEqual(data["candidates"][1]["url"], "http://165.99.42.175:8787")
        self.assertEqual(data["headers"]["X-Upload-Ticket"], "short-lived-ticket")

    def test_hub_media_push_uses_upload_ticket_for_restricted_public_route(self):
        from stream_control_hub import app

        node = {
            "id": "LIGHTCONE-NEW",
            "base_url": "http://100.118.47.126:8787",
            "token": "agent-token",
        }
        public_status = {
            "ok": True,
            "supported": True,
            "public_origin": "http://165.99.42.174:8787",
            "restrict_public_to_upload": True,
            "ticket_required": True,
        }
        captured_route = {}

        def fake_probe(route):
            captured_route.update(route)
            return {"ok": True, "rate_label": "1 MB/s"}

        with patch.object(app, "request_node_json", return_value=public_status), patch.object(
            app,
            "request_node_upload_ticket",
            return_value={"ok": True, "ticket": "short-lived-ticket", "expires_in": 3600},
        ) as ticket_request, patch.object(app, "probe_upload_route", side_effect=fake_probe):
            route = app.select_node_upload_route(
                node,
                upload_id="hub-upload-1",
                filename="video.mp4",
                total_size=1024,
            )

        ticket_request.assert_called_once_with(
            node,
            upload_id="hub-upload-1",
            filename="video.mp4",
            total_size=1024,
        )
        self.assertEqual(route["route"], "public-direct")
        self.assertEqual(route["upload_base_url"], "http://165.99.42.174:8787")
        self.assertEqual(captured_route["headers"], {"X-Upload-Ticket": "short-lived-ticket"})
        self.assertNotIn("X-Control-Token", captured_route["headers"])


class AgentUploadIntegrityTests(unittest.TestCase):
    def agent_paths(self, module, root):
        data_dir = Path(root) / "agent_data"
        media_dir = data_dir / "media"
        media_dir.mkdir(parents=True)
        return (
            patch.object(module, "DATA_DIR", data_dir),
            patch.object(module, "MEDIA_DIR", media_dir),
            patch.object(module, "STATE_FILE", data_dir / "state.json"),
            patch.object(module, "MIN_FREE_AFTER_UPLOAD_BYTES", 0),
        )

    def test_upload_rejects_non_sequential_chunk_offset(self):
        from stream_control_hub import headless_agent

        with tempfile.TemporaryDirectory() as tmp:
            patches = self.agent_paths(headless_agent, tmp)
            with patches[0], patches[1], patches[2], patches[3], patch.object(headless_agent, "CONTROL_TOKEN", ""):
                response = headless_agent.APP.test_client().post(
                    "/api/upload-chunk",
                    data={
                        "upload_id": "upload-1",
                        "filename": "video.mp4",
                        "chunk_index": "1",
                        "total_chunks": "2",
                        "offset": "4",
                        "total_size": "8",
                        "chunk_size": "4",
                        "chunk": (BytesIO(b"bbbb"), "video.mp4"),
                    },
                    content_type="multipart/form-data",
                )

        self.assertEqual(response.status_code, 409)
        self.assertIn("previous upload chunk", response.get_json()["message"])

    def test_upload_rejects_wrong_chunk_size(self):
        from stream_control_hub import headless_agent

        with tempfile.TemporaryDirectory() as tmp:
            patches = self.agent_paths(headless_agent, tmp)
            with patches[0], patches[1], patches[2], patches[3], patch.object(headless_agent, "CONTROL_TOKEN", ""):
                response = headless_agent.APP.test_client().post(
                    "/api/upload-chunk",
                    data={
                        "upload_id": "upload-1",
                        "filename": "video.mp4",
                        "chunk_index": "0",
                        "total_chunks": "2",
                        "offset": "0",
                        "total_size": "8",
                        "chunk_size": "4",
                        "chunk": (BytesIO(b"bb"), "video.mp4"),
                    },
                    content_type="multipart/form-data",
                )

        self.assertEqual(response.status_code, 400)
        self.assertIn("chunk size", response.get_json()["message"])

    def test_cors_headers_are_limited_to_upload_paths(self):
        from stream_control_hub import headless_agent

        client = headless_agent.APP.test_client()
        with patch.object(headless_agent, "CONTROL_TOKEN", ""):
            upload = client.options("/api/upload-chunk", headers={"Origin": "https://example.test"})
            status = client.get("/api/status", headers={"Origin": "https://example.test"})

        self.assertEqual(upload.headers.get("Access-Control-Allow-Origin"), "*")
        self.assertIsNone(status.headers.get("Access-Control-Allow-Origin"))


if __name__ == "__main__":
    unittest.main()
