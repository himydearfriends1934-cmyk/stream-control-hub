from __future__ import annotations

import time
import uuid
from typing import Any


POLICY_VERSION = 2
POLICY_AUTHORITY = "hub"
DEFAULT_DECISION_TTL_SECONDS = 300


def issue_policy(payload: dict[str, Any], *, reason: str, ttl_seconds: int = DEFAULT_DECISION_TTL_SECONDS) -> dict[str, Any]:
    """Attach an explicit, short-lived Hub decision to an Agent command."""
    result = dict(payload)
    now = time.time()
    result.update({
        "policy_version": POLICY_VERSION,
        "policy_authority": POLICY_AUTHORITY,
        "policy_decision_id": f"hub-{uuid.uuid4().hex}",
        "policy_reason": str(reason or "central-policy")[:120],
        "policy_issued_at": now,
        "policy_expires_at": now + max(30, min(900, int(ttl_seconds or DEFAULT_DECISION_TTL_SECONDS))),
    })
    return result


def validate_policy(payload: dict[str, Any], *, now: float | None = None) -> str:
    """Return a safe error string for stale/foreign policy metadata."""
    fields = ("policy_version", "policy_authority", "policy_decision_id", "policy_expires_at")
    if not any(field in payload for field in fields):
        return ""
    if int(payload.get("policy_version") or 0) != POLICY_VERSION:
        return f"unsupported policy version: {payload.get('policy_version')!r}"
    if str(payload.get("policy_authority") or "").strip().lower() != POLICY_AUTHORITY:
        return "stream policy must be issued by the HUB"
    if not str(payload.get("policy_decision_id") or "").strip():
        return "policy decision id is required"
    try:
        issued_at = float(payload.get("policy_issued_at") or 0)
        expires_at = float(payload.get("policy_expires_at") or 0)
    except (TypeError, ValueError):
        return "policy timestamps are invalid"
    current = time.time() if now is None else float(now)
    if issued_at and issued_at > current + 30:
        return "policy issued time is in the future"
    if expires_at <= current:
        return "policy decision has expired"
    if expires_at - current > 901:
        return "policy decision lifetime is too long"
    return ""


def strip_policy(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    for key in (
        "policy_version",
        "policy_authority",
        "policy_decision_id",
        "policy_reason",
        "policy_issued_at",
        "policy_expires_at",
    ):
        result.pop(key, None)
    return result
