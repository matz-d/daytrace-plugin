#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc
URL_PATTERN = re.compile(r"https?://[^\s<>\"]+")


def parse_datetime(value: str | None, *, bound: str = "start") -> datetime | None:
    if not value:
        return None

    raw = value.strip()
    if not raw:
        return None

    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"

    date_only = len(raw) == 10 and raw.count("-") == 2
    parsed: datetime | None = None

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = None

    if parsed is None:
        formats = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")
        for fmt in formats:
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        raise ValueError(f"Unsupported datetime format: {value}")

    if date_only:
        parsed = datetime.combine(
            parsed.date(),
            time.max if bound == "end" else time.min,
            tzinfo=LOCAL_TZ,
        )
    elif parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)

    return parsed


def ensure_datetime(value: datetime | str | int | float | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=LOCAL_TZ)
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        return parse_datetime(value)
    raise TypeError(f"Unsupported datetime value: {value!r}")


def isoformat(value: datetime | str | int | float | None) -> str | None:
    current = ensure_datetime(value)
    return current.isoformat() if current else None


def isoformat_or_now(value: datetime | str | int | float | None) -> str:
    normalized = ensure_datetime(value) if value is not None else datetime.now().astimezone()
    return normalized.isoformat()


def within_range(value: datetime | str | int | float | None, start: datetime | None, end: datetime | None) -> bool:
    current = ensure_datetime(value)
    normalized_start = ensure_datetime(start)
    normalized_end = ensure_datetime(end)
    if current is None:
        return False
    if normalized_start and current < normalized_start:
        return False
    if normalized_end and current > normalized_end:
        return False
    return True


def sanitize_url(raw_url: str) -> str:
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return raw_url
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def sanitize_text(value: str | None) -> str:
    text = value or ""
    return URL_PATTERN.sub(lambda match: sanitize_url(match.group(0)), text)


def summarize_text(value: str | None, limit: int = 160) -> str:
    text = " ".join(sanitize_text(value).split())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return " ".join(part for part in (extract_text(item) for item in value) if part)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("thinking"), str):
            return value["thinking"]
        if value.get("type") == "tool_use":
            name = value.get("name", "tool")
            return f"{name} tool call"
        pieces = []
        for key in ("content", "message", "summary", "arguments", "input"):
            if key in value:
                piece = extract_text(value.get(key))
                if piece:
                    pieces.append(piece)
        return " ".join(pieces)
    return str(value)


def success_response(source: str, events: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "success", "source": source, "events": events}
    payload.update(extra)
    return payload


def skipped_response(source: str, reason: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "skipped", "source": source, "reason": reason, "events": []}
    payload.update(extra)
    return payload


def error_response(source: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "source": source, "message": message, "events": []}
    payload.update(extra)
    return payload


def emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def run_command(args: list[str], *, cwd: str | Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def default_chrome_root() -> Path | None:
    """Return the default Chrome user data directory for the current platform.
    Returns None for unsupported platforms (e.g. Windows).
    """
    if sys.platform.startswith("darwin"):
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    if sys.platform.startswith("linux"):
        return Path.home() / ".config" / "google-chrome"
    return None


def resolve_workspace(workspace: str | None) -> Path:
    target = workspace or os.getcwd()
    return Path(target).expanduser().resolve()


def is_within_path(candidate: str | Path | None, root: str | Path | None) -> bool:
    if root is None:
        return True
    if candidate is None:
        return False
    try:
        Path(candidate).expanduser().resolve().relative_to(Path(root).expanduser().resolve())
        return True
    except ValueError:
        return False


def current_platform() -> str:
    if sys.platform.startswith("darwin"):
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def apply_limit(events: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    if limit is None or limit < 0:
        return events
    return events[:limit]
