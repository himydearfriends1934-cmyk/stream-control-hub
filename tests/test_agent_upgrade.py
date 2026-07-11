import json
import inspect
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

    def test_agent_hub_activation_accepts_seed_nodes(self):
        from stream_control_hub import headless_agent

        seed = [{"id": "node-a", "base_url": "http://100.64.0.10:8787"}]
        with patch.object(headless_agent, "CONTROL_TOKEN", ""), patch.object(
            headless_agent, "schedule_hub_activation", return_value={"unit": "hub-activate-1", "role": "hub"}
        ) as schedule:
            response = headless_agent.APP.test_client().post(
                "/api/roles/hub/activate",
                json={"nodes": seed},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 202)
        schedule.assert_called_once_with(seed)

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
        self.assertIn("box-shadow: inset 5px 0 0 #ff3b4f", app.HTML)
        self.assertIn(".node-row.selected .node-name strong", app.HTML)
        self.assertIn(".node-row.control-hub", app.HTML)
        self.assertIn("function switchHubWithFallback", app.HTML)
        self.assertIn("/api/hubs/switch-target", app.HTML)
        self.assertIn('id="roleSettingsModal"', app.HTML)
        self.assertIn("data-role-settings", app.HTML)
        self.assertIn('data-settings-role="${role}"', app.HTML)
        self.assertIn("const agentRows = nodes.filter", app.HTML)
        self.assertIn("const shouldShowAgentRow", app.HTML)
        self.assertIn("agentEnabled || nodeHasResources(nodeId)", app.HTML)
        self.assertIn("/api/nodes/delete", app.HTML)
        self.assertIn('id="roleSettingsDeleteNodeBtn"', app.HTML)
        self.assertIn("deleteNodeRecord(roleSettingsNodeId)", app.HTML)
        self.assertNotIn('refs.nodeList.addEventListener("contextmenu"', app.HTML)
        self.assertNotIn('refs.nodeSpaceRings.addEventListener("contextmenu"', app.HTML)
        self.assertIn("const activeHubs = nodes.filter", app.HTML)
        self.assertIn("function streamDot(streaming)", app.HTML)
        self.assertIn('streaming ? "stream-live" : "stream-idle"', app.HTML)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr))", app.HTML)
        self.assertIn('id="themeSelect"', app.HTML)
        self.assertIn('id="editableHubTitle"', app.HTML)
        self.assertIn("streamHubTheme", app.HTML)
        self.assertIn("streamHubCustomTitle", app.HTML)
        self.assertNotIn('id="pageTitleInput"', app.HTML)
        self.assertIn('id="mediaSendTargets"', app.HTML)
        self.assertIn('id="mediaMoveTargets"', app.HTML)
        self.assertIn('class="media-workspace"', app.HTML)
        self.assertIn('class="card resource-card"', app.HTML)
        self.assertIn('class="card upload-card"', app.HTML)
        self.assertIn('id="profileQuickBar"', app.HTML)
        self.assertIn('id="mediaProfileFilter"', app.HTML)
        self.assertIn('id="resourceMoreBtn"', app.HTML)
        self.assertIn('id="resourceToolsModal"', app.HTML)
        self.assertIn('id="resourceFilterChip"', app.HTML)
        self.assertNotIn('id="mediaTotalTable"', app.HTML)
        self.assertNotIn("资源总表", app.HTML)
        self.assertNotIn('id="mediaGroupTargets"', app.HTML)
        self.assertIn('id="mediaDiskList" hidden', app.HTML)
        self.assertIn('data-resource-filter="name"', app.HTML)
        self.assertIn('data-resource-filter="profile"', app.HTML)
        self.assertIn('data-media-menu-action="property"', app.HTML)
        self.assertNotIn('data-media-menu-action="move-group"', app.HTML)

    def test_profiles_filter_resources_and_support_double_click_rename(self):
        from stream_control_hub import app

        self.assertNotIn('id="mediaGroupSearchInput"', app.HTML)
        self.assertNotIn('id="quickGroupAddBtn"', app.HTML)
        self.assertNotIn('id="quickGroupRemoveBtn"', app.HTML)
        self.assertNotIn("const QUICK_GROUP_LIMIT = 6", app.HTML)
        self.assertIn("const PROFILE_FILTER_VISIBLE_SLOTS = 6", app.HTML)
        self.assertIn('class="profile-filter-bar"', app.HTML)
        self.assertIn('refs.profileQuickBar.addEventListener("dblclick"', app.HTML)
        self.assertIn('title="双击改名"', app.HTML)
        self.assertIn('prompt("修改 Profile 名称："', app.HTML)
        self.assertIn("saveYouTubeProfileName(nextName, profileId)", app.HTML)
        self.assertNotIn('refs.quickGroupBar.addEventListener("contextmenu"', app.HTML)
        self.assertNotIn('id="quickGroupManageBtn"', app.HTML)
        self.assertNotIn('id="quickGroupCreateBtn"', app.HTML)
        self.assertNotIn('id="quickGroupDeleteBtn"', app.HTML)
        self.assertIn('list="resourceNameOptions"', app.HTML)
        self.assertIn("data-clear-resource-filters", app.HTML)
        self.assertIn("function clearResourceFilters", app.HTML)
        self.assertIn("function ensureSmartStartMedia", app.HTML)
        self.assertIn('postJson("/api/media/share"', app.HTML)
        self.assertIn("复制完成后会自动启动推流", app.HTML)
        self.assertIn("Smart Start 失败：", app.HTML)
        self.assertIn('data-media-local="${localCopy ? "1" : "0"}"', app.HTML)
        self.assertIn('request_node_json(target_node, "/api/public-upload", timeout=10)', inspect.getsource(app.run_share_task))
        self.assertIn("discovered_public_url", inspect.getsource(app.run_share_task))
        self.assertIn("不支持媒体哈希校验", inspect.getsource(app.run_share_task))
        self.assertIn("source_hash=source_hash", inspect.getsource(app.run_share_task))
        self.assertIn("首选源节点不可用，已切换到", inspect.getsource(app.run_share_task))
        self.assertIn("所有在线源 Agent", inspect.getsource(app.run_share_task))
        self.assertIn("function showMediaProperties", app.HTML)
        self.assertNotIn("function moveMediaToGroup", app.HTML)
        self.assertNotIn("function renameQuickGroup", app.HTML)
        self.assertIn("function setResourceToolsOpen", app.HTML)
        self.assertIn('targetButtons("send-node")', app.HTML)
        self.assertIn('targetButtons("move-node")', app.HTML)
        self.assertIn("const sourceNode = nodes.find", app.HTML)
        self.assertIn("const targetNode = selectedNode()", app.HTML)
        self.assertNotIn("data-node-note", app.HTML)
        self.assertNotIn("/api/nodes/note", app.HTML)
        self.assertIn("data-node-name-edit", app.HTML)
        self.assertIn("function beginNodeNameEdit", app.HTML)
        self.assertIn('refs.nodeList.addEventListener("dblclick"', app.HTML)
        self.assertIn("class=\"node-agent-line\"", app.HTML)
        self.assertIn('class="node-live-field"', app.HTML)
        self.assertIn('class="node-row agent-row', app.HTML)
        self.assertIn('class="node-table-head agent-table-head"', app.HTML)
        self.assertIn('class="node-index"', app.HTML)
        self.assertIn('class="node-param-summary"', app.HTML)
        self.assertIn('"index identity online stream actions"', app.HTML)
        self.assertIn('"locks locks locks locks locks"', app.HTML)
        self.assertIn(".node-row.agent-row .node-live-locks { grid-area: locks; }", app.HTML)
        self.assertIn('const lockHint = streaming ? "正在推流，停止后才能修改"', app.HTML)
        self.assertIn('const lockAttr = streaming ? "disabled aria-disabled=\\\"true\\\""', app.HTML)
        self.assertIn("function nodeStreamParameterSummary", app.HTML)
        self.assertIn("if (nodeStreaming(node) && config.youtube_profile_id) return String(config.youtube_profile_id);", app.HTML)
        self.assertIn("if (!nodeStreaming(node)) return lock;", app.HTML)
        self.assertIn("function nodeRowYoutubeStreamId", app.HTML)
        self.assertIn("if (nodeStreaming(node) && config.youtube_stream_id) return String(config.youtube_stream_id || \"\");", app.HTML)
        self.assertIn("const rowStreamId = nodeRowYoutubeStreamId(node, rowLock);", app.HTML)
        self.assertIn("const profileId = nodeRowProfileId(node);", app.HTML)
        self.assertIn("const AGENT_STREAM_REFRESH_MS = 5 * 60 * 1000", app.HTML)
        self.assertIn("function refreshRunningAgentParameters", app.HTML)
        self.assertIn("window.setInterval(refreshRunningAgentParameters, AGENT_STREAM_REFRESH_MS)", app.HTML)
        self.assertIn("if (!hadStreamingAgents && !hasStreamingAgentRows()) return;", app.HTML)
        self.assertIn("function showDiagnostics", app.HTML)
        self.assertIn("function statusSummaryText", app.HTML)
        self.assertIn("正在刷新状态，请稍候", app.HTML)
        self.assertIn("Upload Policy 上传策略", app.HTML)
        self.assertIn("Push Audit 最近推送记录", app.HTML)
        self.assertIn('<span class="node-live-label">Profile</span>', app.HTML)
        self.assertIn('<span class="node-live-label">直播流</span>', app.HTML)
        self.assertIn('<span class="node-live-label">视频</span>', app.HTML)
        self.assertIn("正在推流，停止后才能修改", app.HTML)
        self.assertIn("停止状态，可调整下次推流参数", app.HTML)
        self.assertIn("data-stream-toggle-action=\"start\"", app.HTML)
        self.assertIn("data-stream-toggle-action=\"stop\"", app.HTML)
        self.assertNotIn("data-node-check", app.HTML)
        self.assertNotIn("checkedIds", app.HTML)
        self.assertIn("const ids = new Set();", app.HTML)
        self.assertIn("if (selectedNodeId) ids.add(String(selectedNodeId));", app.HTML)
        self.assertIn("function smartStartNode", app.HTML)
        self.assertIn("data-settings-node-action=\"stop-stream\"", app.HTML)
        self.assertIn("data-settings-node-action=\"restart-stream\"", app.HTML)
        self.assertIn("data-settings-node-action=\"reboot-vps\"", app.HTML)
        self.assertNotIn("data-node-action=\"stop-stream\"", app.HTML)
        self.assertNotIn("data-node-action=\"restart-stream\"", app.HTML)
        self.assertNotIn("data-node-action=\"reboot-vps\"", app.HTML)
        self.assertIn("overflow-y: auto; overflow-x: hidden", app.HTML)
        self.assertIn("grid-template-columns: 34px minmax(0, 1fr) 64px 112px minmax(118px, .42fr)", app.HTML)
        self.assertNotIn("min-width: 1110px", app.HTML)
        self.assertNotIn("minmax(760px", app.HTML)
        self.assertNotIn("升级 Agent</button>", app.HTML)
        self.assertIn("同步节点信息到所有已激活 Hub", app.HTML)
        self.assertIn('id="hubNodeList"', app.HTML)
        self.assertIn('id="agentNodeCount"', app.HTML)
        self.assertIn('id="hubNodeCount"', app.HTML)
        self.assertIn("refs.agentNodeCount.textContent", app.HTML)
        self.assertIn("refs.hubNodeCount.textContent", app.HTML)
        self.assertIn("grid-template-columns: minmax(520px, 0.92fr) minmax(600px, 1.08fr)", app.HTML)
        self.assertIn('id="nodeSpaceRings"', app.HTML)
        self.assertIn('class="upload-stack"', app.HTML)
        self.assertIn("function renderNodeSpaceRings", app.HTML)
        self.assertIn("const diskByNodeId = new Map", app.HTML)
        self.assertIn("conic-gradient", app.HTML)
        self.assertIn("max-height: 88px", app.HTML)
        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(104px, 1fr))", app.HTML)
        self.assertIn('id="nodeRoleSplitter"', app.HTML)
        self.assertIn("function initNodeRoleSplitter", app.HTML)
        self.assertIn('data-space-node-id=', app.HTML)
        self.assertIn("function openNodeResources", app.HTML)
        self.assertIn("复制源 / 上传源", app.HTML)
        self.assertIn("etaSeconds: status.eta_seconds", app.HTML)
        self.assertIn('data-media-rename-name', app.HTML)
        self.assertIn('handleMediaAction("rename", row)', app.HTML)
        self.assertIn("grid-template-columns: minmax(78px, .8fr) minmax(45px, .42fr) minmax(58px, .5fr) minmax(96px, .9fr) minmax(137px, 1.35fr) minmax(90px, .9fr)", app.HTML)
        self.assertNotIn('class="node-role-summary"', app.HTML)
        self.assertIn(".grid > .side-stack { align-self: start; grid-template-rows: auto; }", app.HTML)
        self.assertIn("Agent 节点", app.HTML)
        self.assertIn("Hub 节点", app.HTML)
        self.assertIn("Profile / 直播流 / 直播视频", app.HTML)
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
        schedule.assert_called_once_with(None)

    def test_agent_can_schedule_role_deactivation(self):
        from stream_control_hub import headless_agent

        with patch.object(headless_agent, "CONTROL_TOKEN", ""), patch.object(
            headless_agent, "schedule_hub_deactivation", return_value={"unit": "hub-off", "role": "hub"}
        ) as hub_deactivate:
            response = headless_agent.APP.test_client().post(
                "/api/roles/hub/deactivate", environ_base={"REMOTE_ADDR": "127.0.0.1"}
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["accepted"])
        hub_deactivate.assert_called_once_with()

        with patch.object(headless_agent, "CONTROL_TOKEN", ""), patch.object(
            headless_agent, "schedule_agent_deactivation", return_value={"unit": "agent-off", "role": "agent"}
        ) as agent_deactivate:
            response = headless_agent.APP.test_client().post(
                "/api/roles/agent/deactivate", environ_base={"REMOTE_ADDR": "127.0.0.1"}
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["accepted"])
        agent_deactivate.assert_called_once_with()

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

    def test_hub_imports_nodes_and_transfer_posts_to_target_hub(self):
        from stream_control_hub import app

        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps([{"id": "old", "base_url": "http://100.64.0.2:8787"}]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file):
                response = app.APP.test_client().post(
                    "/api/nodes/import",
                    json={"nodes": [{"id": "new", "base_url": "http://100.64.0.3:8787", "token": "secret"}]},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["imported_count"], 1)
                saved = json.loads(nodes_file.read_text(encoding="utf-8"))
                self.assertEqual({item["id"] for item in saved}, {"old", "new"})

            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "post_url_json", return_value={"ok": True, "imported_count": 2}
            ) as post:
                response = app.APP.test_client().post(
                    "/api/hub-transfer/nodes",
                    json={"target_hub_url": "http://100.64.0.9:8788", "target_token": "hub-token"},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["ok"])
                args, kwargs = post.call_args
                self.assertEqual(args[0], "http://100.64.0.9:8788/api/nodes/import")
                self.assertEqual(kwargs["headers"], {"X-Control-Token": "hub-token"})

    def test_hub_syncs_nodes_to_all_active_hubs(self):
        from stream_control_hub import app

        nodes = [
            {"id": "hub-a", "base_url": "http://100.64.0.2:8787"},
            {"id": "agent-b", "base_url": "http://100.64.0.3:8787"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps(nodes), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app,
                "request_hub_role_status",
                side_effect=[
                    {"ok": True, "enabled": True, "url": "http://100.64.0.2:8788"},
                    {"ok": False, "enabled": False, "url": "http://100.64.0.3:8788"},
                ],
            ), patch.object(app, "post_url_json", return_value={"ok": True, "imported_count": 2}) as post:
                response = app.APP.test_client().post(
                    "/api/hubs/sync",
                    json={},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(response.get_json()["target_count"], 1)
        self.assertEqual(post.call_args.args[0], "http://100.64.0.2:8788/api/nodes/import")
        self.assertEqual(post.call_args.args[1]["nodes"], nodes)

    def test_hub_switch_target_falls_back_to_available_hub(self):
        from stream_control_hub import app

        nodes = [
            {"id": "dead-hub", "name": "Dead", "base_url": "http://100.64.0.2:8787"},
            {"id": "live-hub", "name": "Live", "base_url": "http://100.64.0.3:8787"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps(nodes), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app,
                "request_hub_role_status",
                side_effect=[
                    {"ok": False, "enabled": False, "url": "http://100.64.0.2:8788"},
                    {"ok": True, "enabled": True, "url": "http://100.64.0.3:8788"},
                ],
            ):
                response = app.APP.test_client().post(
                    "/api/hubs/switch-target",
                    json={"node_id": "dead-hub"},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["fallback"])
        self.assertEqual(data["node_id"], "live-hub")
        self.assertEqual(data["url"], "http://100.64.0.3:8788")

    def test_hub_activation_forwards_current_nodes_as_seed(self):
        from stream_control_hub import app

        nodes = [{"id": "node-a", "base_url": "http://100.64.0.10:8787", "token": "secret"}]
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps(nodes), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "post_node_json", return_value={"ok": True, "accepted": True}
            ) as post:
                response = app.APP.test_client().post(
                    "/api/nodes/roles/hub/activate",
                    json={"node_id": "node-a"},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(post.call_args.args[1], "/api/roles/hub/activate")
        self.assertEqual(post.call_args.args[2]["nodes"], nodes)

    def test_hub_deletes_node_record_only_from_config(self):
        from stream_control_hub import app

        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(
                json.dumps([
                    {"id": "keep", "base_url": "http://100.64.0.2:8787"},
                    {"id": "remove", "base_url": "http://100.64.0.3:8787"},
                ]),
                encoding="utf-8",
            )
            with patch.object(app, "NODES_FILE", nodes_file):
                response = app.APP.test_client().post(
                    "/api/nodes/delete",
                    json={"node_id": "remove", "migrate_resources": False},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["deleted"])
                saved = json.loads(nodes_file.read_text(encoding="utf-8"))
                self.assertEqual([item["id"] for item in saved], ["keep"])

    def test_node_delete_plans_unique_resources_by_largest_free_capacity(self):
        from stream_control_hub import app

        source = {"id": "source", "base_url": "http://100.64.0.2:8787"}
        big = {"id": "big", "base_url": "http://100.64.0.3:8787"}
        second = {"id": "second", "base_url": "http://100.64.0.4:8787"}
        library = {
            "nodes": [
                {"node_id": "source", "online": True, "free": 100},
                {"node_id": "big", "online": True, "free": 1000},
                {"node_id": "second", "online": True, "free": 500},
            ],
            "resources": [
                {
                    "name": "large.mp4",
                    "size": 800,
                    "copies": [{"node_id": "source", "video_path": "large.mp4"}],
                },
                {
                    "name": "small.mp4",
                    "size": 400,
                    "copies": [{"node_id": "source", "video_path": "small.mp4"}],
                },
                {
                    "name": "already-safe.mp4",
                    "size": 300,
                    "copies": [
                        {"node_id": "source", "video_path": "already-safe.mp4"},
                        {"node_id": "big", "video_path": "already-safe.mp4"},
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps([source, big, second]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(app, "media_library_payload", return_value=library):
                plan = app.node_delete_migration_plan(source)

        self.assertTrue(plan["ok"])
        self.assertEqual([(item["name"], item["target_node_id"]) for item in plan["plan"]], [
            ("large.mp4", "big"),
            ("small.mp4", "second"),
        ])

    def test_node_delete_creates_migration_task_before_removing_record(self):
        from stream_control_hub import app

        source = {"id": "source", "base_url": "http://100.64.0.2:8787"}
        target = {"id": "target", "base_url": "http://100.64.0.3:8787"}
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps([source, target]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app,
                "node_delete_migration_plan",
                return_value={"ok": True, "online": True, "plan": [{"name": "a.mp4", "video_path": "a.mp4", "size": 10, "target_node": target, "target_node_id": "target"}]},
            ), patch.object(app.threading.Thread, "start", return_value=None) as start:
                response = app.APP.test_client().post(
                    "/api/nodes/delete",
                    json={"node_id": "source"},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["migration_required"])
        start.assert_called_once()

    def test_hub_can_forward_role_deactivation(self):
        from stream_control_hub import app

        node = {"id": "node-a", "base_url": "http://100.64.0.10:8787", "enabled": True}
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps([node]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "post_node_json", return_value={"ok": True, "accepted": True}
            ) as post:
                response = app.APP.test_client().post(
                    "/api/nodes/roles/agent/deactivate",
                    json={"node_id": "node-a"},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )
        self.assertEqual(response.status_code, 202)
        post.assert_called_once()
        self.assertEqual(post.call_args.args[1], "/api/roles/agent/deactivate")


if __name__ == "__main__":
    unittest.main()
