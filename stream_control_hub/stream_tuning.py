from __future__ import annotations

from typing import Any


# YouTube Live H.264 recommendations, in Kbps. Source:
# https://support.google.com/youtube/answer/2853702
YOUTUBE_H264_LIVE_KBPS = {
    (720, 30): 4000,
    (720, 60): 6000,
    (1080, 30): 10000,
    (1080, 60): 12000,
    (1440, 30): 15000,
    (1440, 60): 24000,
    (2160, 30): 30000,
    (2160, 60): 35000,
}


def parse_fraction(value: Any, default: float = 0.0) -> float:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return float(numerator) / float(denominator) if float(denominator) else default
        return float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def parse_resolution(value: Any, default: tuple[int, int] = (1280, 720)) -> tuple[int, int]:
    text = str(value or "").lower().replace("\u00d7", "x")
    try:
        width, height = (int(part.strip()) for part in text.split("x", 1))
    except (TypeError, ValueError):
        return default
    if width <= 0 or height <= 0:
        return default
    return width, height


def youtube_live_video_bitrate_kbps(width: int, height: int, fps: float) -> int:
    short_side, long_side = sorted((max(1, int(width)), max(1, int(height))))
    rate = 60 if fps > 30 else 30
    if short_side <= 720 and long_side <= 1280:
        return YOUTUBE_H264_LIVE_KBPS[(720, rate)]
    if short_side <= 1080 and long_side <= 1920:
        return YOUTUBE_H264_LIVE_KBPS[(1080, rate)]
    if short_side <= 1440 and long_side <= 2560:
        return YOUTUBE_H264_LIVE_KBPS[(1440, rate)]
    return YOUTUBE_H264_LIVE_KBPS[(2160, rate)]


def youtube_live_bitrate_for_payload(payload: dict[str, Any]) -> int:
    width, height = parse_resolution(payload.get("resolution"))
    return youtube_live_video_bitrate_kbps(width, height, float(payload.get("fps") or 30))


def source_copy_compatible(source: dict[str, Any]) -> bool:
    video_codec = str(source.get("video_codec") or "").lower()
    audio_codec = str(source.get("audio_codec") or "").lower()
    pixel_format = str(source.get("pixel_format") or "").lower()
    return (
        video_codec == "h264"
        and audio_codec in {"", "aac", "mp3"}
        and pixel_format in {"", "yuv420p", "yuvj420p"}
        and 0 < float(source.get("fps") or 0) <= 60
    )


def _fit_source_resolution(width: int, height: int, max_long_side: int) -> tuple[int, int]:
    width = max(2, width)
    height = max(2, height)
    long_side = max(width, height)
    if long_side <= max_long_side:
        return width - width % 2, height - height % 2
    scale = max_long_side / long_side
    return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


def initial_stream_recommendation(
    source: dict[str, Any],
    *,
    cpu_count: int,
    memory_available_mb: int,
    egress_capacity_kbps: int = 0,
) -> dict[str, Any]:
    source_width = max(2, int(source.get("width") or 1280))
    source_height = max(2, int(source.get("height") or 720))
    source_fps = max(15.0, min(60.0, float(source.get("fps") or 30)))
    cpu_count = max(1, int(cpu_count or 1))

    if cpu_count <= 2 or memory_available_mb and memory_available_mb < 1200:
        max_long_side, fps, preset = 1280, min(30, round(source_fps)), "superfast"
    elif cpu_count <= 4:
        max_long_side, fps, preset = 1920, min(30, round(source_fps)), "veryfast"
    elif cpu_count <= 8:
        max_long_side, fps, preset = 1920, round(source_fps), "veryfast"
    else:
        max_long_side, fps, preset = 2560, round(source_fps), "faster"

    width, height = _fit_source_resolution(source_width, source_height, max_long_side)
    fps = max(15, min(60, fps))
    audio_bitrate = 128  # YouTube Live stereo recommendation.
    youtube_bitrate = youtube_live_video_bitrate_kbps(width, height, fps)
    network_budget = max(0, int(egress_capacity_kbps * 0.80) - audio_bitrate) if egress_capacity_kbps else 0
    video_bitrate = min(youtube_bitrate, network_budget) if network_budget else youtube_bitrate
    video_bitrate = max(800, video_bitrate)

    copy_safe = source_copy_compatible(source)
    reasons = [
        f"YouTube Live H.264 baseline for {width}x{height}@{fps} is {youtube_bitrate} Kbps.",
        f"Selected {preset} for {cpu_count} logical CPU(s) and {memory_available_mb or 'unknown'} MB available memory.",
    ]
    warnings: list[str] = []
    if network_budget and network_budget < youtube_bitrate:
        reasons.append(
            f"Capped video bitrate at {video_bitrate} Kbps to keep 20% headroom on measured FFmpeg egress capacity."
        )
    elif not egress_capacity_kbps:
        warnings.append("No prior FFmpeg egress sample is available; validate upload capacity before production use.")
    if egress_capacity_kbps and network_budget < 800:
        warnings.append("Measured upload capacity is below the minimum safe encoder budget; do not start a production stream.")
    if not copy_safe:
        warnings.append("Source is not safe for RTMP copy mode; H.264/AAC transcoding is required.")

    recommendation = {
        "copy_mode": False,
        "preset": preset,
        "video_bitrate": video_bitrate,
        "audio_bitrate": audio_bitrate,
        "fps": fps,
        "resolution": f"{width}x{height}",
        "keyframe_seconds": 2,
        "strategy": "youtube_live_guarded",
    }
    minimum = {
        **recommendation,
        "preset": "superfast",
        "video_bitrate": max(800, min(video_bitrate, int(youtube_bitrate * 0.60))),
        "fps": min(30, fps),
    }
    return {
        "recommendation": recommendation,
        "quality_bounds": {
            "max_quality": {**recommendation, "video_bitrate": youtube_bitrate},
            "min_quality": minimum,
        },
        "analysis": {
            "score": 55 if egress_capacity_kbps and network_budget < 800 else 90 if egress_capacity_kbps else 78,
            "source": source,
            "cpu_count": cpu_count,
            "memory_available_mb": memory_available_mb,
            "network_budget_kbps": network_budget,
            "youtube_recommended_bitrate_kbps": youtube_bitrate,
            "copy_compatible": copy_safe,
            "reasons": reasons,
            "warnings": warnings,
        },
    }
