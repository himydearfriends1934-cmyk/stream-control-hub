"""Stream tuning, payload normalization, and adaptive state helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import psutil
from .settings import (
    STREAM_CONFIG_FILE,
    STREAM_ADAPTIVE_CHANGE_COOLDOWN_SECONDS,
    STREAM_ADAPTIVE_DOWNSHIFT_STREAK,
    STREAM_ADAPTIVE_ENABLED,
    STREAM_ADAPTIVE_SHIFT_CONFIRM_SECONDS,
    STREAM_ADAPTIVE_UPSHIFT_COOLDOWN_SECONDS,
    STREAM_ADAPTIVE_UPSHIFT_STREAK,
    STREAM_ADAPTIVE_WARMUP_SECONDS,
    STREAM_FIFO_ENABLED,
    STREAM_FIFO_QUEUE_SIZE,
    STREAM_FIFO_RECOVERY_WAIT_SECONDS,
    STREAM_FIFO_TIMESHIFT_SECONDS,
    STREAM_RELAY_ENABLED,
    STREAM_RELAY_HEALTH_TIMEOUT_SECONDS,
    STREAM_RELAY_LOCAL_URL,
    STREAM_RUNTIME_FILE,
    STREAM_LOG_FILE,
    STREAM_TUNING_FILE,
)
from .state import (
    STREAM_ADAPTIVE_LOCK,
    STREAM_ADAPTIVE_STATE,
    LAST_NET_SNAPSHOT,
    MEDIA_PROBE_CACHE,
    MEDIA_PROBE_LOCK,
    STREAM_RUNTIME_LOCK,
    STREAM_TUNING_LOCK,
)


def parse_resolution(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    match = re.match(r"^\s*(\d{3,5})x(\d{3,5})\s*$", str(value))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def safe_int(value, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def safe_float(value, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def safe_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "auto", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def default_stream_tuning() -> dict:
    return {
        "adaptive_enabled": STREAM_ADAPTIVE_ENABLED,
        "downshift_streak": STREAM_ADAPTIVE_DOWNSHIFT_STREAK,
        "upshift_streak": STREAM_ADAPTIVE_UPSHIFT_STREAK,
        "change_cooldown_seconds": STREAM_ADAPTIVE_CHANGE_COOLDOWN_SECONDS,
        "upshift_cooldown_seconds": STREAM_ADAPTIVE_UPSHIFT_COOLDOWN_SECONDS,
        "warmup_seconds": STREAM_ADAPTIVE_WARMUP_SECONDS,
        "shift_confirm_seconds": STREAM_ADAPTIVE_SHIFT_CONFIRM_SECONDS,
        "severe_cpu_percent": 92.0,
        "pressure_cpu_percent": 82.0,
        "healthy_cpu_percent": 60.0,
        "severe_speed": 0.92,
        "pressure_speed": 0.985,
        "healthy_speed": 1.02,
        "severe_upload_ratio": 0.82,
        "pressure_upload_ratio": 0.94,
        "healthy_upload_ratio": 1.35,
        "startup_severe_cpu_percent": 88.0,
        "startup_pressure_cpu_percent": 72.0,
        "startup_slow_speed": 0.97,
        "fifo_enabled": STREAM_FIFO_ENABLED,
        "fifo_queue_size": STREAM_FIFO_QUEUE_SIZE,
        "fifo_timeshift_seconds": STREAM_FIFO_TIMESHIFT_SECONDS,
        "fifo_recovery_wait_seconds": STREAM_FIFO_RECOVERY_WAIT_SECONDS,
        "relay_enabled": STREAM_RELAY_ENABLED,
        "relay_local_url": STREAM_RELAY_LOCAL_URL,
    }


def sanitize_stream_tuning(payload: dict | None = None) -> dict:
    source = dict(default_stream_tuning())
    if isinstance(payload, dict):
        source.update(payload)
    return {
        "adaptive_enabled": safe_bool(source.get("adaptive_enabled"), STREAM_ADAPTIVE_ENABLED),
        "downshift_streak": safe_int(source.get("downshift_streak"), STREAM_ADAPTIVE_DOWNSHIFT_STREAK, minimum=1, maximum=20),
        "upshift_streak": safe_int(source.get("upshift_streak"), STREAM_ADAPTIVE_UPSHIFT_STREAK, minimum=1, maximum=120),
        "change_cooldown_seconds": safe_int(source.get("change_cooldown_seconds"), STREAM_ADAPTIVE_CHANGE_COOLDOWN_SECONDS, minimum=10, maximum=3600),
        "upshift_cooldown_seconds": safe_int(source.get("upshift_cooldown_seconds"), STREAM_ADAPTIVE_UPSHIFT_COOLDOWN_SECONDS, minimum=30, maximum=7200),
        "warmup_seconds": safe_int(source.get("warmup_seconds"), STREAM_ADAPTIVE_WARMUP_SECONDS, minimum=0, maximum=600),
        "shift_confirm_seconds": safe_int(source.get("shift_confirm_seconds"), STREAM_ADAPTIVE_SHIFT_CONFIRM_SECONDS, minimum=5, maximum=120),
        "severe_cpu_percent": safe_float(source.get("severe_cpu_percent"), 92.0, minimum=50.0, maximum=100.0),
        "pressure_cpu_percent": safe_float(source.get("pressure_cpu_percent"), 82.0, minimum=30.0, maximum=99.0),
        "healthy_cpu_percent": safe_float(source.get("healthy_cpu_percent"), 60.0, minimum=5.0, maximum=95.0),
        "severe_speed": safe_float(source.get("severe_speed"), 0.92, minimum=0.3, maximum=1.2),
        "pressure_speed": safe_float(source.get("pressure_speed"), 0.985, minimum=0.5, maximum=1.5),
        "healthy_speed": safe_float(source.get("healthy_speed"), 1.02, minimum=0.8, maximum=2.0),
        "severe_upload_ratio": safe_float(source.get("severe_upload_ratio"), 0.82, minimum=0.1, maximum=2.0),
        "pressure_upload_ratio": safe_float(source.get("pressure_upload_ratio"), 0.94, minimum=0.1, maximum=2.5),
        "healthy_upload_ratio": safe_float(source.get("healthy_upload_ratio"), 1.35, minimum=0.5, maximum=5.0),
        "startup_severe_cpu_percent": safe_float(source.get("startup_severe_cpu_percent"), 88.0, minimum=50.0, maximum=100.0),
        "startup_pressure_cpu_percent": safe_float(source.get("startup_pressure_cpu_percent"), 72.0, minimum=30.0, maximum=99.0),
        "startup_slow_speed": safe_float(source.get("startup_slow_speed"), 0.97, minimum=0.3, maximum=1.2),
        "fifo_enabled": safe_bool(source.get("fifo_enabled"), STREAM_FIFO_ENABLED),
        "fifo_queue_size": safe_int(source.get("fifo_queue_size"), STREAM_FIFO_QUEUE_SIZE, minimum=64, maximum=32768),
        "fifo_timeshift_seconds": safe_float(source.get("fifo_timeshift_seconds"), STREAM_FIFO_TIMESHIFT_SECONDS, minimum=12.0, maximum=60.0),
        "fifo_recovery_wait_seconds": safe_float(source.get("fifo_recovery_wait_seconds"), STREAM_FIFO_RECOVERY_WAIT_SECONDS, minimum=0.1, maximum=30.0),
        "relay_enabled": safe_bool(source.get("relay_enabled"), STREAM_RELAY_ENABLED),
        "relay_local_url": str(source.get("relay_local_url") or STREAM_RELAY_LOCAL_URL).strip() or STREAM_RELAY_LOCAL_URL,
    }


def load_stream_tuning() -> dict:
    data = {}
    if STREAM_TUNING_FILE.exists():
        try:
            parsed = json.loads(STREAM_TUNING_FILE.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
    return sanitize_stream_tuning(data)


def save_stream_tuning(payload: dict) -> dict:
    tuning = sanitize_stream_tuning(payload)
    with STREAM_TUNING_LOCK:
        STREAM_TUNING_FILE.parent.mkdir(parents=True, exist_ok=True)
        STREAM_TUNING_FILE.write_text(json.dumps(tuning, ensure_ascii=False, indent=2), encoding="utf-8")
    return tuning


def load_stream_config() -> dict | None:
    if not STREAM_CONFIG_FILE.exists():
        return None
    try:
        data = json.loads(STREAM_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_stream_config(payload: dict) -> None:
    STREAM_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    STREAM_CONFIG_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_stream_runtime_state() -> dict:
    if not STREAM_RUNTIME_FILE.exists():
        return {"desired_state": "running", "manual_stop": False, "updated_at": 0.0}
    try:
        data = json.loads(STREAM_RUNTIME_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"desired_state": "running", "manual_stop": False, "updated_at": 0.0}
    if not isinstance(data, dict):
        return {"desired_state": "running", "manual_stop": False, "updated_at": 0.0}
    desired_state = str(data.get("desired_state") or "").strip().lower()
    manual_stop = bool(data.get("manual_stop")) or desired_state == "manual_stop"
    return {
        "desired_state": "manual_stop" if manual_stop else "running",
        "manual_stop": manual_stop,
        "updated_at": safe_float(data.get("updated_at"), 0.0, minimum=0.0),
        "reason": str(data.get("reason") or "").strip(),
    }


def save_stream_runtime_state(*, manual_stop: bool, reason: str = "") -> dict:
    state = {
        "desired_state": "manual_stop" if manual_stop else "running",
        "manual_stop": bool(manual_stop),
        "updated_at": time.time(),
        "reason": str(reason or "").strip(),
    }
    with STREAM_RUNTIME_LOCK:
        STREAM_RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
        STREAM_RUNTIME_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def stream_config_public_view(config: dict | None) -> dict:
    config = config or {}
    public_config = {
        key: config.get(key)
        for key in (
            "stream_url",
            "video_path",
            "copy_mode",
            "preset",
            "video_bitrate",
            "audio_bitrate",
            "fps",
            "resolution",
            "keyframe_seconds",
            "adaptive_mode",
            "stream_output_mode",
        )
        if key in config
    }
    public_config["has_stream_key"] = bool(config.get("stream_key"))
    return public_config


def normalize_stream_payload(payload: dict | None) -> dict:
    data = dict(payload or {})
    default_output_mode = "local_relay" if load_stream_tuning().get("relay_enabled") else "direct"
    normalized = {
        "stream_url": str(data.get("stream_url", "")).strip().rstrip("/"),
        "stream_key": str(data.get("stream_key", "")).strip(),
        "video_path": str(data.get("video_path", "")).strip(),
        "copy_mode": bool(data.get("copy_mode")),
        "preset": str(data.get("preset", "veryfast")).strip() or "veryfast",
        "video_bitrate": safe_int(data.get("video_bitrate", 4500), 4500, minimum=800),
        "audio_bitrate": safe_int(data.get("audio_bitrate", 192), 192, minimum=64),
        "fps": safe_int(data.get("fps", 30), 30, minimum=15, maximum=60),
        "resolution": str(data.get("resolution", "1280x720")).strip() or "1280x720",
        "keyframe_seconds": safe_int(data.get("keyframe_seconds", 2), 2, minimum=1, maximum=4),
        "adaptive_mode": str(data.get("adaptive_mode") or "auto").strip().lower() or "auto",
        "stream_output_mode": str(data.get("stream_output_mode") or default_output_mode).strip().lower() or default_output_mode,
    }
    if normalized["adaptive_mode"] not in {"auto", "off"}:
        normalized["adaptive_mode"] = "auto"
    if normalized["stream_output_mode"] not in {"direct", "local_relay"}:
        normalized["stream_output_mode"] = default_output_mode
    if normalized["copy_mode"]:
        normalized["preset"] = "copy"
    return normalized


def stream_payload_signature(payload: dict | None, *, include_secrets: bool = False) -> tuple:
    normalized = normalize_stream_payload(payload)
    parts = (
        normalized.get("stream_url", ""),
        normalized.get("video_path", ""),
        normalized.get("copy_mode", False),
        normalized.get("preset", ""),
        normalized.get("video_bitrate", 0),
        normalized.get("audio_bitrate", 0),
        normalized.get("fps", 0),
        normalized.get("resolution", ""),
        normalized.get("keyframe_seconds", 0),
        normalized.get("adaptive_mode", "auto"),
    )
    if include_secrets:
        return parts + (normalized.get("stream_key", ""),)
    return parts


def adaptive_state_snapshot() -> dict:
    with STREAM_ADAPTIVE_LOCK:
        state = dict(STREAM_ADAPTIVE_STATE)
    for key in ("current_target", "recommended_target", "last_metrics"):
        state[key] = dict(state.get(key) or {})
    return state


def update_adaptive_state(**kwargs) -> None:
    with STREAM_ADAPTIVE_LOCK:
        STREAM_ADAPTIVE_STATE.update(kwargs)


def get_current_net_rates(net) -> dict:
    now = time.time()
    last_ts = LAST_NET_SNAPSHOT["ts"]
    last_sent = LAST_NET_SNAPSHOT["bytes_sent"]
    last_recv = LAST_NET_SNAPSHOT["bytes_recv"]

    upload_bps = 0.0
    download_bps = 0.0
    if last_ts is not None and last_sent is not None and last_recv is not None:
        elapsed = max(now - last_ts, 0.001)
        upload_bps = max(0.0, (net.bytes_sent - last_sent) / elapsed)
        download_bps = max(0.0, (net.bytes_recv - last_recv) / elapsed)

    LAST_NET_SNAPSHOT["ts"] = now
    LAST_NET_SNAPSHOT["bytes_sent"] = net.bytes_sent
    LAST_NET_SNAPSHOT["bytes_recv"] = net.bytes_recv

    return {
        "upload_bps": upload_bps,
        "download_bps": download_bps,
        "combined_bps": upload_bps + download_bps,
    }


def read_current_stream_bitrate_kbps() -> float | None:
    if not STREAM_LOG_FILE.exists():
        return None
    try:
        with open(STREAM_LOG_FILE, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536), os.SEEK_SET)
            chunk = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return None

    matches = re.findall(r"bitrate=\s*([0-9]+(?:\.[0-9]+)?)kbits/s", chunk, flags=re.IGNORECASE)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def read_recent_ffmpeg_speed() -> float | None:
    if not STREAM_LOG_FILE.exists():
        return None
    try:
        with open(STREAM_LOG_FILE, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 131072), os.SEEK_SET)
            chunk = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return None

    matches = re.findall(r"speed=\s*([0-9]+(?:\.[0-9]+)?)x", chunk, flags=re.IGNORECASE)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def read_recent_ffmpeg_position_seconds() -> float | None:
    if not STREAM_LOG_FILE.exists():
        return None
    try:
        with open(STREAM_LOG_FILE, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 131072), os.SEEK_SET)
            chunk = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return None

    matches = re.findall(
        r"time=\s*([0-9]{2}):([0-9]{2}):([0-9]{2}(?:\.[0-9]+)?)",
        chunk,
        flags=re.IGNORECASE,
    )
    if not matches:
        return None
    hh, mm, ss = matches[-1]
    try:
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except ValueError:
        return None


def ffprobe_video_info(video_path: str) -> dict:
    path = Path(video_path) if video_path else None
    if not path or not path.exists() or not shutil.which("ffprobe"):
        return {}
    try:
        stat = path.stat()
        cache_key = f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        cache_key = ""
    if cache_key:
        with MEDIA_PROBE_LOCK:
            cached = MEDIA_PROBE_CACHE.get(cache_key)
            if cached is not None:
                return dict(cached)
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type,width,height,avg_frame_rate,r_frame_rate,bit_rate,codec_name,sample_rate,channels,duration:format=duration",
                "-of",
                "json",
                str(path),
            ],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    streams = data.get("streams") or []
    format_info = data.get("format") or {}
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video_stream:
        return {}

    def parse_rate(value: str | None) -> float | None:
        if not value or value == "0/0":
            return None
        if "/" in value:
            left, right = value.split("/", 1)
            try:
                denom = float(right)
                return float(left) / denom if denom else None
            except ValueError:
                return None
        try:
            return float(value)
        except ValueError:
            return None

    keyframe_times: list[float] = []
    try:
        key_result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-skip_frame",
                "nokey",
                "-read_intervals",
                "%+90",
                "-show_entries",
                "frame=best_effort_timestamp_time,pkt_pts_time",
                "-of",
                "csv=p=0",
                str(path),
            ],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if key_result.returncode == 0:
            for line in key_result.stdout.splitlines():
                parts = [p for p in line.split(",") if p]
                if not parts:
                    continue
                try:
                    keyframe_times.append(float(parts[0]))
                except ValueError:
                    continue
    except (OSError, subprocess.TimeoutExpired):
        keyframe_times = []

    intervals = [
        round(keyframe_times[idx] - keyframe_times[idx - 1], 3)
        for idx in range(1, len(keyframe_times))
        if keyframe_times[idx] > keyframe_times[idx - 1]
    ]
    keyframe_seconds = None
    max_keyframe_seconds = None
    if intervals:
        sorted_intervals = sorted(intervals)
        keyframe_seconds = sorted_intervals[len(sorted_intervals) // 2]
        max_keyframe_seconds = max(intervals)

    info = {
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "fps": parse_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
        "bitrate_kbps": (
            int(video_stream["bit_rate"]) / 1000
            if str(video_stream.get("bit_rate") or "").isdigit()
            else None
        ),
        "codec": video_stream.get("codec_name"),
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
        "audio_sample_rate": int(audio_stream["sample_rate"]) if audio_stream and str(audio_stream.get("sample_rate") or "").isdigit() else None,
        "audio_channels": audio_stream.get("channels") if audio_stream else None,
        "duration_seconds": parse_rate(video_stream.get("duration") or format_info.get("duration")),
        "keyframe_seconds": keyframe_seconds,
        "max_keyframe_seconds": max_keyframe_seconds,
    }
    if cache_key:
        with MEDIA_PROBE_LOCK:
            if len(MEDIA_PROBE_CACHE) > 32:
                MEDIA_PROBE_CACHE.clear()
            MEDIA_PROBE_CACHE[cache_key] = dict(info)
    return info


YOUTUBE_H264_BITRATES = {
    (2160, 60): 35000,
    (2160, 30): 30000,
    (1440, 60): 24000,
    (1440, 30): 15000,
    (1080, 60): 12000,
    (1080, 30): 10000,
    (720, 60): 6000,
    (720, 30): 4000,
    (540, 30): 2500,
    (480, 30): 1800,
    (360, 30): 1000,
}
STANDARD_HEIGHTS = [2160, 1440, 1080, 900, 720, 576, 540, 480, 360, 240]
PRESET_ORDER = ["faster", "veryfast", "ultrafast"]
SNAP_FPS_VALUES = [60, 50, 48, 30, 25, 24, 20, 15]


def bitrate_for_height(height: int, fps: int) -> int:
    fps_bucket = 60 if fps > 30 else 30
    for candidate_height in STANDARD_HEIGHTS:
        if height >= candidate_height:
            return YOUTUBE_H264_BITRATES.get((candidate_height, fps_bucket), 3000)
    return 800


def dimensions_for_height(source_width: int, source_height: int, height: int) -> tuple[int, int]:
    height = min(max(240, int(height)), source_height)
    if height >= source_height:
        return source_width, source_height
    width = int(round(source_width * height / max(1, source_height) / 2) * 2)
    return max(426, min(width, source_width)), max(240, height)


def next_lower_height(height: int, floor_height: int) -> int:
    for candidate in STANDARD_HEIGHTS:
        if candidate < height and candidate >= floor_height:
            return candidate
    return floor_height


def snap_fps(value: int | float, *, ceiling: int) -> int:
    ceiling = max(15, int(ceiling))
    value = max(15, min(int(round(value)), ceiling))
    for candidate in SNAP_FPS_VALUES:
        if candidate <= value and candidate <= ceiling:
            return candidate
    return min(ceiling, 15)


def preset_rank(value: str) -> int:
    try:
        return PRESET_ORDER.index(value)
    except ValueError:
        return PRESET_ORDER.index("veryfast")


def interpolate_preset(start: str, end: str, fraction: float) -> str:
    left = preset_rank(start)
    right = preset_rank(end)
    target = left + (right - left) * max(0.0, min(1.0, fraction))
    return PRESET_ORDER[int(round(target))]


def stream_target_view(payload: dict | None) -> dict:
    normalized = normalize_stream_payload(payload)
    target = {
        "copy_mode": normalized["copy_mode"],
        "preset": normalized["preset"],
        "video_bitrate": normalized["video_bitrate"],
        "audio_bitrate": normalized["audio_bitrate"],
        "fps": normalized["fps"],
        "resolution": normalized["resolution"],
        "keyframe_seconds": normalized["keyframe_seconds"],
    }
    strategy = str((payload or {}).get("strategy") or "").strip()
    if strategy:
        target["strategy"] = strategy
    label = str((payload or {}).get("label") or "").strip()
    if label:
        target["label"] = label
    return target


def target_total_bitrate_kbps(target: dict | None) -> int:
    target = target or {}
    return int(target.get("video_bitrate") or 0) + int(target.get("audio_bitrate") or 0)


def target_quality_key(target: dict | None) -> tuple:
    target = target or {}
    _, height = parse_resolution(target.get("resolution")) or (0, 0)
    return (
        1 if target.get("copy_mode") else 0,
        height,
        int(target.get("fps") or 0),
        int(target.get("video_bitrate") or 0),
        int(target.get("audio_bitrate") or 0),
        -preset_rank(str(target.get("preset") or "veryfast")),
    )


def format_seek_seconds(value: float) -> str:
    safe = max(0.0, float(value or 0.0))
    return f"{safe:.3f}".rstrip("0").rstrip(".") or "0"


def compute_resume_position_seconds(payload: dict | None) -> float:
    payload = payload or {}
    requested = payload.get("resume_position_seconds")
    if requested not in (None, ""):
        try:
            return max(0.0, float(requested))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def derive_resume_position_seconds(source: dict | None) -> float:
    position = read_recent_ffmpeg_position_seconds()
    if position is None:
        return 0.0
    tuning = load_stream_tuning()
    resume_at = max(0.0, position - max(0.0, float(tuning.get("fifo_timeshift_seconds") or 0.0)))
    duration = float((source or {}).get("duration_seconds") or 0.0)
    if duration > 1.0:
        resume_at %= duration
    return resume_at


def stream_output_url(payload: dict | None) -> str:
    normalized = normalize_stream_payload(payload)
    tuning = load_stream_tuning()
    if normalized.get("stream_output_mode") == "local_relay":
        return str(tuning.get("relay_local_url") or STREAM_RELAY_LOCAL_URL).strip()
    return f"{normalized['stream_url']}/{normalized['stream_key']}"


def stream_relay_status(config: dict | None = None) -> dict:
    tuning = load_stream_tuning()
    normalized = normalize_stream_payload(config or {})
    enabled = normalized.get("stream_output_mode") == "local_relay" or bool(tuning.get("relay_enabled"))
    local_url = str(tuning.get("relay_local_url") or STREAM_RELAY_LOCAL_URL).strip()
    parsed = urlparse(local_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "rtmps" else 1935)
    status = {
        "enabled": enabled,
        "mode": normalized.get("stream_output_mode", "direct"),
        "local_url": local_url,
        "host": host,
        "port": port,
        "reachable": False,
        "message": "local relay disabled",
    }
    if not enabled:
        return status
    try:
        with socket.create_connection((host, port), timeout=STREAM_RELAY_HEALTH_TIMEOUT_SECONDS):
            status["reachable"] = True
            status["message"] = "local relay port reachable"
    except OSError as exc:
        status["message"] = f"local relay port is not reachable: {exc.__class__.__name__}"
    return status


def build_transcode_target(
    *,
    source_width: int,
    source_height: int,
    height: int,
    fps: int,
    video_bitrate: int,
    audio_bitrate: int,
    preset: str,
    keyframe_seconds: int = 2,
    label: str = "",
) -> dict:
    width, height = dimensions_for_height(source_width, source_height, height)
    target = stream_target_view({
        "copy_mode": False,
        "preset": preset,
        "video_bitrate": video_bitrate,
        "audio_bitrate": audio_bitrate,
        "fps": fps,
        "resolution": f"{width}x{height}",
        "keyframe_seconds": keyframe_seconds,
        "strategy": "transcode",
        "label": label,
    })
    target["strategy"] = "transcode"
    if label:
        target["label"] = label
    return target


def build_copy_target(
    *,
    source_width: int,
    source_height: int,
    source_fps: int,
    source_bitrate_kbps: int,
    source_keyframe_seconds: int,
    audio_bitrate: int,
    label: str = "",
) -> dict:
    target = stream_target_view({
        "copy_mode": True,
        "preset": "copy",
        "video_bitrate": max(800, int(source_bitrate_kbps)),
        "audio_bitrate": max(64, int(audio_bitrate)),
        "fps": source_fps,
        "resolution": f"{source_width}x{source_height}",
        "keyframe_seconds": max(1, min(4, int(source_keyframe_seconds))),
        "strategy": "copy",
        "label": label,
    })
    target["strategy"] = "copy"
    if label:
        target["label"] = label
    return target


def append_unique_target(targets: list[dict], target: dict) -> None:
    signature = stream_payload_signature(target)
    if any(stream_payload_signature(item) == signature for item in targets):
        return
    targets.append(target)


def build_adaptive_ladder(
    *,
    max_target: dict,
    min_target: dict,
    source_width: int,
    source_height: int,
    transcode_ceiling: dict | None = None,
) -> list[dict]:
    ladder: list[dict] = []
    append_unique_target(ladder, max_target)

    if max_target.get("copy_mode") and transcode_ceiling:
        append_unique_target(ladder, transcode_ceiling)

    top_transcode = stream_target_view(transcode_ceiling or max_target)
    if top_transcode.get("copy_mode"):
        return ladder

    top_resolution = parse_resolution(top_transcode.get("resolution")) or (source_width, source_height)
    bottom_resolution = parse_resolution(min_target.get("resolution")) or top_resolution
    top_height = top_resolution[1]
    bottom_height = bottom_resolution[1]

    height_candidates = [top_height]
    for candidate in STANDARD_HEIGHTS:
        if bottom_height < candidate < top_height:
            height_candidates.append(candidate)
    height_candidates.append(bottom_height)

    if len(height_candidates) > 4:
        step = (len(height_candidates) - 1) / 3
        height_candidates = [
            height_candidates[0],
            height_candidates[int(round(step))],
            height_candidates[int(round(step * 2))],
            height_candidates[-1],
        ]

    top_video = int(top_transcode.get("video_bitrate") or 0)
    bottom_video = int(min_target.get("video_bitrate") or top_video)
    top_audio = int(top_transcode.get("audio_bitrate") or 160)
    bottom_audio = int(min_target.get("audio_bitrate") or top_audio)
    top_fps = int(top_transcode.get("fps") or 30)
    bottom_fps = int(min_target.get("fps") or top_fps)

    count = max(2, len(height_candidates))
    for index, height in enumerate(height_candidates):
        if index == 0:
            continue
        fraction = index / (count - 1)
        fps = snap_fps(top_fps - (top_fps - bottom_fps) * fraction, ceiling=top_fps)
        video_bitrate = int(round(top_video - (top_video - bottom_video) * fraction))
        guide = bitrate_for_height(height, fps)
        video_bitrate = min(video_bitrate, guide if guide >= bottom_video else video_bitrate)
        video_bitrate = max(bottom_video, video_bitrate)
        audio_bitrate = int(round(top_audio - (top_audio - bottom_audio) * fraction))
        audio_bitrate = max(bottom_audio, min(top_audio, audio_bitrate))
        preset = interpolate_preset(
            str(top_transcode.get("preset") or "veryfast"),
            str(min_target.get("preset") or "ultrafast"),
            fraction,
        )
        target = build_transcode_target(
            source_width=source_width,
            source_height=source_height,
            height=height,
            fps=fps,
            video_bitrate=video_bitrate,
            audio_bitrate=audio_bitrate,
            preset=preset,
            keyframe_seconds=int(min_target.get("keyframe_seconds") or top_transcode.get("keyframe_seconds") or 2),
            label="adaptive-step",
        )
        append_unique_target(ladder, target)

    append_unique_target(ladder, min_target)
    return ladder


def find_target_index(ladder: list[dict], payload: dict | None) -> int:
    signature = stream_payload_signature(payload)
    for index, item in enumerate(ladder):
        if stream_payload_signature(item) == signature:
            return index
    return max(0, len(ladder) - 1)


def select_runtime_target(
    *,
    bounds: dict,
    ladder: list[dict],
    analysis: dict,
    current_payload: dict,
    tuning: dict | None = None,
) -> tuple[int, dict, str]:
    tuning = sanitize_stream_tuning(tuning)
    max_target = bounds.get("max_quality") or {}
    min_target = bounds.get("min_quality") or {}
    current_total = int(current_payload.get("video_bitrate") or 0) + int(current_payload.get("audio_bitrate") or 0)
    current_index = find_target_index(ladder, current_payload)
    cpu_percent = float(analysis.get("cpu_percent") or 0.0)
    ffmpeg_speed = analysis.get("ffmpeg_speed")
    upload_kbps = analysis.get("current_upload_kbps")
    network_budget_kbps = analysis.get("network_budget_kbps")
    recommended_index = 0
    reason = "inside calibrated quality range"

    severe_overload = (
        cpu_percent >= float(tuning["severe_cpu_percent"])
        or (ffmpeg_speed is not None and ffmpeg_speed < float(tuning["severe_speed"]))
        or (
            upload_kbps is not None
            and current_total > 0
            and upload_kbps < current_total * float(tuning["severe_upload_ratio"])
        )
    )
    mild_pressure = (
        cpu_percent >= float(tuning["pressure_cpu_percent"])
        or (ffmpeg_speed is not None and ffmpeg_speed < float(tuning["pressure_speed"]))
        or (
            upload_kbps is not None
            and current_total > 0
            and upload_kbps < current_total * float(tuning["pressure_upload_ratio"])
        )
    )
    healthy_headroom = (
        cpu_percent <= float(tuning["healthy_cpu_percent"])
        and (ffmpeg_speed is None or ffmpeg_speed >= float(tuning["healthy_speed"]))
        and (
            upload_kbps is None
            or current_total <= 0
            or upload_kbps > current_total * float(tuning["healthy_upload_ratio"])
        )
        and (
            network_budget_kbps is None
            or target_total_bitrate_kbps(max_target) <= network_budget_kbps
        )
    )

    if severe_overload:
        recommended_index = min(len(ladder) - 1, current_index + 2)
        reason = "runtime pressure is severe; move down two guarded ladder steps"
    elif mild_pressure:
        recommended_index = min(len(ladder) - 1, current_index + 1)
        reason = "runtime pressure is elevated; move down one guarded ladder step"
    elif healthy_headroom and current_index > 0:
        recommended_index = current_index - 1
        reason = "runtime headroom is healthy; recover one guarded ladder step"
    else:
        recommended_index = current_index

    recommended_target = ladder[recommended_index] if ladder else min_target or max_target
    return recommended_index, recommended_target, reason


def recommend_stream_settings(payload: dict | None = None) -> dict:
    current = load_stream_config() or {}
    if payload:
        current.update({k: v for k, v in payload.items() if v not in (None, "")})
    current = normalize_stream_payload(current)
    tuning = load_stream_tuning()

    cpu_count = psutil.cpu_count() or 1
    cpu_percent = psutil.cpu_percent(interval=0.2)
    memory = psutil.virtual_memory()
    net = psutil.net_io_counters()
    net_rates = get_current_net_rates(net)
    current_bitrate = read_current_stream_bitrate_kbps()
    ffmpeg_speed = read_recent_ffmpeg_speed()
    source = ffprobe_video_info(str(current.get("video_path", "")).strip())

    source_width = int(source.get("width") or 1280)
    source_height = int(source.get("height") or 720)
    source_fps = snap_fps(
        source.get("fps") or current.get("fps") or 24,
        ceiling=60,
    )
    reasons: list[str] = []
    warnings: list[str] = []
    copy_reasons: list[str] = []

    def copy_eligible() -> tuple[bool, list[str]]:
        checks = []
        eligible = True
        if source.get("codec") != "h264":
            eligible = False
            checks.append("copy rejected: video codec is not H.264.")
        if source.get("audio_codec") not in ("aac", "mp3"):
            eligible = False
            checks.append("copy rejected: audio is not AAC/MP3.")
        if source_fps > 60:
            eligible = False
            checks.append("copy rejected: FPS exceeds YouTube live guidance.")
        max_keyframe = source.get("max_keyframe_seconds")
        if not max_keyframe:
            eligible = False
            checks.append("copy rejected: source keyframe interval is unknown.")
        elif max_keyframe > 4.0:
            eligible = False
            checks.append(f"copy rejected: source keyframes reach {max_keyframe:.1f}s; YouTube needs <=4s.")
        elif max_keyframe > 2.5:
            checks.append(f"copy allowed only as guarded upper bound: keyframes are {max_keyframe:.1f}s, above the 2s ideal.")
        else:
            checks.append("copy candidate: source H.264/AAC and keyframes are close to 2s.")
        return eligible, checks

    copy_ok, copy_reasons = copy_eligible()

    upload_kbps_raw = net_rates["upload_bps"] * 8 / 1000
    upload_kbps = round(upload_kbps_raw) if upload_kbps_raw >= 128 else None
    network_budget_kbps = None
    if upload_kbps and upload_kbps >= 800:
        network_budget_kbps = int(upload_kbps * 0.78)
        reasons.append(f"实时可见上传吞吐约 {upload_kbps} kbps，启动质量会保守限制在该带宽预算内。")
    elif current_bitrate and current_bitrate >= 800:
        network_budget_kbps = int(current_bitrate * 1.15 + int(current.get("audio_bitrate") or 160))
        reasons.append("未采到明显的实时上传样本，先用当前推流码率反推可用网络预算。")
    else:
        warnings.append("启动前没有足够的实时上传样本，网络上限先按保守默认处理，开播后再自适应校正。")

    relay = stream_relay_status(current)
    if current.get("stream_output_mode") == "local_relay" and not relay.get("reachable"):
        warnings.append(f"Local relay mode is selected but the relay port is not reachable: {relay.get('local_url')}.")

    ceiling_height = source_height
    if cpu_count <= 1:
        ceiling_height = min(ceiling_height, 720)
        max_preset = "ultrafast"
        reasons.append("1-core VPS: highest stable profile is capped to 720p ultrafast.")
    elif cpu_count <= 2:
        ceiling_height = min(ceiling_height, 1080)
        max_preset = "veryfast" if cpu_percent < 55 else "ultrafast"
        reasons.append("Small VPS: highest stable profile is limited to 1080p or below.")
    elif cpu_count <= 6:
        ceiling_height = min(ceiling_height, 1080)
        max_preset = "veryfast"
        reasons.append("Mid-size VPS: highest stable profile can stay within 1080p.")
    else:
        ceiling_height = min(ceiling_height, 1440)
        max_preset = "faster" if cpu_percent < 40 else "veryfast"
        reasons.append("Larger VPS: quality ceiling can extend to 1440p when source allows it.")

    if memory.total < 1500 * 1024 ** 2:
        ceiling_height = min(ceiling_height, 720)
        warnings.append("内存低于 1.5GB，已下压最高质量上限，避免高峰期抖动。")
    elif memory.total < 3000 * 1024 ** 2:
        ceiling_height = min(ceiling_height, 1080)

    if cpu_percent >= float(tuning["startup_severe_cpu_percent"]):
        ceiling_height = next_lower_height(ceiling_height, 360)
        max_preset = "ultrafast"
        warnings.append(f"当前 CPU {cpu_percent:.0f}% 偏高，最高质量上限已主动下调一档。")
    elif cpu_percent >= float(tuning["startup_pressure_cpu_percent"]):
        max_preset = interpolate_preset(max_preset, "ultrafast", 0.7)
        warnings.append(f"当前 CPU {cpu_percent:.0f}% 较高，最高质量编码 preset 已偏向低负载。")

    if ffmpeg_speed is not None and ffmpeg_speed < float(tuning["startup_slow_speed"]):
        ceiling_height = next_lower_height(ceiling_height, 360)
        max_preset = "ultrafast"
        warnings.append(f"最近 FFmpeg speed={ffmpeg_speed:.2f}x，最高质量上限再下调一档，保证实时性。")

    if source_fps > 30 and cpu_count <= 2:
        ceiling_fps = 30
        reasons.append("High-FPS source on a small VPS: quality ceiling keeps output at 30fps.")
    else:
        ceiling_fps = min(source_fps, 60)
    if cpu_count <= 1 and ceiling_fps > 24:
        ceiling_fps = 24
    ceiling_fps = snap_fps(ceiling_fps, ceiling=source_fps)

    floor_height = 360
    if ceiling_height >= 1440:
        floor_height = 720
    elif ceiling_height >= 1080:
        floor_height = 540
    elif ceiling_height >= 720:
        floor_height = 480
    elif ceiling_height >= 540:
        floor_height = 360
    floor_height = min(floor_height, ceiling_height)

    floor_fps = 24 if source_fps >= 24 else source_fps
    if floor_height <= 540 and floor_fps > 30:
        floor_fps = 30
    floor_fps = snap_fps(floor_fps, ceiling=ceiling_fps)

    max_audio_bitrate = 192 if ceiling_height >= 1080 else 160
    min_audio_bitrate = 128 if floor_height <= 540 else 160

    max_video_bitrate = bitrate_for_height(ceiling_height, ceiling_fps)
    if cpu_count <= 1:
        max_video_bitrate = min(max_video_bitrate, 3000)
    elif cpu_count <= 2:
        max_video_bitrate = min(max_video_bitrate, 4500)
    if memory.total < 1500 * 1024 ** 2:
        max_video_bitrate = min(max_video_bitrate, 3000)
    if network_budget_kbps:
        max_video_bitrate = min(max_video_bitrate, max(1200, network_budget_kbps - max_audio_bitrate))
    max_video_bitrate = max(1200, int(max_video_bitrate))

    min_video_bitrate = min(
        bitrate_for_height(floor_height, floor_fps),
        max_video_bitrate,
    )
    if network_budget_kbps:
        min_video_bitrate = min(min_video_bitrate, max(900, int((network_budget_kbps - min_audio_bitrate) * 0.72)))
    min_video_bitrate = max(900, int(min(min_video_bitrate, max_video_bitrate)))
    if max_video_bitrate - min_video_bitrate < 500 and max_video_bitrate > 1600:
        min_video_bitrate = max(900, int(max_video_bitrate * 0.78))

    max_transcode_target = build_transcode_target(
        source_width=source_width,
        source_height=source_height,
        height=ceiling_height,
        fps=ceiling_fps,
        video_bitrate=max_video_bitrate,
        audio_bitrate=max_audio_bitrate,
        preset=max_preset,
        keyframe_seconds=2,
        label="max-quality",
    )
    min_transcode_target = build_transcode_target(
        source_width=source_width,
        source_height=source_height,
        height=floor_height,
        fps=floor_fps,
        video_bitrate=min_video_bitrate,
        audio_bitrate=min_audio_bitrate,
        preset="ultrafast",
        keyframe_seconds=2,
        label="min-quality",
    )

    source_bitrate = int(source.get("bitrate_kbps") or current_bitrate or max_video_bitrate)
    copy_target = build_copy_target(
        source_width=source_width,
        source_height=source_height,
        source_fps=source_fps,
        source_bitrate_kbps=source_bitrate,
        source_keyframe_seconds=int(round(source.get("keyframe_seconds") or 2)),
        audio_bitrate=int(current.get("audio_bitrate") or max_audio_bitrate),
        label="max-quality-copy",
    )

    copy_allowed_as_ceiling = False
    copy_total = target_total_bitrate_kbps(copy_target)
    transcode_total = target_total_bitrate_kbps(max_transcode_target)
    if copy_ok:
        if network_budget_kbps and copy_total > network_budget_kbps:
            warnings.append("源视频直推码率高于当前稳定网络预算，最高质量上限改为转码上限。")
        elif copy_total > transcode_total * 1.45:
            warnings.append("源视频原码率显著高于当前稳定上限，最高质量上限改为受控转码。")
        else:
            copy_allowed_as_ceiling = True
            reasons.append("源视频满足 YouTube 直播直推条件，最高质量上限采用 copy 直推。")

    max_quality_target = copy_target if copy_allowed_as_ceiling else max_transcode_target
    if not copy_allowed_as_ceiling:
        reasons.append("最高质量上限采用受控转码，保证清晰度和稳定性同时成立。")

    adaptive_ladder = build_adaptive_ladder(
        max_target=max_quality_target,
        min_target=min_transcode_target,
        source_width=source_width,
        source_height=source_height,
        transcode_ceiling=max_transcode_target,
    )

    warnings.extend(copy_reasons)

    score = 100
    if warnings:
        score -= min(35, len(warnings) * 10)
    if cpu_percent > float(tuning["pressure_cpu_percent"]):
        score -= 10
    if ffmpeg_speed is not None and ffmpeg_speed < float(tuning["pressure_speed"]):
        score -= 10
    if network_budget_kbps and target_total_bitrate_kbps(max_quality_target) > network_budget_kbps * 0.92:
        score -= 8

    return {
        "recommendation": max_quality_target,
        "quality_bounds": {
            "max_quality": max_quality_target,
            "min_quality": min_transcode_target,
            "transcode_ceiling": max_transcode_target,
        },
        "adaptive_ladder": adaptive_ladder,
        "analysis": {
            "score": max(35, score),
            "cpu_count": cpu_count,
            "cpu_percent": cpu_percent,
            "memory_total_mb": round(memory.total / 1024 / 1024),
            "memory_available_mb": round(memory.available / 1024 / 1024),
            "current_stream_bitrate_kbps": current_bitrate,
            "current_upload_kbps": upload_kbps,
            "network_budget_kbps": network_budget_kbps,
            "ffmpeg_speed": ffmpeg_speed,
            "source": source,
            "reasons": reasons,
            "warnings": warnings,
        },
    }


__all__ = [
    "adaptive_state_snapshot",
    "append_unique_target",
    "bitrate_for_height",
    "build_adaptive_ladder",
    "build_copy_target",
    "build_transcode_target",
    "compute_resume_position_seconds",
    "default_stream_tuning",
    "derive_resume_position_seconds",
    "dimensions_for_height",
    "ffprobe_video_info",
    "find_target_index",
    "format_seek_seconds",
    "get_current_net_rates",
    "load_stream_config",
    "load_stream_runtime_state",
    "load_stream_tuning",
    "next_lower_height",
    "normalize_stream_payload",
    "parse_resolution",
    "preset_rank",
    "read_current_stream_bitrate_kbps",
    "read_recent_ffmpeg_position_seconds",
    "read_recent_ffmpeg_speed",
    "recommend_stream_settings",
    "safe_bool",
    "safe_float",
    "safe_int",
    "sanitize_stream_tuning",
    "save_stream_config",
    "save_stream_runtime_state",
    "save_stream_tuning",
    "select_runtime_target",
    "snap_fps",
    "stream_config_public_view",
    "stream_output_url",
    "stream_payload_signature",
    "stream_relay_status",
    "stream_target_view",
    "target_quality_key",
    "target_total_bitrate_kbps",
    "update_adaptive_state",
]
