import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stream_control_hub.env_file import load_env_file, parse_env_assignments, update_env_file_values


class EnvFileTests(unittest.TestCase):
    def test_parse_collapsed_hub_env_values(self):
        values = parse_env_assignments(
            "STREAM_HUB_CONTROL_TOKEN=token123"
            "STREAM_HUB_NODES_FILE=/opt/stream-control-hub/data/nodes.local.json"
            "STREAM_HUB_HOST=100.84.117.8"
            "STREAM_HUB_PORT=8788"
            "STREAM_HUB_TRUSTED_REMOTE_WRITES=1"
            "YOUTUBE_CLIENT_ID=client-id"
            "YOUTUBE_CLIENT_SECRET=client-secret"
        )

        self.assertEqual(values["STREAM_HUB_CONTROL_TOKEN"], "token123")
        self.assertEqual(values["STREAM_HUB_NODES_FILE"], "/opt/stream-control-hub/data/nodes.local.json")
        self.assertEqual(values["STREAM_HUB_HOST"], "100.84.117.8")
        self.assertEqual(values["STREAM_HUB_TRUSTED_REMOTE_WRITES"], "1")
        self.assertEqual(values["YOUTUBE_CLIENT_ID"], "client-id")
        self.assertEqual(values["YOUTUBE_CLIENT_SECRET"], "client-secret")

    def test_load_env_file_accepts_literal_newline_sequences(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "STREAM_HUB_HOST": "already-set",
            },
            clear=False,
        ):
            path = Path(tmp) / ".env"
            path.write_text(
                "STREAM_HUB_CONTROL_TOKEN=token123\\n"
                "STREAM_HUB_HOST=100.84.117.8\\n"
                "STREAM_HUB_TRUSTED_REMOTE_WRITES=1\\n",
                encoding="utf-8",
            )
            os.environ.pop("STREAM_HUB_CONTROL_TOKEN", None)
            os.environ.pop("STREAM_HUB_TRUSTED_REMOTE_WRITES", None)

            load_env_file(path)

            self.assertEqual(os.environ["STREAM_HUB_CONTROL_TOKEN"], "token123")
            self.assertEqual(os.environ["STREAM_HUB_HOST"], "already-set")
            self.assertEqual(os.environ["STREAM_HUB_TRUSTED_REMOTE_WRITES"], "1")

    def test_update_env_file_values_repairs_collapsed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "STREAM_HUB_CONTROL_TOKEN=token123"
                "STREAM_HUB_NODES_FILE=/opt/stream-control-hub/data/nodes.local.json"
                "STREAM_HUB_TRUSTED_REMOTE_WRITES=0"
                "YOUTUBE_CLIENT_ID=old-client\\n"
                "YOUTUBE_CLIENT_SECRET=old-secret",
                encoding="utf-8",
            )

            update_env_file_values(
                path,
                {
                    "STREAM_HUB_TRUSTED_REMOTE_WRITES": "1",
                    "YOUTUBE_CLIENT_ID": "new-client",
                },
            )

            rewritten = path.read_text(encoding="utf-8")
            self.assertNotIn("\\n", rewritten)
            self.assertIn("STREAM_HUB_CONTROL_TOKEN=token123\n", rewritten)
            self.assertIn("STREAM_HUB_NODES_FILE=/opt/stream-control-hub/data/nodes.local.json\n", rewritten)
            self.assertIn("STREAM_HUB_TRUSTED_REMOTE_WRITES=1\n", rewritten)
            self.assertIn("YOUTUBE_CLIENT_ID=new-client\n", rewritten)
            self.assertIn("YOUTUBE_CLIENT_SECRET=old-secret\n", rewritten)


if __name__ == "__main__":
    unittest.main()
