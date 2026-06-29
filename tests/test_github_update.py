import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def git_result(stdout="", *, ok=True, stderr=""):
    return {
        "ok": ok,
        "exit_code": 0 if ok else 1,
        "stdout": stdout,
        "stderr": stderr,
    }


class GitHubUpdateCheckTests(unittest.TestCase):
    def test_check_compares_deployed_head_with_fetched_main(self):
        from stream_control_hub import app

        source_repo = "https://github.com/example/stream-control-hub.git"

        def fake_run_git(args, cwd=None, timeout=60):
            command = tuple(args)
            results = {
                ("fetch", "--quiet", "--no-tags", source_repo, "main"): git_result(),
                ("rev-parse", "HEAD"): git_result("local-commit"),
                ("rev-parse", "FETCH_HEAD"): git_result("remote-commit"),
                ("rev-list", "--count", "HEAD..FETCH_HEAD"): git_result("1"),
                ("rev-list", "--count", "FETCH_HEAD..HEAD"): git_result("0"),
                ("diff", "--stat", "HEAD", "FETCH_HEAD"): git_result("app.py | 2 +-"),
                ("log", "-1", "--format=%h %s", "HEAD"): git_result("local old"),
                ("log", "-1", "--format=%h %s", "FETCH_HEAD"): git_result("remote new"),
            }
            return results[command]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            with patch.object(app, "ROOT", root), patch.object(app, "SOURCE_REPO", source_repo), patch.object(
                app, "SOURCE_BRANCH", "main"
            ), patch.object(app, "run_git", side_effect=fake_run_git):
                response = app.APP.test_client().post("/api/github/check")

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["has_updates"])
        self.assertEqual(data["behind_count"], 1)
        self.assertEqual(data["local"], "local-commit")
        self.assertEqual(data["remote"], "remote-commit")

    def test_fetch_failure_is_reported_without_falling_back_to_old_cache(self):
        from stream_control_hub import app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            with patch.object(app, "ROOT", root), patch.object(
                app, "run_git", return_value=git_result(ok=False, stderr="repository unavailable")
            ):
                response = app.APP.test_client().post("/api/github/check")

        data = response.get_json()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(data["ok"])
        self.assertEqual(data["step"], "fetch")


if __name__ == "__main__":
    unittest.main()
