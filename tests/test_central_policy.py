import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stream_control_hub.motion_analysis import classify_motion_samples
from stream_control_hub.policy import issue_policy, validate_policy
from stream_control_hub.role_lock import RoleConflictError, assert_role, write_role_marker


class CentralPolicyTests(unittest.TestCase):
    def test_motion_classifier_covers_static_medium_and_dynamic_sources(self):
        static = classify_motion_samples([
            {"changed_ratio": 0.02, "mean_diff": 0.01},
            {"changed_ratio": 0.04, "mean_diff": 0.02},
        ])
        medium = classify_motion_samples([
            {"changed_ratio": 0.16, "mean_diff": 0.05},
            {"changed_ratio": 0.20, "mean_diff": 0.04},
        ])
        dynamic = classify_motion_samples([
            {"changed_ratio": 0.55, "mean_diff": 0.13},
            {"changed_ratio": 0.48, "mean_diff": 0.11},
        ])

        self.assertEqual(static["level"], "static")
        self.assertEqual(medium["level"], "medium")
        self.assertEqual(dynamic["level"], "dynamic")

    def test_policy_rejects_expired_or_wrong_authority_commands(self):
        payload = issue_policy({"video_path": "clip.mp4"}, reason="test", ttl_seconds=60)

        self.assertEqual(validate_policy(payload, now=float(payload["policy_issued_at"]) + 1), "")
        self.assertIn("expired", validate_policy(payload, now=float(payload["policy_expires_at"]) + 1))
        payload["policy_authority"] = "agent"
        self.assertIn("HUB", validate_policy(payload, now=float(payload["policy_issued_at"]) + 1))

    def test_agent_refuses_expired_hub_policy_before_touching_media(self):
        from stream_control_hub import headless_agent

        payload = issue_policy({"video_path": "missing.mp4"}, reason="test", ttl_seconds=30)
        payload["policy_expires_at"] = 1
        with patch.object(headless_agent, "CONTROL_TOKEN", ""):
            response = headless_agent.APP.test_client().post("/api/start-stream", json=payload)

        self.assertEqual(response.status_code, 409)
        self.assertIn("expired", response.get_json()["message"])

    def test_role_marker_refuses_opposite_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "role"
            with patch.dict(os.environ, {"STREAM_NODE_ROLE_FILE": str(marker)}, clear=False):
                write_role_marker("hub")
                with self.assertRaises(RoleConflictError):
                    assert_role("agent")


if __name__ == "__main__":
    unittest.main()
