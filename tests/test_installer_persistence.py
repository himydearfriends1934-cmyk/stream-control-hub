import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerPersistenceTests(unittest.TestCase):
    def test_hub_preserves_trusted_remote_write_setting(self):
        script = (ROOT / "scripts" / "install-hub.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "scripts" / "install-hub.ps1").read_text(encoding="utf-8")

        self.assertIn("EXISTING_TRUSTED_REMOTE_WRITES", script)
        self.assertIn(
            "STREAM_HUB_TRUSTED_REMOTE_WRITES=$STREAM_HUB_TRUSTED_REMOTE_WRITES",
            script,
        )
        self.assertIn("EnvironmentFile=$ENV_FILE", script)
        self.assertIn("existingTrustedRemoteWrites", powershell)
        self.assertIn(
            '"STREAM_HUB_TRUSTED_REMOTE_WRITES=$TrustedRemoteWrites"',
            powershell,
        )
        self.assertIn("existingHost", powershell)
        self.assertIn("existingPort", powershell)

    def test_agent_preserves_connection_settings_during_update(self):
        script = (ROOT / "scripts" / "install-agent.sh").read_text(encoding="utf-8")

        for name in (
            "STREAM_AGENT_HOST",
            "STREAM_AGENT_PORT",
            "STREAM_AGENT_NAME",
            "STREAM_AGENT_CONTROL_HUB",
            "STREAM_AUTO_RESTART_ENABLED",
            "STREAM_AGENT_TRUSTED_REMOTE_WRITES",
        ):
            self.assertIn(f'existing_env_value {name}', script)
        self.assertIn(
            "STREAM_AGENT_TRUSTED_REMOTE_WRITES=$STREAM_AGENT_TRUSTED_REMOTE_WRITES",
            script,
        )

    def test_agent_resolution_uses_existing_environment(self):
        shell = shutil.which("sh") or shutil.which("bash")
        if not shell:
            self.skipTest("POSIX shell is unavailable")
        script = (ROOT / "scripts" / "install-agent.sh").read_text(encoding="utf-8")
        preamble = script.split("\nneed_root() {", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            (install_dir / ".agent.env").write_text(
                "STREAM_AGENT_HOST=100.64.0.20\n"
                "STREAM_AGENT_PORT=9876\n"
                "STREAM_AGENT_NAME=existing-agent\n"
                "STREAM_AGENT_CONTROL_HUB=http://100.64.0.1:8788\n"
                "STREAM_AUTO_RESTART_ENABLED=0\n"
                "STREAM_AGENT_TRUSTED_REMOTE_WRITES=1\n",
                encoding="utf-8",
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("STREAM_AGENT_") and key != "STREAM_AUTO_RESTART_ENABLED"
            }
            environment["INSTALL_DIR"] = str(install_dir)
            command = preamble + "\nprintf '%s|%s|%s|%s|%s|%s' \"$STREAM_AGENT_HOST\" \"$STREAM_AGENT_PORT\" \"$STREAM_AGENT_NAME\" \"$STREAM_AGENT_CONTROL_HUB\" \"$STREAM_AUTO_RESTART_ENABLED\" \"$STREAM_AGENT_TRUSTED_REMOTE_WRITES\"\n"
            result = subprocess.run(
                [shell, "-c", command],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )

        self.assertEqual(
            result.stdout,
            "100.64.0.20|9876|existing-agent|http://100.64.0.1:8788|0|1",
        )

    def test_services_use_consistent_restart_hardening(self):
        hub = (ROOT / "scripts" / "install-hub.sh").read_text(encoding="utf-8")
        agent = (ROOT / "scripts" / "install-agent.sh").read_text(encoding="utf-8")

        for directive in (
            "RestartSec=3",
            "TimeoutStopSec=20",
            "KillMode=control-group",
            "UMask=0077",
        ):
            self.assertIn(directive, hub)
            self.assertIn(directive, agent)

    def test_agent_installer_ignores_its_transient_upgrade_unit(self):
        script = (ROOT / "scripts" / "install-agent.sh").read_text(encoding="utf-8")

        self.assertIn("stream-control-agent-upgrade-*.service) continue", script)


if __name__ == "__main__":
    unittest.main()
