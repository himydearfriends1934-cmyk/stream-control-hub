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
FPS_FALLBACK_LADDER = (24, 30, 60)
MIN_STREAM_SHORT_SIDE = 720
FULL_HD_SHORT_SIDE = 1080
FULL_HD_MIN_FPS = 24
FULL_HD_MIN_VIDEO_BITRATE_KBPS = 8000
HD_MIN_VIDEO_BITRATE_KBPS = 3200


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


def next_lower_fps(value: Any, *, minimum: int = FULL_HD_MIN_FPS) -> int:
    current = max(15, min(60, int(float(value or 30))))
    minimum = max(15, min(60, int(minimum or FULL_HD_MIN_FPS)))
    lower = [candidate for candidate in FPS_FALLBACK_LADDER if minimum <= candidate < current]
    return max(lower) if lower else current


def lower_stream_resolution(value: Any) -> str:
    width, height = parse_resolution(value)
    short_side = min(width, height)
    if short_side <= MIN_STREAM_SHORT_SIDE:
        return f"{width - width % 2}x{height - height % 2}"
    target_short_side = FULL_HD_SHORT_SIDE if short_side > FULL_HD_SHORT_SIDE else MIN_STREAM_SHORT_SIDE
    scale = target_short_side / short_side
    next_width = max(2, int(width * scale) // 2 * 2)
    next_height = max(2, int(height * scale) // 2 * 2)
    return f"{next_width}x{next_height}"


def minimum_video_bitrate_for_resolution(value: Any) -> int:
    short_side = min(parse_resolution(value))
    if short_side >= FULL_HD_SHORT_SIDE:
        return FULL_HD_MIN_VIDEO_BITRATE_KBPS
    if short_side >= MIN_STREAM_SHORT_SIDE:
        return HD_MIN_VIDEO_BITRATE_KBPS
    return 800


def enforce_youtube_quality_floor(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep YouTube output quality bounded while preserving the adaptive order."""
    result = dict(payload)
    resolution = str(result.get("resolution") or "1280x720").strip().lower() or "1280x720"
    width, height = parse_resolution(resolution)
    resolution = f"{width - width % 2}x{height - height % 2}"
    fps = max(15, min(60, int(float(result.get("fps") or 30))))
    bitrate = max(800, min(40000, int(result.get("video_bitrate") or 4000)))
    short_side = min(parse_resolution(resolution))

    while short_side >= FULL_HD_SHORT_SIDE and bitrate < FULL_HD_MIN_VIDEO_BITRATE_KBPS:
        fallback_resolution = lower_stream_resolution(resolution)
        if fallback_resolution == resolution:
            break
        fallback_fps = min(fps, 30)
        fallback_bitrate = youtube_live_bitrate_for_payload({
            "resolution": fallback_resolution,
            "fps": fallback_fps,
        })
        resolution = fallback_resolution
        fps = fallback_fps
        bitrate = max(
            HD_MIN_VIDEO_BITRATE_KBPS,
            min(bitrate, fallback_bitrate),
        )
        short_side = min(parse_resolution(resolution))

    if short_side >= MIN_STREAM_SHORT_SIDE:
        result["resolution"] = resolution
        result["fps"] = fps
        result["video_bitrate"] = max(
            minimum_video_bitrate_for_resolution(resolution),
            bitrate,
        )
    return result


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
    motion_level: str = "medium",
) -> dict[str, Any]:
    source_width = max(2, int(source.get("width") or 1280))
    source_height = max(2, int(source.get("height") or 720))
    source_fps = max(15.0, min(60.0, float(source.get("fps") or 30)))
    cpu_count = max(1, int(cpu_count or 1))

    if cpu_count <= 2 or memory_available_mb and memory_available_mb < 1200:
        max_long_side, fps, preset = 7680, min(30, round(source_fps)), "superfast"
    elif cpu_count <= 4:
        max_long_side, fps, preset = 7680, min(30, round(source_fps)), "veryfast"
    elif cpu_count <= 8:
        max_long_side, fps, preset = 7680, round(source_fps), "veryfast"
    else:
        max_long_side, fps, preset = 7680, round(source_fps), "faster"

    width, height = _fit_source_resolution(source_width, source_height, max_long_side)
    fps = max(15, min(60, fps))
    audio_bitrate = 128  # YouTube Live stereo recommendation.
    network_budget = max(0, int(egress_capacity_kbps * 0.80) - audio_bitrate) if egress_capacity_kbps else 0
    source_resolution = f"{width}x{height}"
    resolution = source_resolution
    if network_budget:
        while (
            min(parse_resolution(resolution)) >= FULL_HD_SHORT_SIDE
            and network_budget < minimum_video_bitrate_for_resolution(resolution)
        ):
            fallback_resolution = lower_stream_resolution(resolution)
            if fallback_resolution == resolution:
                break
            resolution = fallback_resolution
            fps = min(fps, 30)
    width, height = parse_resolution(resolution)
    youtube_bitrate = youtube_live_video_bitrate_kbps(width, height, fps)
    video_bitrate = min(youtube_bitrate, network_budget) if network_budget else youtube_bitrate
    video_bitrate = max(minimum_video_bitrate_for_resolution(resolution), video_bitrate)

    copy_safe = source_copy_compatible(source)
    motion_level = str(motion_level or "medium").strip().lower()
    if motion_level not in {"static", "medium", "dynamic"}:
        motion_level = "medium"
    # YouTube requires a controlled GOP. Even a compatible static source can
    # carry long or irregular keyframe intervals, so remuxing it with Copy
    # mode cannot satisfy the live encoder contract.
    static_copy_safe = False
    reasons = [
        f"YouTube Live H.264 baseline for {width}x{height}@{fps} is {youtube_bitrate} Kbps.",
        f"Selected {preset} for {cpu_count} logical CPU(s) and {memory_available_mb or 'unknown'} MB available memory; preserve source resolution until runtime pressure requires degradation.",
        f"Motion profile: {motion_level}.",
    ]
    warnings: list[str] = []
    if network_budget and network_budget < youtube_bitrate:
        reasons.append(
            f"Capped video bitrate at {video_bitrate} Kbps to keep 20% headroom on measured FFmpeg egress capacity."
        )
    elif not egress_capacity_kbps:
        warnings.append("No prior FFmpeg egress sample is available; validate upload capacity before production use.")
    if egress_capacity_kbps and network_budget < minimum_video_bitrate_for_resolution(resolution):
        warnings.append(
            "Measured upload capacity is below the minimum safe quality budget; do not start a production stream until the upload path improves."
        )
    elif egress_capacity_kbps and resolution != source_resolution:
        warnings.append(
            f"Measured upload capacity cannot preserve the source quality at 720P ({resolution}); start at this guarded resolution instead."
        )
    if not copy_safe:
        warnings.append("Source is not safe for RTMP copy mode; H.264/AAC transcoding is required.")
    elif motion_level == "static" and not static_copy_safe:
        warnings.append("Static source still uses H.264/AAC transcoding so YouTube receives a controlled two-second GOP.")

    recommendation = {
        "copy_mode": static_copy_safe,
        "preset": "copy" if static_copy_safe else preset,
        "video_bitrate": video_bitrate,
        "audio_bitrate": audio_bitrate,
        "fps": fps,
        "resolution": resolution,
        "keyframe_seconds": 2,
        "strategy": "youtube_live_guarded",
    }
    minimum = {
        **recommendation,
        "copy_mode": False,
        "preset": "superfast",
        "video_bitrate": max(
            minimum_video_bitrate_for_resolution(lower_stream_resolution(recommendation["resolution"])),
            min(
                video_bitrate,
                youtube_live_bitrate_for_payload({
                    "resolution": lower_stream_resolution(recommendation["resolution"]),
                    "fps": min(30, fps),
                }),
            ),
        ),
        "fps": min(30, fps),
        "resolution": lower_stream_resolution(recommendation["resolution"]),
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
            "source_resolution": f"{source_width}x{source_height}",
            "resolution_policy": "preserve-source-until-runtime-pressure",
            "copy_compatible": copy_safe,
            "motion_level": motion_level,
            "static_copy_safe": static_copy_safe,
            "reasons": reasons,
            "warnings": warnings,
        },
    }
