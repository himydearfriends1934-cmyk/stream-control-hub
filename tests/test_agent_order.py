import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class AgentOrderTests(unittest.TestCase):
    def test_agent_order_settings_are_normalized_and_persisted(self):
        from stream_control_hub import app

        nodes = [
            {"id": "cloudnium3", "name": "CLOUDNIUM3", "enabled": True},
            {"id": "cloudnium4", "name": "CLOUDNIUM4", "enabled": True},
            {"id": "cloudnium2", "name": "CLOUDNIUM2", "enabled": True},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes_file = root / "nodes.json"
            settings_file = root / "hub-settings.json"
            nodes_file.write_text(json.dumps(nodes), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "HUB_SETTINGS_FILE", settings_file
            ):
                client = app.APP.test_client()
                saved = client.post("/api/settings", json={
                    "agent_order": ["cloudnium4", "cloudnium4", "missing", "cloudnium3"],
                })
                loaded = client.get("/api/settings")
                stored = json.loads(settings_file.read_text(encoding="utf-8"))

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["agent_order"], ["cloudnium4", "cloudnium3", "cloudnium2"])
        self.assertEqual(loaded.get_json()["agent_order"], ["cloudnium4", "cloudnium3", "cloudnium2"])
        self.assertEqual(stored["agent_order"], ["cloudnium4", "cloudnium3", "cloudnium2"])

    def test_deleted_node_is_removed_from_agent_order(self):
        from stream_control_hub import app

        nodes = [
            {"id": "cloudnium3", "name": "CLOUDNIUM3", "enabled": False},
            {"id": "cloudnium4", "name": "CLOUDNIUM4", "enabled": False},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes_file = root / "nodes.json"
            settings_file = root / "hub-settings.json"
            nodes_file.write_text(json.dumps(nodes), encoding="utf-8")
            settings_file.write_text(json.dumps({"agent_order": ["cloudnium4", "cloudnium3"]}), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "HUB_SETTINGS_FILE", settings_file
            ):
                response = app.APP.test_client().post("/api/nodes/delete", json={
                    "node_id": "cloudnium3",
                    "migrate_resources": False,
                })
                stored = json.loads(settings_file.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(stored["agent_order"], ["cloudnium4"])

    def test_same_media_source_keeps_stream_state_per_agent(self):
        from stream_control_hub import app

        nodes = [
            {"id": "cloudnium3", "name": "CLOUDNIUM3", "base_url": "http://100.64.0.3:8787", "enabled": True},
            {"id": "cloudnium4", "name": "CLOUDNIUM4", "base_url": "http://100.64.0.4:8787", "enabled": True},
        ]
        statuses = [
            {
                "ok": True,
                "agent": {"version": "test"},
                "stream": {"running": True},
                "stream_config": {
                    "video_path": "/media/shared.mp4",
                    "source_node_id": "cloudnium3",
                    "youtube_stream_id": "stream-3",
                },
            },
            {
                "ok": True,
                "agent": {"version": "test"},
                "stream": {"running": True},
                "stream_config": {
                    "video_path": "/media/shared.mp4",
                    "source_node_id": "cloudnium3",
                    "youtube_stream_id": "stream-4",
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes_file = root / "nodes.json"
            settings_file = root / "hub-settings.json"
            nodes_file.write_text(json.dumps(nodes), encoding="utf-8")
            settings_file.write_text(json.dumps({
                "node_stream_locks": {
                    "cloudnium3": {"youtube_stream_id": "stream-3", "video_path": "/media/shared.mp4"},
                    "cloudnium4": {"youtube_stream_id": "stream-4", "video_path": "/media/shared.mp4"},
                },
            }), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "HUB_SETTINGS_FILE", settings_file
            ), patch.object(app, "request_node_json", side_effect=statuses), patch.object(
                app, "request_hub_role_status", return_value={"enabled": False}
            ):
                response = app.APP.test_client().get("/api/nodes")

        listed = {item["id"]: item for item in response.get_json()}
        self.assertEqual(listed["cloudnium3"]["stream_lock"]["youtube_stream_id"], "stream-3")
        self.assertEqual(listed["cloudnium4"]["stream_lock"]["youtube_stream_id"], "stream-4")
        self.assertEqual(listed["cloudnium3"]["health"]["stream_config"]["video_path"], "/media/shared.mp4")
        self.assertEqual(listed["cloudnium4"]["health"]["stream_config"]["video_path"], "/media/shared.mp4")

    def test_agent_table_contains_drag_sort_and_persistence_logic(self):
        from stream_control_hub import app

        self.assertIn("data-node-drag-handle", app.HTML)
        self.assertIn('refs.nodeList.addEventListener("dragstart"', app.HTML)
        self.assertIn('refs.nodeList.addEventListener("drop"', app.HTML)
        self.assertIn('postJson("/api/settings", { agent_order: payloadOrder })', app.HTML)
        self.assertIn("orderedAgentRows(nodes.filter(shouldShowAgentNode))", app.HTML)
