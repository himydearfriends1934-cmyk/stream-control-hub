from __future__ import annotations

from statistics import median
from typing import Any


MOTION_LEVELS = ("static", "medium", "dynamic")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def classify_motion_samples(samples: list[dict[str, Any]] | None) -> dict[str, Any]:
    valid = [item for item in samples or [] if isinstance(item, dict)]
    changed = [_float(item.get("changed_ratio")) for item in valid]
    differences = [_float(item.get("mean_diff")) for item in valid]
    cuts = sum(1 for item in valid if item.get("scene_cut"))
    if not changed or not differences:
        return {
            "level": "medium",
            "confidence": 0.0,
            "score": None,
            "sample_count": 0,
            "reason": "motion samples were unavailable; use the guarded medium-dynamic profile",
        }

    changed_median = median(changed)
    diff_median = median(differences)
    changed_high = sum(1 for value in changed if value >= 0.30)
    score = round(min(1.0, changed_median * 0.75 + diff_median * 0.25), 4)
    if changed_median <= 0.08 and diff_median <= 0.025 and changed_high <= max(1, len(changed) // 5):
        level = "static"
    elif changed_median > 0.30 or diff_median > 0.08 or changed_high >= max(2, len(changed) * 0.6):
        level = "dynamic"
    else:
        level = "medium"

    boundary_distance = min(
        abs(changed_median - 0.08) / 0.08 if changed_median else 1.0,
        abs(changed_median - 0.30) / 0.30,
        abs(diff_median - 0.025) / 0.025 if diff_median else 1.0,
        abs(diff_median - 0.08) / 0.08,
    )
    confidence = round(min(1.0, 0.35 + min(0.45, len(valid) / 32) + min(0.20, boundary_distance)), 3)
    return {
        "level": level,
        "confidence": confidence,
        "score": score,
        "sample_count": len(valid),
        "median_changed_ratio": round(changed_median, 4),
        "median_mean_diff": round(diff_median, 4),
        "scene_cut_count": cuts,
        "reason": {
            "static": "most sampled frames changed only in a small local area",
            "medium": "sampled motion was mixed or close to a classification boundary",
            "dynamic": "most sampled frames changed across a large part of the image",
        }[level],
    }


def motion_profile_cache_key(node_id: str, video_path: str, *, size: int = 0, modified: float = 0.0) -> str:
    return f"{node_id}:{video_path}:{int(size or 0)}:{float(modified or 0):.3f}"

