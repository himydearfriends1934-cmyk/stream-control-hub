import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stream_control_hub.stream_tuning import (
    initial_stream_recommendation,
    youtube_live_video_bitrate_kbps,
)


class StreamTuningTests(unittest.TestCase):
    def test_youtube_live_h264_baselines_cover_orientation_and_frame_rate(self):
        self.assertEqual(youtube_live_video_bitrate_kbps(1280, 720, 30), 4000)
        self.assertEqual(youtube_live_video_bitrate_kbps(720, 1280, 30), 4000)
        self.assertEqual(youtube_live_video_bitrate_kbps(1920, 1080, 60), 12000)

    def test_initial_recommendation_keeps_youtube_headroom_on_measured_egress(self):
        result = initial_stream_recommendation(
            {
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "video_codec": "h264",
                "audio_codec": "aac",
                "pixel_format": "yuv420p",
            },
            cpu_count=8,
            memory_available_mb=4096,
            egress_capacity_kbps=6000,
        )

        recommendation = result["recommendation"]
        self.assertEqual(recommendation["resolution"], "1920x1080")
        self.assertEqual(recommendation["video_bitrate"], 4672)
        self.assertEqual(recommendation["audio_bitrate"], 128)
        self.assertEqual(result["analysis"]["youtube_recommended_bitrate_kbps"], 10000)

    def test_low_cpu_recommendation_downscales_without_distorting_vertical_source(self):
        result = initial_stream_recommendation(
            {"width": 1080, "height": 1920, "fps": 60, "video_codec": "vp9", "audio_codec": "opus"},
            cpu_count=2,
            memory_available_mb=1024,
        )

        self.assertEqual(result["recommendation"]["resolution"], "720x1280")
        self.assertEqual(result["recommendation"]["fps"], 30)
        self.assertEqual(result["recommendation"]["preset"], "superfast")
        self.assertFalse(result["analysis"]["copy_compatible"])

    def test_ffmpeg_command_uses_progress_aspect_safe_filter_and_silent_audio(self):
        from stream_control_hub import headless_agent

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"video")
            with patch.object(headless_agent.shutil, "which", return_value="ffmpeg"), patch.object(
                headless_agent, "DATA_DIR", Path(tmp)
            ), patch.object(
                headless_agent,
                "probe_media",
                return_value={"has_audio": False, "video_codec": "h264", "audio_codec": "", "pixel_format": "yuv420p", "fps": 30},
            ):
                command = headless_agent.ffmpeg_command(
                    {"resolution": "720x1280", "fps": 30, "video_bitrate": 4000, "audio_bitrate": 128},
                    video,
                    "rtmps://example.test/live/key",
                )

        self.assertIn("-progress", command)
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=44100", command)
        self.assertIn("scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1", command)
        self.assertEqual(command[command.index("-minrate") + 1], "4000k")

    def test_ffmpeg_network_rate_uses_only_ffmpeg_socket_counter(self):
        from stream_control_hub import headless_agent

        state = {"last_stream_net_sample": {"pid": 42, "at": 1, "bytes_sent": 1000}}
        with patch.object(
            headless_agent,
            "ffmpeg_socket_stats",
            return_value={"bytes_sent": 401000, "delivery_rate_kbps": 10000},
        ):
            result = headless_agent.ffmpeg_network_status(42, state, now=11)

        self.assertEqual(result["upload_bps"], 40000)
        self.assertEqual(result["upload_kbps"], 320.0)
        self.assertEqual(state["stream_egress_capacity_kbps"], 10000)

    def test_socket_stats_extract_delivery_rate_for_owned_ffmpeg_pid(self):
        from stream_control_hub import headless_agent

        output = (
            'ESTAB 0 0 10.0.0.2:40000 1.2.3.4:443 users:(("ffmpeg",pid=42,fd=3))\n'
            ' cubic bytes_sent:1000 bytes_acked:900 delivery_rate 12Mbps\n'
        )
        with patch.object(headless_agent.shutil, "which", return_value="ss"), patch.object(
            headless_agent.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=output),
        ):
            result = headless_agent.ffmpeg_socket_stats(42)

        self.assertEqual(result["bytes_sent"], 1000)
        self.assertEqual(result["delivery_rate_kbps"], 12000)


if __name__ == "__main__":
    unittest.main()
