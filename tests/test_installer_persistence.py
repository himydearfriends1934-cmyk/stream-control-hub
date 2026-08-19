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
        self.assertIn("EnvironmentFile=-$ENV_FILE", script)
        self.assertIn("existingTrustedRemoteWrites", powershell)
        self.assertIn(
            '"STREAM_HUB_TRUSTED_REMOTE_WRITES=$TrustedRemoteWrites"',
            powershell,
        )
        self.assertIn("existingHost", powershell)
        self.assertIn("existingPort", powershell)
        self.assertIn("STREAM_HUB_SUPPRESS_TOKEN_OUTPUT", script)

    def test_hub_defaults_trusted_remote_writes_for_tailscale_host(self):
        script = (ROOT / "scripts" / "install-hub.sh").read_text(encoding="utf-8")

        self.assertIn('case "$STREAM_HUB_HOST" in', script)
        self.assertIn(
            "100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*)",
            script,
        )
        self.assertIn('STREAM_HUB_TRUSTED_REMOTE_WRITES="1"', script)
        self.assertIn('STREAM_HUB_TRUSTED_REMOTE_WRITES="0"', script)

    def test_hub_preserves_youtube_environment_during_update(self):
        script = (ROOT / "scripts" / "install-hub.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "scripts" / "install-hub.ps1").read_text(encoding="utf-8")

        for name in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_CREDENTIAL_FILE"):
            self.assertIn(f"EXISTING_{name}", script)
            self.assertIn(f"{name}=${name}", script)
        self.assertIn("existingYoutubeClientId", powershell)
        self.assertIn("existingYoutubeClientSecret", powershell)
        self.assertIn("existingYoutubeCredentialFile", powershell)
        self.assertIn('"YOUTUBE_CLIENT_ID=$youtubeClientId"', powershell)
        self.assertIn('"YOUTUBE_CREDENTIAL_FILE=$youtubeCredentialFile"', powershell)

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

    def test_role_installers_reconcile_existing_conflicting_services(self):
        hub = (ROOT / "scripts" / "install-hub.sh").read_text(encoding="utf-8")
        agent = (ROOT / "scripts" / "install-agent.sh").read_text(encoding="utf-8")

        self.assertIn("Conflicts=stream-control-headless-agent.service", hub)
        self.assertIn("Conflicts=stream-control-hub.service", agent)
        self.assertIn("systemctl disable --now stream-control-headless-agent.service", hub)
        self.assertIn("systemctl disable --now stream-control-hub.service", agent)
        self.assertIn("systemctl is-enabled --quiet stream-control-headless-agent.service", hub)
        self.assertIn("systemctl is-enabled --quiet stream-control-hub.service", agent)
        self.assertIn("pgrep -x ffmpeg", hub)
        self.assertIn("confirm the role switch explicitly", hub)
        self.assertIn("write_hub_service_unit", hub)
        self.assertIn("write_agent_service_unit", agent)

        hub_install = hub.index("reconcile_hub_role\nwrite_hub_service_unit", hub.index("need_cmd curl"))
        hub_refresh = hub.rindex("transactional_refresh_hub")
        agent_install = agent.index("reconcile_agent_role\nwrite_agent_service_unit", agent.index("need_cmd systemctl"))
        agent_refresh = agent.rindex("transactional_refresh_agent")
        self.assertLess(hub_install, hub_refresh)
        self.assertLess(agent_install, agent_refresh)
        agent_switch_marker = agent.index('if [ "$ROLE_SWITCH_CONFIRMED" = "1" ]; then')
        self.assertLess(agent_switch_marker, agent_refresh)

    def test_hub_update_restarts_the_declared_systemd_service_strictly(self):
        script = (ROOT / "scripts" / "install-hub.sh").read_text(encoding="utf-8")

        self.assertIn("systemctl show -p WorkingDirectory", script)
        self.assertIn("sed -n 's/^WorkingDirectory=//p'", script)
        self.assertIn("$SYSTEMCTL daemon-reload", script)
        self.assertIn("$SYSTEMCTL enable stream-control-hub.service", script)
        self.assertIn("$SYSTEMCTL reset-failed stream-control-hub.service", script)
        self.assertIn("$SYSTEMCTL restart stream-control-hub.service", script)
        self.assertNotIn("$SYSTEMCTL enable --now stream-control-hub.service", script)
        self.assertNotIn("if ! $SYSTEMCTL enable --now stream-control-hub.service", script)

    def test_managed_updates_stage_a_candidate_and_keep_a_rollback_snapshot(self):
        hub = (ROOT / "scripts" / "install-hub.sh").read_text(encoding="utf-8")
        agent = (ROOT / "scripts" / "install-agent.sh").read_text(encoding="utf-8")

        for script in (hub, agent):
            self.assertIn("worktree add --detach", script)
            self.assertIn("compileall -q", script)
            self.assertIn("upgrade-backups", script)
            self.assertIn("rolling back", script)
            self.assertNotIn('git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"', script)

    def test_managed_activation_resets_git_metadata_after_candidate_copy(self):
        hub = (ROOT / "scripts" / "install-hub.sh").read_text(encoding="utf-8")

        self.assertIn('git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"', hub)
        self.assertIn("cleanup_candidate\n  if [ ! -f \"$ENV_FILE\" ]", hub)
        self.assertNotIn('git -C "$INSTALL_DIR" checkout -B "$BRANCH" "origin/$BRANCH"', hub)

    def test_hub_upgrade_ignores_runtime_media_and_data_paths(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        hub = (ROOT / "scripts" / "install-hub.sh").read_text(encoding="utf-8")

        self.assertIn("media/", gitignore)
        self.assertIn(":(exclude)data", hub)
        self.assertIn(":(exclude)agent_data", hub)
        self.assertIn(":(exclude)media", hub)
        self.assertIn("local code changes", hub)

    def test_hub_role_activation_defers_restart_until_env_exists(self):
        hub = (ROOT / "scripts" / "install-hub.sh").read_text(encoding="utf-8")

        guard = 'if [ ! -f "$ENV_FILE" ]; then'
        self.assertIn(guard, hub)
        self.assertIn('echo "HUB code refreshed; environment file will be initialized before service start."', hub)
        self.assertLess(hub.index(guard), hub.index('hub_systemctl restart stream-control-hub.service'))

    def test_hub_installer_allows_root_to_manage_existing_non_root_checkout(self):
        hub = (ROOT / "scripts" / "install-hub.sh").read_text(encoding="utf-8")

        self.assertIn('git config --global --add safe.directory "$INSTALL_DIR"', hub)

    def test_installers_default_without_interactive_tty(self):
        hub = (ROOT / "scripts" / "install-hub.sh").read_text(encoding="utf-8")
        agent = (ROOT / "scripts" / "install-agent.sh").read_text(encoding="utf-8")

        self.assertIn("[ -t 0 ] && [ -r /dev/tty ] && [ -w /dev/tty ]", hub)
        self.assertIn("[ -t 0 ] && [ -r /dev/tty ] && [ -w /dev/tty ]", agent)

    def test_agent_installer_ignores_its_transient_upgrade_unit(self):
        script = (ROOT / "scripts" / "install-agent.sh").read_text(encoding="utf-8")

        self.assertIn("stream-control-agent-upgrade-*.service) continue", script)

    def test_unified_hub_install_prefers_tailscale_host(self):
        script = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

        self.assertIn("hub_host_arg()", script)
        self.assertIn("tailscale ip -4", script)
        self.assertIn('STREAM_HUB_HOST="$HUB_HOST_ARG"', script)


if __name__ == "__main__":
    unittest.main()
