import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class OfflineNodeRetentionTests(unittest.TestCase):
    def write_nodes(self, path: Path, nodes: list[dict]) -> None:
        path.write_text(json.dumps(nodes), encoding="utf-8")

    def read_nodes(self, path: Path) -> list[dict]:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_first_offline_probe_records_start_without_deleting(self):
        from stream_control_hub import app

        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            self.write_nodes(nodes_file, [{"id": "node-1", "enabled": True}])
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "request_node_json", return_value={"ok": False, "message": "offline"}
            ), patch.object(app, "OFFLINE_NODE_RETENTION_SECONDS", 86400):
                removed = app.prune_offline_nodes(now=1_000_000)

            self.assertEqual(removed, [])
            saved = self.read_nodes(nodes_file)
            self.assertEqual(saved[0]["offline_since"], "1970-01-12T13:46:40+00:00")

    def test_expired_offline_node_is_removed(self):
        from stream_control_hub import app

        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            self.write_nodes(nodes_file, [{
                "id": "node-1",
                "enabled": True,
                "offline_since": "2024-01-01T00:00:00+00:00",
            }])
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "request_node_json", return_value={"ok": False, "message": "offline"}
            ), patch.object(app, "OFFLINE_NODE_RETENTION_SECONDS", 86400):
                removed = app.prune_offline_nodes(now=1_704_153_600)

            self.assertEqual(removed, ["node-1"])
            self.assertEqual(self.read_nodes(nodes_file), [])

    def test_online_node_clears_offline_marker(self):
        from stream_control_hub import app

        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            self.write_nodes(nodes_file, [{
                "id": "node-1",
                "enabled": True,
                "offline_since": "2024-01-01T00:00:00+00:00",
            }])
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "request_node_json", return_value={"ok": True}
            ):
                removed = app.prune_offline_nodes(now=1_704_153_600)

            self.assertEqual(removed, [])
            saved = self.read_nodes(nodes_file)
            self.assertNotIn("offline_since", saved[0])
            self.assertEqual(saved[0]["last_online_at"], "2024-01-02T00:00:00+00:00")

    def test_online_hub_role_prevents_agent_record_removal(self):
        from stream_control_hub import app

        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            self.write_nodes(nodes_file, [{
                "id": "node-1",
                "enabled": True,
                "hub_url": "http://100.64.0.10:8788",
                "offline_since": "2024-01-01T00:00:00+00:00",
            }])
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "request_node_json", return_value={"ok": False, "message": "agent offline"}
            ), patch.object(
                app, "request_hub_role_status", return_value={"ok": True, "enabled": True}
            ), patch.object(app, "OFFLINE_NODE_RETENTION_SECONDS", 86400):
                removed = app.prune_offline_nodes(now=1_704_153_600)

            self.assertEqual(removed, [])
            self.assertEqual(len(self.read_nodes(nodes_file)), 1)


if __name__ == "__main__":
    unittest.main()
