import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class StreamRecoveryTests(unittest.TestCase):
    def recovery_paths(self, module, root):
        data_dir = Path(root) / "agent_data"
        media_dir = data_dir / "media"
        media_dir.mkdir(parents=True)
        return (
            patch.object(module, "DATA_DIR", data_dir),
            patch.object(module, "MEDIA_DIR", media_dir),
            patch.object(module, "STATE_FILE", data_dir / "state.json"),
            patch.object(module, "STREAM_RESTART_FILE", data_dir / "stream_restart.json"),
        )

    def test_recovery_payload_is_private_and_removed_on_stop(self):
        from stream_control_hub import headless_agent

        with tempfile.TemporaryDirectory() as tmp:
            patches = self.recovery_paths(headless_agent, tmp)
            with patches[0], patches[1], patches[2], patches[3], patch.object(
                headless_agent, "CONTROL_TOKEN", ""
            ), patch.object(headless_agent, "ffmpeg_command", return_value=["ffmpeg"]), patch.object(
                headless_agent, "stream_output_url", return_value="rtmp://example/live/key"
            ), patch.object(
                headless_agent.subprocess,
                "Popen",
                side_effect=[SimpleNamespace(pid=4101), SimpleNamespace(pid=4102)],
            ), patch.object(
                headless_agent, "verify_stream_started", return_value={"ok": True}
            ), patch.object(
                headless_agent, "stop_process", return_value={"ok": True, "skipped": True}
            ):
                video = headless_agent.MEDIA_DIR / "video.mp4"
                video.write_bytes(b"video")
                client = headless_agent.APP.test_client()
                start = client.post(
                    "/api/start-stream",
                    json={
                        "video_path": str(video),
                        "stream_key": "private-stream-key",
                        "youtube_profile_id": "account-a",
                    },
                )
                recovery_file = headless_agent.STREAM_RESTART_FILE
                saved = json.loads(recovery_file.read_text(encoding="utf-8"))
                mode = stat.S_IMODE(recovery_file.stat().st_mode)
                restart = client.post("/api/restart-stream")
                stopped = client.post("/api/stop-stream")
                recovery_exists_after_stop = recovery_file.exists()

        self.assertEqual(start.status_code, 200)
        self.assertEqual(saved["stream_key"], "private-stream-key")
        self.assertEqual(saved["youtube_profile_id"], "account-a")
        if os.name != "nt":
            self.assertEqual(mode, 0o600)
        self.assertEqual(restart.status_code, 200)
        self.assertEqual(restart.get_json()["result"]["started_pid"], 4102)
        self.assertEqual(stopped.status_code, 200)
        self.assertFalse(recovery_exists_after_stop)

    def test_watchdog_restarts_desired_stream_after_process_exit(self):
        from stream_control_hub import headless_agent

        with tempfile.TemporaryDirectory() as tmp:
            patches = self.recovery_paths(headless_agent, tmp)
            with patches[0], patches[1], patches[2], patches[3], patch.object(
                headless_agent, "STREAM_AUTO_RESTART_ENABLED", True
            ), patch.object(headless_agent, "stream_process_owned", return_value=False), patch.object(
                headless_agent,
                "launch_stream_process",
                return_value={"pid": 4201, "log_path": "ffmpeg.log", "video_path": "video.mp4"},
            ) as launch:
                headless_agent.save_state({"stream_desired": True, "stream_pid": 4101})
                headless_agent.write_private_json(
                    headless_agent.STREAM_RESTART_FILE,
                    {"video_path": "video.mp4", "stream_key": "private-stream-key"},
                )
                result = headless_agent.stream_watchdog_tick()

        self.assertTrue(result["restarted"])
        launch.assert_called_once()
        self.assertEqual(launch.call_args.kwargs["reason"], "auto-recovery")

    def test_start_stream_reports_immediate_ffmpeg_exit(self):
        from stream_control_hub import headless_agent

        with tempfile.TemporaryDirectory() as tmp:
            patches = self.recovery_paths(headless_agent, tmp)
            with patches[0], patches[1], patches[2], patches[3], patch.object(
                headless_agent, "CONTROL_TOKEN", ""
            ), patch.object(headless_agent, "ffmpeg_command", return_value=["ffmpeg"]), patch.object(
                headless_agent, "stream_output_url", return_value="rtmp://example/live/private-key"
            ), patch.object(headless_agent, "stream_process_owned", return_value=False), patch.object(
                headless_agent, "STREAM_START_VERIFY_SECONDS", 0.1
            ), patch.object(headless_agent, "STREAM_START_VERIFY_INTERVAL_SECONDS", 0.01), patch.object(
                headless_agent.subprocess,
                "Popen",
                return_value=SimpleNamespace(pid=4101),
            ):
                video = headless_agent.MEDIA_DIR / "video.mp4"
                video.write_bytes(b"video")
                (headless_agent.DATA_DIR / "ffmpeg.log").write_text(
                    "Error writing trailer of rtmp://example/live/private-key: Broken pipe\n",
                    encoding="utf-8",
                )
                response = headless_agent.APP.test_client().post(
                    "/api/start-stream",
                    json={"video_path": str(video), "stream_key": "private-stream-key"},
                )

        data = response.get_json()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(data["ok"])
        self.assertIn("Broken pipe", data["message"])
        self.assertNotIn("private-key", json.dumps(data))

    def test_status_never_returns_recovery_stream_key(self):
        from stream_control_hub import headless_agent

        with tempfile.TemporaryDirectory() as tmp:
            patches = self.recovery_paths(headless_agent, tmp)
            with patches[0], patches[1], patches[2], patches[3], patch.object(
                headless_agent, "CONTROL_TOKEN", ""
            ), patch.object(headless_agent, "stream_process_owned", return_value=False), patch.object(
                headless_agent, "ffmpeg_processes", return_value=[]
            ), patch.object(headless_agent, "discover_public_origin", return_value=""):
                headless_agent.write_private_json(
                    headless_agent.STREAM_RESTART_FILE,
                    {"video_path": "video.mp4", "stream_key": "private-stream-key"},
                )
                response = headless_agent.APP.test_client().get("/api/status")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["stream_config"]["restart_ready"])
        self.assertNotIn("private-stream-key", response.get_data(as_text=True))

    def test_stop_process_refuses_pid_not_owned_by_agent(self):
        from stream_control_hub import headless_agent

        with patch.object(headless_agent, "process_running", return_value=True), patch.object(
            headless_agent, "stream_process_owned", return_value=False
        ), patch.object(headless_agent.os, "killpg", create=True) as killpg:
            result = headless_agent.stop_process(4101)

        self.assertTrue(result["skipped"])
        killpg.assert_not_called()

    def test_hub_uses_default_agent_restart_endpoint(self):
        from stream_control_hub import app

        node = {"id": "node-a", "base_url": "http://100.64.0.10:8787", "enabled": True}
        with tempfile.TemporaryDirectory() as tmp:
            nodes_file = Path(tmp) / "nodes.json"
            nodes_file.write_text(json.dumps([node]), encoding="utf-8")
            with patch.object(app, "NODES_FILE", nodes_file), patch.object(
                app, "request_node_json", return_value={"ok": True, "stream_config": {"restart_ready": True}}
            ), patch.object(
                app, "post_node_json", return_value={"ok": True, "message": "stream restarted"}
            ) as post:
                response = app.APP.test_client().post(
                    "/api/nodes/restart-stream",
                    json={"node_id": "node-a"},
                )

        self.assertEqual(response.status_code, 200)
        post.assert_called_once_with(node, "/api/restart-stream", {}, timeout=30)


if __name__ == "__main__":
    unittest.main()
