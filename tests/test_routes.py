from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest


class RouteRegistrationTests(unittest.TestCase):
    def test_hub_routes_exist(self) -> None:
        from stream_control_hub.app import APP

        routes = {str(rule) for rule in APP.url_map.iter_rules()}
        self.assertIn("/", routes)
        self.assertIn("/api/nodes", routes)
        self.assertIn("/api/media/push", routes)
        self.assertIn("/api/github/check", routes)
        self.assertIn("/api/nodes/deploy/plan", routes)

    def test_node_agent_routes_are_registered_once(self) -> None:
        from stream_control_hub.node_agent.app import APP

        routes = [str(rule) for rule in APP.url_map.iter_rules()]
        expected = [
            "/",
            "/api/status",
            "/api/public-upload",
            "/api/upload-chunk",
            "/api/upload-probe",
            "/api/start-stream",
            "/api/stream/recommend",
            "/api/chat-plan",
            "/api/youtube-auth/client",
        ]
        for route in expected:
            self.assertEqual(routes.count(route), 1, route)

        duplicates = sorted({route for route in routes if routes.count(route) > 1})
        self.assertEqual(duplicates, [])

    def test_headless_index_contract(self) -> None:
        from stream_control_hub.node_agent.app import APP
        from stream_control_hub.node_agent import dashboard_ui

        original = dashboard_ui.STREAM_NODE_AGENT_MODE
        dashboard_ui.STREAM_NODE_AGENT_MODE = True
        try:
            response = APP.test_client().get("/")
        finally:
            dashboard_ui.STREAM_NODE_AGENT_MODE = original

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_json)
        payload = response.get_json()
        self.assertEqual(payload["mode"], "headless-agent")
        self.assertEqual(payload["api"]["status"], "/api/status")

    def test_deployment_plan_builder(self) -> None:
        from stream_control_hub.deployment import build_deployment_plan

        plan = build_deployment_plan(
            {
                "id": "node-a",
                "name": "Primary Stream Node",
                "control_hub_url": "http://100.64.0.1:8788",
            },
            source_repo="https://github.com/example/stream-control-hub.git",
            source_branch="main",
        )

        self.assertTrue(plan["ok"])
        self.assertEqual(plan["environment"]["STREAM_CONTROL_ROLE"], "agent")
        self.assertIn("stream_control_hub.node_agent.wsgi:APP", plan["systemd_unit"])
        self.assertTrue(plan["bootstrap_script"].startswith("#!/usr/bin/env bash"))
        self.assertTrue(plan["upgrade_commands"])

    def test_deployment_plan_api(self) -> None:
        from stream_control_hub import app

        original_nodes_file = app.NODES_FILE
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps([{
                "id": "node-a",
                "name": "Primary Stream Node",
                "base_url": "http://100.64.0.10:8787",
                "enabled": True,
            }]), encoding="utf-8")
            app.NODES_FILE = nodes_file
            try:
                response = app.APP.test_client().post(
                    "/api/nodes/deploy/plan",
                    json={"node_ids": ["node-a"], "include_script": False},
                )
            finally:
                app.NODES_FILE = original_nodes_file

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["plans"][0]["node_id"], "node-a")
        self.assertNotIn("bootstrap_script", payload["plans"][0])


if __name__ == "__main__":
    unittest.main()
