import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class AgentUpgradeTests(unittest.TestCase):
    def test_agent_reports_git_revision(self):
        from stream_control_hub import headless_agent

        results = [
            SimpleNamespace(returncode=0, stdout="abc1234\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="main\n", stderr=""),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            with patch.object(headless_agent, "ROOT", root), patch.object(
                headless_agent.subprocess, "run", side_effect=results
            ), patch.object(headless_agent.shutil, "which", return_value="/usr/bin/systemd-run"):
                status = headless_agent.agent_version_status()

        self.assertEqual(status["version"], "abc1234")
        self.assertEqual(status["branch"], "main")
        self.assertTrue(status["upgrade_supported"])

    def test_agent_upgrade_endpoint_schedules_background_job(self):
        from stream_control_hub import headless_agent

        scheduled = {"unit": "upgrade-1", "from_version": "abc1234", "target_branch": "main"}
        with patch.object(headless_agent, "CONTROL_TOKEN", ""), patch.object(
            headless_agent, "schedule_agent_upgrade", return_value=scheduled
        ) as schedule:
            response = headless_agent.APP.test_client().post(
                "/api/upgrade", environ_base={"REMOTE_ADDR": "127.0.0.1"}
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["accepted"])
        schedule.assert_called_once_with()

    def test_unmanaged_agent_can_bootstrap_in_place(self):
        from stream_control_hub import headless_agent

        version = {
            "version": "unmanaged",
            "managed_install": False,
            "upgrade_supported": True,
        }
        completed = SimpleNamespace(returncode=0, stdout="scheduled", stderr="")
        with patch.object(headless_agent, "agent_version_status", return_value=version), patch.object(
            headless_agent, "current_systemd_service", return_value="stream-control-headless-agent-local.service"
        ), patch.object(headless_agent.shutil, "which", return_value="/usr/bin/systemd-run"), patch.object(
            headless_agent.subprocess, "run", return_value=completed
        ) as run:
            result = headless_agent.schedule_agent_upgrade()

        self.assertEqual(result["install_mode"], "in-place-bootstrap")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "systemd-run")
        self.assertIn("git clone", command[-1])
        self.assertIn("stream-control-headless-agent-local.service", command[-1])

    def test_hub_upgrades_only_requested_agent(self):
        from stream_control_hub import app

        nodes = [
            {"id": "node-a", "base_url": "http://100.64.0.10:8787", "enabled": True},
            {"id": "node-b", "base_url": "http://100.64.0.11:8787", "enabled": True},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps(nodes), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app,
                "post_node_json",
                return_value={"ok": True, "accepted": True, "message": "scheduled"},
            ) as post:
                response = app.APP.test_client().post(
                    "/api/nodes/upgrade", json={"node_id": "node-b"}
                )

        self.assertEqual(response.status_code, 202)
        post.assert_called_once_with(nodes[1], "/api/upgrade", {}, timeout=30)

    def test_ui_remembers_last_agent_and_keeps_role_actions_in_settings(self):
        from stream_control_hub import app

        self.assertIn("streamHubLastSelectedNodeId", app.HTML)
        self.assertIn('id="roleSettingsModal"', app.HTML)
        self.assertIn("data-role-settings", app.HTML)
        self.assertIn('data-settings-role="${role}"', app.HTML)
        self.assertIn("const activeAgents = nodes.filter", app.HTML)
        self.assertIn("const activeHubs = nodes.filter", app.HTML)
        self.assertIn("function streamDot(streaming)", app.HTML)
        self.assertIn('streaming ? "stream-live" : "stream-idle"', app.HTML)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr))", app.HTML)
        self.assertIn('id="themeSelect"', app.HTML)
        self.assertIn('id="editableHubTitle"', app.HTML)
        self.assertIn('id="pageTitleInput"', app.HTML)
        self.assertIn("streamHubTheme", app.HTML)
        self.assertIn("streamHubCustomTitle", app.HTML)
        self.assertIn("streamHubPageTitle", app.HTML)
        self.assertIn('id="mediaSendTargets"', app.HTML)
        self.assertIn('id="mediaMoveTargets"', app.HTML)
        self.assertIn('data-media-menu-action="send-node"', app.HTML)
        self.assertIn('data-media-menu-action="move-node"', app.HTML)
        self.assertIn("const resourceNode = selectedNode()", app.HTML)
        self.assertNotIn("升级 Agent</button>", app.HTML)
        self.assertNotIn("激活 Hub</button>", app.HTML)
        self.assertIn('id="hubNodeList"', app.HTML)
        self.assertIn("Agent 组", app.HTML)
        self.assertIn("Hub 组", app.HTML)
        self.assertNotIn("upgradeSelectedNodes", app.HTML)

    def test_agent_can_schedule_hub_activation(self):
        from stream_control_hub import headless_agent

        scheduled = {"unit": "hub-activate-1", "role": "hub", "url": "http://100.64.0.10:8788"}
        with patch.object(headless_agent, "CONTROL_TOKEN", ""), patch.object(
            headless_agent, "schedule_hub_activation", return_value=scheduled
        ) as schedule:
            response = headless_agent.APP.test_client().post(
                "/api/roles/hub/activate", environ_base={"REMOTE_ADDR": "127.0.0.1"}
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["accepted"])
        schedule.assert_called_once_with()

    def test_hub_reports_agent_and_hub_roles_per_node(self):
        from stream_control_hub import app

        node = {"id": "node-a", "base_url": "http://100.64.0.10:8787", "enabled": True}
        health = {"ok": True, "agent": {"version": "abc1234"}}
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps([node]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "request_node_json", return_value=health
            ), patch.object(
                app,
                "request_hub_role_status",
                return_value={"ok": True, "enabled": True, "version": "def5678", "url": "http://100.64.0.10:8788"},
            ):
                response = app.APP.test_client().get("/api/nodes")

        roles = response.get_json()[0]["roles"]
        self.assertEqual(roles["agent"]["version"], "abc1234")
        self.assertEqual(roles["hub"]["version"], "def5678")
        self.assertTrue(roles["hub"]["enabled"])


if __name__ == "__main__":
    unittest.main()
