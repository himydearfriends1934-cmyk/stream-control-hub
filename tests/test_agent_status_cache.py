import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class AgentStatusCacheTests(unittest.TestCase):
    def test_nodes_api_uses_fresh_cache_without_probing_agents(self):
        from stream_control_hub import app

        node = {"id": "cached-node", "name": "Cached", "base_url": "http://192.0.2.10:8787", "enabled": True}
        health = {"ok": True, "agent": {"version": "cached-version"}, "stream": {"running": True}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes_file = root / "nodes.json"
            cache_file = root / "agent-status-cache.json"
            nodes_file.write_text(json.dumps([node]), encoding="utf-8")
            now = time.time()
            cache_file.write_text(json.dumps({
                "version": 1,
                "updated_at": "2026-09-02T00:00:00+00:00",
                "updated_at_epoch": now,
                "nodes": {
                    "cached-node": {
                        "node_id": "cached-node",
                        "checked_at": "2026-09-02T00:00:00+00:00",
                        "checked_at_epoch": now,
                        "ok": True,
                        "health": health,
                        "hub_role": {"ok": False, "enabled": False},
                        "error": "",
                    },
                },
            }), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "AGENT_STATUS_CACHE_FILE", cache_file
            ), patch.object(
                app, "prune_local_hub_node_records", side_effect=lambda nodes: (nodes, [])
            ), patch.object(
                app, "request_node_status", side_effect=AssertionError("fresh cache should avoid Agent probes")
            ), patch.object(
                app, "request_hub_role_status", side_effect=AssertionError("fresh cache should avoid Hub probes")
            ):
                response = app.APP.test_client().get("/api/nodes")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()[0]["health"]["agent"]["version"], "cached-version")
        self.assertEqual(response.get_json()[0]["status_meta"]["source"], "cache")
        self.assertEqual(response.headers["X-Node-Status-Source"], "cache")
        self.assertEqual(response.headers["X-Node-Status-Refresh-Running"], "0")

    def test_nodes_api_returns_pending_snapshot_when_cache_is_missing(self):
        from stream_control_hub import app

        node = {"id": "new-node", "name": "New", "base_url": "http://192.0.2.11:8787", "enabled": True}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes_file = root / "nodes.json"
            cache_file = root / "agent-status-cache.json"
            nodes_file.write_text(json.dumps([node]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "AGENT_STATUS_CACHE_FILE", cache_file
            ), patch.object(
                app, "prune_local_hub_node_records", side_effect=lambda nodes: (nodes, [])
            ), patch.object(app, "schedule_agent_status_refresh", return_value=True) as schedule:
                response = app.APP.test_client().get("/api/nodes")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()[0]
        self.assertFalse(data["health"]["ok"])
        self.assertIn("读取中", data["health"]["message"])
        self.assertEqual(data["status_meta"]["source"], "pending")
        self.assertEqual(response.headers["X-Node-Status-Source"], "pending")
        schedule.assert_called_once()

    def test_refresh_probes_nodes_concurrently_and_records_each_result(self):
        from stream_control_hub import app

        nodes = [
            {"id": "node-a", "base_url": "http://192.0.2.20:8787", "enabled": True},
            {"id": "node-b", "base_url": "http://192.0.2.21:8787", "enabled": True},
            {"id": "node-c", "base_url": "http://192.0.2.22:8787", "enabled": True},
        ]
        barrier = threading.Barrier(3, timeout=2)

        def fake_status(node, *, timeout):
            barrier.wait()
            return {"ok": True, "agent": {"version": node["id"]}}

        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "agent-status-cache.json"
            with patch.object(app, "AGENT_STATUS_CACHE_FILE", cache_file), patch.object(
                app, "AGENT_STATUS_REFRESH_MAX_WORKERS", 3
            ), patch.object(app, "request_node_status", side_effect=fake_status), patch.object(
                app, "request_hub_role_status", return_value={"ok": False, "enabled": False}
            ):
                payload = app.refresh_agent_status_cache(nodes)

            saved = json.loads(cache_file.read_text(encoding="utf-8"))

        self.assertEqual(set(payload["nodes"]), {"node-a", "node-b", "node-c"})
        self.assertEqual(set(saved["nodes"]), {"node-a", "node-b", "node-c"})
        self.assertEqual(
            {entry["health"]["agent"]["version"] for entry in saved["nodes"].values()},
            {"node-a", "node-b", "node-c"},
        )

    def test_status_cache_does_not_persist_credentials(self):
        from stream_control_hub import app

        node = {"id": "secret-node", "base_url": "http://192.0.2.30:8787", "enabled": True}
        health = {
            "ok": True,
            "token": "agent-token-value",
            "stream_config": {
                "stream_key": "stream-key-value",
                "access_token": "access-token-value",
                "youtube_ingestion_url": "rtmp://example.invalid/ingestion-value",
                "has_stream_key": True,
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "agent-status-cache.json"
            with patch.object(app, "AGENT_STATUS_CACHE_FILE", cache_file), patch.object(
                app, "request_node_status", return_value=health
            ), patch.object(
                app, "request_hub_role_status", return_value={"ok": False, "enabled": False}
            ):
                app.refresh_agent_status_cache([node])
            text = cache_file.read_text(encoding="utf-8")
            saved = json.loads(text)

        self.assertNotIn("agent-token-value", text)
        self.assertNotIn("stream-key-value", text)
        self.assertNotIn("access-token-value", text)
        self.assertNotIn("ingestion-value", text)
        self.assertTrue(saved["nodes"]["secret-node"]["health"]["stream_config"]["has_stream_key"])


if __name__ == "__main__":
    unittest.main()
