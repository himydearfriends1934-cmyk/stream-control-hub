import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class AgentTrustTests(unittest.TestCase):
    def test_configured_tailscale_hub_is_trusted(self):
        from stream_control_hub import headless_agent

        client = headless_agent.APP.test_client()
        with patch.object(headless_agent, "CONTROL_HUB", "http://100.85.233.24:8788"), patch.object(
            headless_agent, "CONTROL_TOKEN", "configured-token"
        ), patch.object(
            headless_agent, "discover_public_origin", return_value="http://165.99.42.174:8787"
        ):
            response = client.get("/api/status", environ_base={"REMOTE_ADDR": "100.85.233.24"})

        self.assertEqual(response.status_code, 200)

    def test_other_tailnet_peer_still_needs_token(self):
        from stream_control_hub import headless_agent

        client = headless_agent.APP.test_client()
        with patch.object(headless_agent, "CONTROL_HUB", "http://100.85.233.24:8788"), patch.object(
            headless_agent, "CONTROL_TOKEN", "configured-token"
        ), patch.object(
            headless_agent, "discover_public_origin", return_value="http://165.99.42.174:8787"
        ):
            response = client.get("/api/status", environ_base={"REMOTE_ADDR": "100.118.47.126"})

        self.assertEqual(response.status_code, 403)

    def test_public_control_hub_address_is_not_implicitly_trusted(self):
        from stream_control_hub import headless_agent

        client = headless_agent.APP.test_client()
        with patch.object(headless_agent, "CONTROL_HUB", "http://203.0.113.10:8788"), patch.object(
            headless_agent, "CONTROL_TOKEN", "configured-token"
        ), patch.object(
            headless_agent, "discover_public_origin", return_value="http://165.99.42.174:8787"
        ):
            response = client.get("/api/status", environ_base={"REMOTE_ADDR": "203.0.113.10"})

        self.assertEqual(response.status_code, 403)

    def test_pair_falls_back_to_tailscale_status_when_whois_json_fails(self):
        from stream_control_hub import headless_agent

        client = headless_agent.APP.test_client()
        with patch.object(headless_agent, "CONTROL_TOKEN", "configured-token"), patch.object(
            headless_agent.shutil, "which", return_value="/usr/bin/tailscale"
        ), patch.object(
            headless_agent.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="whois json unsupported"),
        ), patch.object(
            headless_agent,
            "tailscale_status",
            return_value={
                "ok": True,
                "peers": [{"tailscale_ips": ["100.118.47.126"], "online": True}],
            },
        ):
            response = client.post("/pair", environ_base={"REMOTE_ADDR": "100.118.47.126"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["token"], "configured-token")

    def test_pair_rejects_tailnet_ip_missing_from_status_fallback(self):
        from stream_control_hub import headless_agent

        client = headless_agent.APP.test_client()
        with patch.object(headless_agent, "CONTROL_TOKEN", "configured-token"), patch.object(
            headless_agent.shutil, "which", return_value="/usr/bin/tailscale"
        ), patch.object(
            headless_agent.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="whois json unsupported"),
        ), patch.object(
            headless_agent,
            "tailscale_status",
            return_value={
                "ok": True,
                "peers": [{"tailscale_ips": ["100.118.47.127"], "online": True}],
            },
        ):
            response = client.post("/pair", environ_base={"REMOTE_ADDR": "100.118.47.126"})

        self.assertEqual(response.status_code, 403)

    def test_unbound_pair_binds_requesting_hub(self):
        from stream_control_hub import headless_agent

        client = headless_agent.APP.test_client()
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".agent.env"
            with patch.object(headless_agent, "CONTROL_HUB", ""), patch.object(
                headless_agent, "CONTROL_TOKEN", "configured-token"
            ), patch.object(
                headless_agent, "AGENT_ENV_FILE", env_file
            ), patch.object(
                headless_agent, "request_is_verified_tailscale_peer", return_value=True
            ):
                response = client.post(
                    "/pair",
                    json={"hub_url": "http://100.118.47.1:8788"},
                    environ_base={"REMOTE_ADDR": "100.118.47.1"},
                )

            saved_env = env_file.read_text(encoding="utf-8")

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["bound_control_hub"], "http://100.118.47.1:8788")
        self.assertFalse(data["token_rotated"])
        self.assertIn("STREAM_AGENT_CONTROL_HUB=http://100.118.47.1:8788", saved_env)

    def test_bound_pair_requires_confirmation_before_rebinding(self):
        from stream_control_hub import headless_agent

        client = headless_agent.APP.test_client()
        with patch.object(headless_agent, "CONTROL_HUB", "http://100.64.0.1:8788"), patch.object(
            headless_agent, "CONTROL_TOKEN", "configured-token"
        ), patch.object(
            headless_agent, "request_is_verified_tailscale_peer", return_value=True
        ):
            response = client.post(
                "/pair",
                json={"hub_url": "http://100.118.47.1:8788"},
                environ_base={"REMOTE_ADDR": "100.118.47.1"},
            )

        data = response.get_json()
        self.assertEqual(response.status_code, 409)
        self.assertTrue(data["confirmation_required"])
        self.assertEqual(data["current_control_hub"], "http://100.64.0.1:8788")
        self.assertNotIn("token", data)

    def test_confirmed_pair_rebinds_to_requesting_hub(self):
        from stream_control_hub import headless_agent

        client = headless_agent.APP.test_client()
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".agent.env"
            env_file.write_text("STREAM_AGENT_CONTROL_HUB=http://100.64.0.1:8788\n", encoding="utf-8")
            with patch.object(headless_agent, "CONTROL_HUB", "http://100.64.0.1:8788"), patch.object(
                headless_agent, "CONTROL_TOKEN", "configured-token"
            ), patch.object(
                headless_agent, "AGENT_ENV_FILE", env_file
            ), patch.object(
                headless_agent, "request_is_verified_tailscale_peer", return_value=True
            ), patch.object(
                headless_agent.secrets, "token_urlsafe", return_value="rotated-token"
            ):
                response = client.post(
                    "/pair",
                    json={"hub_url": "http://100.118.47.1:8788", "confirm_rebind": True},
                    environ_base={"REMOTE_ADDR": "100.118.47.1"},
                )

            saved_env = env_file.read_text(encoding="utf-8")

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["rebound"])
        self.assertTrue(data["token_rotated"])
        self.assertEqual(data["token"], "rotated-token")
        self.assertEqual(data["previous_control_hub"], "http://100.64.0.1:8788")
        self.assertEqual(data["bound_control_hub"], "http://100.118.47.1:8788")
        self.assertIn("STREAM_AGENT_CONTROL_HUB=http://100.118.47.1:8788", saved_env)
        self.assertIn("STREAM_AGENT_CONTROL_TOKEN=rotated-token", saved_env)


class HubAgentConnectionTests(unittest.TestCase):
    def test_tailscale_discovery_marks_agent_and_hub_without_credentials(self):
        from stream_control_hub import app

        class FakeResponse:
            def __init__(self, ok, payload):
                self.ok = ok
                self._payload = payload

            def json(self):
                return self._payload

        responses = {
            "http://100.118.47.120:8787/": FakeResponse(False, {}),
            "http://100.118.47.120:8788/api/role-status": FakeResponse(
                True,
                {"ok": True, "roles": {"hub": {"enabled": True}}},
            ),
            "http://100.118.47.126:8787/": FakeResponse(
                True,
                {"ok": True, "name": "LIGHTCONE", "mode": "headless-agent"},
            ),
            "http://100.118.47.126:8788/api/role-status": FakeResponse(
                True,
                {"ok": True, "roles": {"hub": {"enabled": False}}},
            ),
            "http://100.118.47.127:8787/": FakeResponse(False, {}),
            "http://100.118.47.127:8788/api/role-status": FakeResponse(
                True,
                {"ok": True, "roles": {"hub": {"enabled": True, "url": "http://100.118.47.127:8788"}}},
            ),
        }

        def fake_get(url, *, timeout):
            return responses[url]

        raw = {
            "ok": True,
            "self": {"host_name": "hub", "tailscale_ips": ["100.118.47.120"], "online": True},
            "peers": [
                {"host_name": "agent", "tailscale_ips": ["100.118.47.126"], "online": True},
                {"host_name": "hub-2", "tailscale_ips": ["100.118.47.127"], "online": True},
            ],
        }
        with patch.object(app.requests, "get", side_effect=fake_get):
            enriched = app.enrich_tailscale_status(raw)

        by_ip = {item["tailscale_ip"]: item for item in enriched["agent_nodes"]}
        self.assertTrue(by_ip["100.118.47.126"]["agent_installed"])
        self.assertFalse(by_ip["100.118.47.126"]["hub_enabled"])
        self.assertEqual(by_ip["100.118.47.126"]["agent_name"], "LIGHTCONE")
        self.assertTrue(by_ip["100.118.47.127"]["hub_enabled"])
        self.assertEqual(by_ip["100.118.47.127"]["hub_url"], "http://100.118.47.127:8788")
        self.assertNotIn("token", json.dumps(enriched).lower())

    def test_tailscale_status_endpoint_returns_discovered_agent_nodes(self):
        from stream_control_hub import app

        raw = {
            "ok": True,
            "self": {"host_name": "hub", "tailscale_ips": ["100.118.47.120"], "online": True},
            "peers": [],
        }
        with patch.object(app, "tailscale_status", return_value=raw), patch.object(
            app, "probe_tailscale_node", return_value={"tailscale_ip": "100.118.47.120", "self": True}
        ):
            response = app.APP.test_client().get("/api/tailscale/status")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["agent_nodes"], [{"tailscale_ip": "100.118.47.120", "self": True}])

    def test_existing_dual_role_record_recovers_agent_and_hub_urls(self):
        from stream_control_hub import app

        self.assertEqual(
            app.node_role_urls({
                "id": "dual-role",
                "base_url": "http://100.118.47.126:8788",
            }),
            {
                "agent": "http://100.118.47.126:8787",
                "hub": "http://100.118.47.126:8788",
            },
        )
        self.assertEqual(
            app.node_role_urls({
                "id": "legacy-dual-role",
                "hub_url": "http://100.118.47.127",
            }),
            {
                "agent": "http://100.118.47.127:8787",
                "hub": "http://100.118.47.127:8788",
            },
        )

    def test_current_hub_tailscale_address_is_seen_as_online(self):
        from stream_control_hub import app

        with patch.object(
            app,
            "tailscale_status",
            return_value={
                "ok": True,
                "self": {
                    "host_name": "current-hub",
                    "dns_name": "current-hub.tailnet",
                    "tailscale_ips": ["100.64.0.1"],
                },
                "peers": [],
            },
        ):
            peer = app.online_tailscale_peer_for_ip("100.64.0.1")

        self.assertTrue(peer["self"])
        self.assertTrue(peer["online"])

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
                app, "online_tailscale_peer_for_ip", return_value={"online": True}
            ), patch.object(
                app, "local_tailscale_hub_url", return_value="http://100.64.0.1:8788"
            ), patch.object(
                app, "pair_tailscale_agent", return_value={"ok": True, "token": "paired-token"}
            ), patch.object(app, "request_node_json", return_value=status):
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
                app, "online_tailscale_peer_for_ip", return_value={"online": True}
            ), patch.object(
                app, "local_tailscale_hub_url", return_value="http://100.64.0.1:8788"
            ), patch.object(
                app, "pair_tailscale_agent", return_value={"ok": True, "token": "paired-token"}
            ), patch.object(app, "request_node_json", return_value={"ok": True, "hostname": "new-agent-host"}):
                response = app.APP.test_client().post(
                    "/api/tailscale/connect-existing-ip",
                    json={"tailscale_ip": "100.118.47.126"},
                )

            saved = json.loads(nodes_file.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["created"])
        self.assertEqual(len(saved), 1)

    def test_ip_only_connection_uses_supplied_token_when_pairing_fails(self):
        from stream_control_hub import app

        captured = {}

        def fake_request_node_json(node, path, *, timeout=6):
            captured["node"] = dict(node)
            captured["path"] = path
            return {
                "ok": True,
                "hostname": "recovered-agent-host",
                "platform": "Linux",
                "agent": {"name": "RECOVERED"},
            }

        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text("[]", encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "online_tailscale_peer_for_ip", return_value={"online": True}
            ), patch.object(
                app, "local_tailscale_hub_url", return_value="http://100.64.0.1:8788"
            ), patch.object(
                app, "pair_tailscale_agent", return_value={"ok": False, "status_code": 403, "message": "pairing rejected"}
            ), patch.object(app, "request_node_json", side_effect=fake_request_node_json):
                response = app.APP.test_client().post(
                    "/api/tailscale/connect-existing-ip",
                    json={"tailscale_ip": "100.118.47.126", "token": "manual-token"},
                )

            saved = json.loads(nodes_file.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["path"], "/api/status")
        self.assertEqual(captured["node"]["token"], "manual-token")
        self.assertEqual(saved[0]["token"], "manual-token")
        self.assertEqual(response.get_json()["paired_via"], "supplied-token")

    def test_ip_only_connection_returns_rebind_confirmation(self):
        from stream_control_hub import app

        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text("[]", encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "online_tailscale_peer_for_ip", return_value={"online": True}
            ), patch.object(
                app, "local_tailscale_hub_url", return_value="http://100.64.0.2:8788"
            ), patch.object(
                app,
                "pair_tailscale_agent",
                return_value={
                    "ok": False,
                    "status_code": 409,
                    "confirmation_required": True,
                    "action": "confirm_rebind",
                    "current_control_hub": "http://100.64.0.1:8788",
                    "requested_control_hub": "http://100.64.0.2:8788",
                    "name": "BOUND-AGENT",
                },
            ):
                response = app.APP.test_client().post(
                    "/api/tailscale/connect-existing-ip",
                    json={"tailscale_ip": "100.118.47.126"},
                )

            saved = json.loads(nodes_file.read_text(encoding="utf-8"))

        data = response.get_json()
        self.assertEqual(response.status_code, 409)
        self.assertTrue(data["confirmation_required"])
        self.assertEqual(data["current_control_hub"], "http://100.64.0.1:8788")
        self.assertEqual(data["requested_control_hub"], "http://100.64.0.2:8788")
        self.assertEqual(saved, [])

    def test_ip_only_connection_confirms_rebind_and_saves_rotated_token(self):
        from stream_control_hub import app

        captured = {}

        def fake_pair(base_url, *, hub_url="", confirm_rebind=False, timeout=12):
            captured["base_url"] = base_url
            captured["hub_url"] = hub_url
            captured["confirm_rebind"] = confirm_rebind
            return {
                "ok": True,
                "token": "rotated-token",
                "token_rotated": True,
                "name": "REBIND-AGENT",
                "bound_control_hub": hub_url,
            }

        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text("[]", encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "online_tailscale_peer_for_ip", return_value={"online": True}
            ), patch.object(
                app, "local_tailscale_hub_url", return_value="http://100.64.0.2:8788"
            ), patch.object(
                app, "pair_tailscale_agent", side_effect=fake_pair
            ), patch.object(
                app,
                "request_node_json",
                return_value={"ok": True, "hostname": "rebind-host", "agent": {"name": "REBIND-AGENT"}},
            ):
                response = app.APP.test_client().post(
                    "/api/tailscale/connect-existing-ip",
                    json={"tailscale_ip": "100.118.47.126", "confirm_rebind": True},
                )

            saved = json.loads(nodes_file.read_text(encoding="utf-8"))

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["base_url"], "http://100.118.47.126:8787")
        self.assertEqual(captured["hub_url"], "http://100.64.0.2:8788")
        self.assertTrue(captured["confirm_rebind"])
        self.assertTrue(data["token_rotated"])
        self.assertEqual(saved[0]["token"], "rotated-token")

    def test_replace_connection_clears_stale_hub_metadata(self):
        from stream_control_hub import app

        existing = [{
            "id": "agent-a",
            "name": "Agent A",
            "base_url": "http://100.118.47.126:8787",
            "tailscale_ip": "100.118.47.126",
            "enabled": True,
            "public_base_url": "http://old.example",
            "hub_url": "http://100.64.0.1:8788",
            "control_hub_url": "http://100.64.0.1:8788",
            "role_hints": {"hub": {"activation_pending": True}},
            "offline_since": "2026-01-01T00:00:00Z",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps(existing), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "online_tailscale_peer_for_ip", return_value={"online": True}
            ), patch.object(
                app, "local_tailscale_hub_url", return_value="http://100.64.0.2:8788"
            ), patch.object(
                app, "pair_tailscale_agent", return_value={"ok": True, "token": "new-token"}
            ), patch.object(
                app, "request_node_json", return_value={"ok": True, "hostname": "agent-a"}
            ):
                response = app.APP.test_client().post(
                    "/api/tailscale/connect-existing-ip",
                    json={
                        "tailscale_ip": "100.118.47.126",
                        "replace_connection": True,
                        "confirm_rebind": True,
                    },
                )
            saved = json.loads(nodes_file.read_text(encoding="utf-8"))[0]

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["connection_replaced"])
        self.assertEqual(saved["control_hub_url"], "http://100.64.0.2:8788")
        self.assertNotIn("hub_url", saved)
        self.assertNotIn("public_base_url", saved)
        self.assertNotIn("role_hints", saved)
        self.assertNotIn("offline_since", saved)

    def test_ip_only_connection_recognizes_hub_only_node(self):
        from stream_control_hub import app

        existing = [{
            "id": "tokyo",
            "name": "TOKYO",
            "base_url": "http://100.98.19.85:8787",
            "enabled": True,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps(existing), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "online_tailscale_peer_for_ip", return_value={"online": True}
            ), patch.object(
                app, "local_tailscale_hub_url", return_value="http://100.64.0.1:8788"
            ), patch.object(
                app, "pair_tailscale_agent", return_value={"ok": False, "message": "connection refused"}
            ), patch.object(
                app,
                "request_hub_status_url",
                return_value={"ok": True, "roles": {"hub": {"enabled": True, "version": "abc1234"}}},
            ):
                response = app.APP.test_client().post(
                    "/api/tailscale/connect-existing-ip",
                    json={"tailscale_ip": "100.98.19.85"},
                )
            saved = json.loads(nodes_file.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["hub_only"])
        self.assertIn("Hub 在线", data["message"])
        self.assertNotIn("??", data["message"])
        self.assertEqual(data["node_id"], "tokyo")
        self.assertEqual(data["hub_url"], "http://100.98.19.85:8788")
        self.assertEqual(saved[0]["hub_url"], "http://100.98.19.85:8788")
        self.assertTrue(saved[0]["hub_only"])


if __name__ == "__main__":
    unittest.main()
