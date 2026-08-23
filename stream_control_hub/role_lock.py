from __future__ import annotations

import os
from pathlib import Path


ROLE_NAMES = frozenset({"hub", "agent"})


class RoleConflictError(RuntimeError):
    """Raised when a service is started with a different declared role."""


def role_file_path(root: Path | None = None) -> Path:
    configured = str(os.environ.get("STREAM_NODE_ROLE_FILE") or "").strip()
    if configured:
        return Path(configured)
    system_path = Path("/etc/stream-control/role")
    if system_path.exists() or os.name != "nt":
        return system_path
    return (root or Path.cwd()) / ".node-role"


def _valid_role(value: str) -> str:
    role = str(value or "").strip().lower()
    return role if role in ROLE_NAMES else ""


def declared_role(root: Path | None = None) -> str:
    env_role = _valid_role(os.environ.get("STREAM_NODE_ROLE", ""))
    marker = role_file_path(root)
    try:
        file_role = _valid_role(marker.read_text(encoding="utf-8").strip())
    except OSError:
        file_role = ""
    if env_role and file_role and env_role != file_role:
        raise RoleConflictError(
            f"role marker conflict: STREAM_NODE_ROLE={env_role!r}, file={file_role!r}"
        )
    return file_role or env_role


def assert_role(expected: str, root: Path | None = None) -> str:
    role = _valid_role(expected)
    if not role:
        raise ValueError(f"unsupported role: {expected}")
    configured = declared_role(root)
    if configured and configured != role:
        raise RoleConflictError(
            f"this machine is declared as {configured}; refusing to start {role} service"
        )
    return configured or role


def write_role_marker(role: str, root: Path | None = None) -> Path:
    value = _valid_role(role)
    if not value:
        raise ValueError(f"unsupported role: {role}")
    marker = role_file_path(root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(f".{marker.name}.tmp")
    temporary.write_text(value + "\n", encoding="utf-8")
    temporary.replace(marker)
    try:
        marker.chmod(0o600)
    except OSError:
        pass
    return marker

