import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class HubPublicUploadRouteTests(unittest.TestCase):
    def test_upload_target_prefers_agent_discovered_public_origin(self):
        from stream_control_hub import app

        node = {
            "id": "LIGHTCONE-NEW",
            "name": "LIGHTCONE-NEW",
            "base_url": "http://100.118.47.126:8787",
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
            ), patch.object(app, "request_node_json", return_value=public_status):
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
        self.assertEqual(data["candidates"][1]["url"], "http://100.118.47.126:8787")
        self.assertEqual(data["headers"]["X-Upload-Ticket"], "short-lived-ticket")


if __name__ == "__main__":
    unittest.main()
