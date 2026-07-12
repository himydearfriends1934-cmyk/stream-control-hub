import json
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class AgentUpgradeTests(unittest.TestCase):
    def test_agent_installer_ignores_activation_job_and_adopts_existing_data(self):
        scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
        script = (scripts_dir / "install-agent.sh").read_text(encoding="utf-8")
        hub_script = (scripts_dir / "install-hub.sh").read_text(encoding="utf-8")

        self.assertIn("stream-control-agent-upgrade-*.service) continue", script)
        self.assertIn("stream-control-agent-activate-*.service) continue", script)
        self.assertIn('git -C "$INSTALL_DIR" init', script)
        self.assertIn('git -C "$INSTALL_DIR" checkout -B "$BRANCH" FETCH_HEAD', script)
        self.assertIn("systemctl restart stream-control-hub.service", script)
        self.assertIn('git -C "$INSTALL_DIR" init', hub_script)
        self.assertIn("systemctl restart stream-control-headless-agent.service", hub_script)
        self.assertNotIn('INSTALL_DIR exists but is not a git checkout', script)
        self.assertNotIn('INSTALL_DIR exists but is not a git checkout', hub_script)

    def test_hub_activates_agent_from_shared_checkout_without_exposing_token(self):
        from stream_control_hub import app

        completed = SimpleNamespace(returncode=0, stdout="scheduled", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "install-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            with patch.object(app, "ROOT", root), patch.object(
                app.shutil, "which", return_value="/usr/bin/systemd-run"
            ), patch.object(app.subprocess, "run", return_value=completed) as run:
                result = app.schedule_agent_role_activation(
                    "http://100.64.0.1:8788",
                    agent_name="node-a",
                    agent_token="private-token",
                )

            saved_env = (root / ".agent.env").read_text(encoding="utf-8")

        command = run.call_args.args[0][-1]
        self.assertEqual(result["role"], "agent")
        self.assertIn(str(root), command)
        self.assertIn("INSTALL_DIR=", command)
        self.assertNotIn("private-token", command)
        self.assertIn("STREAM_AGENT_NAME=node-a", saved_env)
        self.assertIn("STREAM_AGENT_CONTROL_TOKEN=private-token", saved_env)

    def test_agent_activates_hub_from_shared_checkout(self):
        from stream_control_hub import headless_agent

        completed = SimpleNamespace(returncode=0, stdout="scheduled", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "agent_data"
            with patch.object(headless_agent, "ROOT", root), patch.object(
                headless_agent, "DATA_DIR", data_dir
            ), patch.object(
                headless_agent, "tailscale_status", return_value={"self": {"tailscale_ips": ["100.64.0.2"]}}
            ), patch.object(
                headless_agent.shutil, "which", return_value="/usr/bin/systemd-run"
            ), patch.object(headless_agent.subprocess, "run", return_value=completed) as run:
                result = headless_agent.schedule_hub_activation()

        command = run.call_args.args[0][-1]
        self.assertEqual(result["role"], "hub")
        self.assertIn(str(root), command)
        self.assertIn("INSTALL_DIR=", command)
        self.assertNotIn("INSTALL_DIR=/opt/stream-control-hub ", command)

    def test_role_status_reports_inactive_counterpart_as_prepared(self):
        from stream_control_hub import app, headless_agent

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "install-agent.sh").write_text("", encoding="utf-8")
            (root / "scripts" / "install-hub.sh").write_text("", encoding="utf-8")
            with patch.object(app, "ROOT", root), patch.object(
                app, "service_active", return_value=False
            ), patch.object(app, "local_git_version", return_value="abc1234"):
                hub_data = app.APP.test_client().get("/api/role-status").get_json()
            with patch.object(headless_agent, "ROOT", root), patch.object(
                headless_agent, "CONTROL_TOKEN", ""
            ), patch.object(
                headless_agent, "systemd_service_active", return_value=False
            ), patch.object(
                headless_agent, "agent_version_status", return_value={"version": "abc1234"}
            ):
                agent_data = headless_agent.APP.test_client().get(
                    "/api/role-status", environ_base={"REMOTE_ADDR": "127.0.0.1"}
                ).get_json()

        self.assertTrue(hub_data["roles"]["agent"]["prepared"])
        self.assertFalse(hub_data["roles"]["agent"]["enabled"])
        self.assertEqual(hub_data["roles"]["agent"]["version"], "abc1234")
        self.assertTrue(agent_data["roles"]["hub"]["prepared"])
        self.assertFalse(agent_data["roles"]["hub"]["enabled"])
        self.assertEqual(agent_data["roles"]["hub"]["version"], "abc1234")

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
        self.assertNotIn('class="media-workspace"', app.HTML)
        self.assertIn('class="left-stack"', app.HTML)
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
        self.assertIn('id="mediaProfileTargets"', app.HTML)
        self.assertIn("function resourceEntriesForScope", app.HTML)
        self.assertIn("resourceAllMode", app.HTML)
        self.assertIn("selectResourceNodeScope", app.HTML)
        self.assertIn('Agent Profile（自动继承）', app.HTML)
        self.assertIn("function ensureSmartStartMedia", app.HTML)
        self.assertIn('postJson("/api/media/share"', app.HTML)
        self.assertIn('same_node: true', app.HTML)
        self.assertIn("复制完成后会自动启动推流", app.HTML)
        self.assertIn("Smart Start 失败：", app.HTML)
        self.assertIn('data-media-local="${localCopy ? "1" : "0"}"', app.HTML)
        self.assertIn('request_node_json(target_node, "/api/public-upload", timeout=10)', inspect.getsource(app.run_share_task))
        self.assertIn("discovered_public_url", inspect.getsource(app.run_share_task))
        self.assertIn("share_transfer_preflight", inspect.getsource(app.api_media_share))
        self.assertIn("function showShareRepairGuide", app.HTML)
        self.assertIn("Agent 公网互传预检失败", app.HTML)
        self.assertIn("打开目标 Agent 设置", app.HTML)
        self.assertIn("云厂商安全组必须另外放行入站 TCP 8787", app.HTML)

    def test_media_profile_follows_agent_profile_in_library(self):
        from stream_control_hub import app

        profile_config = {
            "version": 1,
            "active_profile_id": "default",
            "profiles": [
                {"id": "default", "name": "Default"},
                {"id": "account-a", "name": "Account A"},
            ],
        }
        node = {"id": "node-a", "name": "Node A", "enabled": True}
        status = {
            "ok": True,
            "disk": {"total": 1000, "used": 100, "free": 900, "percent": 10},
            "videos": [{
                "name": "video.mp4",
                "video_path": "/media/video.mp4",
                "size": 123,
                "modified": 100,
                "modified_label": "now",
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            metadata_file = Path(tmp) / "media-library-meta.json"
            with patch.object(app, "MEDIA_METADATA_FILE", metadata_file), patch.object(
                app, "load_youtube_profiles_config", return_value=profile_config
            ), patch.object(app, "load_nodes", return_value=[node]), patch.object(
                app, "node_youtube_profile_map", return_value={"node-a": "account-a"}
            ), patch.object(app, "request_node_json", return_value=status):
                library = app.media_library_payload()

        copy = library["resources"][0]["copies"][0]
        self.assertEqual(copy["profile_id"], "account-a")
        self.assertEqual(copy["profile_name"], "Account A")
        self.assertEqual(library["resources"][0]["profile_id"], "account-a")
        self.assertEqual(library["resources"][0]["profile_name"], "Account A")
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
        self.assertIn('data-node-lock-toggle data-lock-field="profile"', app.HTML)
        self.assertIn('data-node-lock-toggle data-lock-field="stream"', app.HTML)
        self.assertIn('alert("请先解锁 Profile")', app.HTML)
        self.assertIn('alert("请先解锁直播流")', app.HTML)
        self.assertIn('<span class="node-live-label">视频</span>', app.HTML)
        self.assertIn("正在推流，停止后才能修改", app.HTML)
        self.assertIn("停止状态，可调整下次推流参数", app.HTML)
        self.assertIn("data-stream-toggle-action=\"start\"", app.HTML)
        self.assertIn("data-stream-toggle-action=\"stop\"", app.HTML)
        self.assertIn("data-node-smart-tune", app.HTML)
        self.assertIn('class="node-stream-controls"', app.HTML)
        self.assertIn('class="smart-tune-button', app.HTML)
        self.assertIn("function enableNodeSmartTune", app.HTML)
        self.assertIn("const nextSmartTuneEnabled = !Boolean(profile.auto_tune_enabled);", app.HTML)
        self.assertIn("auto_tune_enabled: nextSmartTuneEnabled", app.HTML)
        self.assertIn("window.confirm([", app.HTML)
        self.assertIn('refs.nodeList.addEventListener("click"', app.HTML)
        self.assertIn('event.target.closest("[data-node-smart-tune]")', app.HTML)
        self.assertNotIn("data-node-check", app.HTML)
        self.assertNotIn("checkedIds", app.HTML)
        self.assertIn("const ids = new Set();", app.HTML)
        self.assertIn("if (selectedNodeId) ids.add(String(selectedNodeId));", app.HTML)
        self.assertIn("function smartStartNode", app.HTML)
        self.assertIn("function streamStartFailureText", app.HTML)
        self.assertIn("FFmpeg 日志尾部", app.HTML)
        self.assertIn("if (!data.ok)", app.HTML)
        self.assertIn("data-settings-node-action=\"stop-stream\"", app.HTML)
        self.assertIn("data-settings-node-action=\"restart-stream\"", app.HTML)
        self.assertIn("data-settings-node-action=\"reboot-vps\"", app.HTML)
        self.assertNotIn("data-node-action=\"stop-stream\"", app.HTML)
        self.assertNotIn("data-node-action=\"restart-stream\"", app.HTML)
        self.assertNotIn("data-node-action=\"reboot-vps\"", app.HTML)
        self.assertIn(".node-role-split { display: block; height: auto; min-height: 0;", app.HTML)
        self.assertIn("grid-template-columns: 34px minmax(0, 1fr) 64px 176px minmax(102px, .36fr)", app.HTML)
        self.assertIn("grid-template-columns: 1fr 1fr", app.HTML)
        self.assertIn("grid-template-columns: minmax(0, 1fr) 54px", app.HTML)
        self.assertNotIn("min-width: 1110px", app.HTML)
        self.assertNotIn("minmax(760px", app.HTML)
        self.assertNotIn("升级 Agent</button>", app.HTML)
        self.assertIn("同步节点信息到所有已激活 Hub", app.HTML)
        self.assertIn('class="actions github-update-actions"', app.HTML)
        self.assertIn('class="github-update-more"', app.HTML)
        self.assertIn('<summary>功能</summary>', app.HTML)
        self.assertIn('class="github-update-menu"', app.HTML)
        self.assertIn('githubUpdateMore: document.getElementById("githubUpdateMore")', app.HTML)
        self.assertIn('refs.githubUpdateMore.addEventListener("click"', app.HTML)
        self.assertIn("grid-template-columns: minmax(250px, .8fr) minmax(390px, 1.25fr) minmax(230px, .7fr)", app.HTML)
        self.assertIn('class="top-utility-strip"', app.HTML)
        self.assertIn('class="top-log-panel log-card"', app.HTML)
        self.assertNotIn('class="bottom-section"', app.HTML)
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
        self.assertIn("max-height: 248px", app.HTML)
        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(128px, 1fr))", app.HTML)
        self.assertIn("-webkit-line-clamp: 2", app.HTML)
        self.assertIn("剩余 ${escapeHtml(fmtBytes(item.free))} · 已用", app.HTML)
        self.assertIn('<details class="role-group node-role-pane hub-role-pane" id="hubNodePane">', app.HTML)
        self.assertNotIn('id="nodeRoleSplitter"', app.HTML)
        self.assertNotIn("syncNodeRoleSplitHeight", app.HTML)
        self.assertIn('data-space-node-id=', app.HTML)
        self.assertIn("function openNodeResources", app.HTML)
        self.assertIn("复制源 / 上传源", app.HTML)
        self.assertIn("etaSeconds: status.eta_seconds", app.HTML)
        self.assertIn('data-media-rename-name', app.HTML)
        self.assertIn('handleMediaAction("rename", row)', app.HTML)
        self.assertIn("grid-template-columns: minmax(86px, .75fr) minmax(88px, .6fr) minmax(108px, .75fr) minmax(96px, .8fr) minmax(126px, 1fr)", app.HTML)
        self.assertIn("归属节点 / 副本数", app.HTML)
        self.assertNotIn('data-resource-filter="copyNode"', app.HTML)
        self.assertNotIn('class="node-role-summary"', app.HTML)
        self.assertIn(".grid > .side-stack { align-self: start; grid-template-rows: auto; }", app.HTML)
        self.assertIn("Agent 节点", app.HTML)
        self.assertIn("Hub 节点", app.HTML)
        self.assertIn("Profile / 直播流 / 直播视频", app.HTML)
        self.assertIn(".left-stack .resource-card { order: -1; }", app.HTML)
        self.assertIn('class="monitor-panel monitor-disclosure" data-monitor-section="engine"', app.HTML)
        self.assertIn('class="monitor-panel monitor-disclosure" data-monitor-section="resources"', app.HTML)
        self.assertIn("function monitorDisclosureOpen", app.HTML)
        self.assertIn("rememberMonitorDisclosure", app.HTML)
        left_stack_start = app.HTML.index('<div class="left-stack">')
        monitor_position = app.HTML.index('<div class="card monitor-card">')
        resource_position = app.HTML.index('<div class="card resource-card">')
        side_stack_start = app.HTML.index('<div class="side-stack">')
        node_table_position = app.HTML.index('<div class="card node-table-card">')
        node_space_position = app.HTML.index('<div class="card node-space-card">')
        upload_position = app.HTML.index('<div class="card upload-card">')
        utility_position = app.HTML.index('<section class="top-utility-strip"')
        quick_connect_position = app.HTML.index('<strong>Agent 快速连接</strong>')
        github_position = app.HTML.index('<strong>GitHub 更新</strong>')
        api_position = app.HTML.index('<strong>YouTube API</strong>')
        log_position = app.HTML.index('<section class="top-log-panel log-card">')
        command_position = app.HTML.index('<section class="card command-strip">')
        self.assertLess(utility_position, quick_connect_position)
        self.assertLess(quick_connect_position, github_position)
        self.assertLess(github_position, api_position)
        self.assertLess(api_position, log_position)
        self.assertLess(log_position, command_position)
        self.assertLess(left_stack_start, monitor_position)
        self.assertLess(monitor_position, resource_position)
        self.assertLess(resource_position, side_stack_start)
        self.assertLess(side_stack_start, node_table_position)
        self.assertLess(node_table_position, node_space_position)
        self.assertLess(node_space_position, upload_position)
        self.assertIn('miniRow("YouTube 智能调参"', app.HTML)
        self.assertIn("youtubeProfile.auto_tune_interval_seconds", app.HTML)
        self.assertIn("youtubeProfile.auto_tune_cooldown_seconds", app.HTML)
        self.assertIn("youtubeProfile.auto_tune_max_bitrate", app.HTML)
        self.assertIn('miniRow("Agent 自适应模式"', app.HTML)
        self.assertIn("智能调参记录", app.HTML)
        self.assertIn("API 问题：", app.HTML)
        self.assertIn("调参判断：", app.HTML)
        self.assertIn("更改 Agent：", app.HTML)
        self.assertIn("更改后：", app.HTML)
        self.assertNotIn("upgradeSelectedNodes", app.HTML)

    def test_media_library_keeps_profile_per_agent_copy(self):
        from stream_control_hub import app

        profile_config = {
            "version": 1,
            "active_profile_id": "default",
            "profiles": [
                {"id": "default", "name": "Default"},
                {"id": "account-a", "name": "Account A"},
            ],
        }
        nodes = [
            {"id": "node-a", "name": "Node A", "enabled": True},
            {"id": "node-b", "name": "Node B", "enabled": True},
        ]
        statuses = [
            {
                "ok": True,
                "disk": {"total": 1000, "used": 100, "free": 900, "percent": 10},
                "videos": [{"name": "video.mp4", "video_path": "/media/a/video.mp4", "size": 123, "modified": 100}],
            },
            {
                "ok": True,
                "disk": {"total": 2000, "used": 200, "free": 1800, "percent": 10},
                "videos": [{"name": "video.mp4", "video_path": "/media/b/video.mp4", "size": 456, "modified": 200}],
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app, "MEDIA_METADATA_FILE", Path(tmp) / "media-library-meta.json"), patch.object(
                app, "load_youtube_profiles_config", return_value=profile_config
            ), patch.object(app, "load_nodes", return_value=nodes), patch.object(
                app, "node_youtube_profile_map", return_value={"node-a": "account-a", "node-b": "default"}
            ), patch.object(app, "request_node_json", side_effect=statuses):
                item = app.media_library_payload()["resources"][0]

        self.assertEqual({copy["profile_id"] for copy in item["copies"]}, {"account-a", "default"})
        self.assertEqual(item["profile_ids"], ["account-a", "default"])
        self.assertEqual(item["profile_id"], "")
        self.assertEqual({copy["size"] for copy in item["copies"]}, {123, 456})

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
