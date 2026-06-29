import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class AgentTrustTests(unittest.TestCase):
    def test_configured_tailscale_hub_is_trusted(self):
        from stream_control_hub import headless_agent

        client = headless_agent.APP.test_client()
        with patch.object(headless_agent, "CONTROL_HUB", "http://100.85.233.24:8788"), patch.object(
            headless_agent, "CONTROL_TOKEN", "configured-token"
        ):
            response = client.get("/api/status", environ_base={"REMOTE_ADDR": "100.85.233.24"})

        self.assertEqual(response.status_code, 200)

    def test_other_tailnet_peer_still_needs_token(self):
        from stream_control_hub import headless_agent

        client = headless_agent.APP.test_client()
        with patch.object(headless_agent, "CONTROL_HUB", "http://100.85.233.24:8788"), patch.object(
            headless_agent, "CONTROL_TOKEN", "configured-token"
        ):
            response = client.get("/api/status", environ_base={"REMOTE_ADDR": "100.118.47.126"})

        self.assertEqual(response.status_code, 403)

    def test_public_control_hub_address_is_not_implicitly_trusted(self):
        from stream_control_hub import headless_agent

        client = headless_agent.APP.test_client()
        with patch.object(headless_agent, "CONTROL_HUB", "http://203.0.113.10:8788"), patch.object(
            headless_agent, "CONTROL_TOKEN", "configured-token"
        ):
            response = client.get("/api/status", environ_base={"REMOTE_ADDR": "203.0.113.10"})

        self.assertEqual(response.status_code, 403)


class HubAgentConnectionTests(unittest.TestCase):
    def test_ip_only_connection_creates_node(self):
        from stream_control_hub import app

        status = {
            "ok": True,
            "hostname": "new-agent-host",
            "platform": "Linux",
            "agent": {"name": "LIGHTCONE-NEW", "mode": "headless-agent"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text("[]", encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "request_node_json", return_value=status
            ):
                response = app.APP.test_client().post(
                    "/api/tailscale/connect-existing-ip",
                    json={"tailscale_ip": "100.118.47.126"},
                )

            saved = json.loads(nodes_file.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["created"])
        self.assertEqual(saved[0]["id"], "agent-100-118-47-126")
        self.assertEqual(saved[0]["name"], "LIGHTCONE-NEW")
        self.assertEqual(saved[0]["base_url"], "http://100.118.47.126:8787")

    def test_reconnecting_same_ip_updates_generated_node(self):
        from stream_control_hub import app

        existing = [{
            "id": "agent-100-118-47-126",
            "name": "LIGHTCONE-NEW",
            "base_url": "http://100.118.47.126:8787",
            "enabled": True,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps(existing), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "request_node_json", return_value={"ok": True, "hostname": "new-agent-host"}
            ):
                response = app.APP.test_client().post(
                    "/api/tailscale/connect-existing-ip",
                    json={"tailscale_ip": "100.118.47.126"},
                )

            saved = json.loads(nodes_file.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["created"])
        self.assertEqual(len(saved), 1)


if __name__ == "__main__":
    unittest.main()
