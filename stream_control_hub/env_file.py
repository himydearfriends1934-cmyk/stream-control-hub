from __future__ import annotations

import os
import re
import uuid
from contextlib import suppress
from pathlib import Path


ENV_KEY_PATTERN = re.compile(r"(?:STREAM_[A-Z0-9_]+|YOUTUBE_[A-Z0-9_]+)=")


def _normalize_env_text(text: str) -> str:
    normalized = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    return normalized.replace("\r\n", "\n").replace("\r", "\n")


def _split_possible_collapsed_line(line: str) -> list[str]:
    matches = list(ENV_KEY_PATTERN.finditer(line))
    if len(matches) <= 1:
        return [line]
    parts: list[str] = []
    if matches[0].start() > 0:
        prefix = line[: matches[0].start()]
        if prefix.strip():
            parts.append(prefix)
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        parts.append(line[start:end])
    return parts


def iter_env_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in _normalize_env_text(text).split("\n"):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            lines.append(raw_line)
            continue
        lines.extend(_split_possible_collapsed_line(raw_line))
    return lines


def parse_env_assignments(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in iter_env_lines(text):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in values:
            values[key] = value.strip().strip('"').strip("'")
    return values


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for key, value in parse_env_assignments(path.read_text(encoding="utf-8")).items():
        if key not in os.environ:
            os.environ[key] = value


def update_env_file_values(path: Path, updates: dict[str, str]) -> None:
    values = {str(key): str(value).replace("\r", "").replace("\n", "") for key, value in updates.items() if value is not None}
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    if path.exists():
        for raw_line in iter_env_lines(path.read_text(encoding="utf-8")):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or "=" not in raw_line:
                existing.append((raw_line, None))
                continue
            key, _ = raw_line.split("=", 1)
            key = key.strip()
            if key in values:
                existing.append((f"{key}={values[key]}", key))
                seen.add(key)
            else:
                existing.append((raw_line, key))
    for key, value in values.items():
        if key not in seen:
            existing.append((f"{key}={value}", key))
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text("\n".join(line for line, _ in existing) + "\n", encoding="utf-8")
        with suppress(OSError):
            temporary.chmod(0o600)
        temporary.replace(path)
        with suppress(OSError):
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
