#!/usr/bin/env python3

from __future__ import annotations

import argparse
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from common import (
    apply_limit,
    default_chrome_root,
    emit,
    error_response,
    parse_datetime,
    skipped_response,
    success_response,
    summarize_text,
    within_range,
)


SOURCE_NAME = "chrome-history"
CHROME_ROOT = default_chrome_root() or Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Emit Chrome browsing history as DayTrace events.")
    parser.add_argument("--workspace", default=".", help="Accepted for aggregator compatibility. Ignored.")
    parser.add_argument("--since", help="Start datetime or date (inclusive).")
    parser.add_argument("--until", help="End datetime or date (inclusive).")
    parser.add_argument("--limit", type=int, help="Maximum number of events to return.")
    parser.add_argument("--profile", action="append", help="Specific Chrome profile(s) to inspect.")
    parser.add_argument("--root", default=str(CHROME_ROOT), help="Chrome root directory.")
    return parser


def discover_history_files(root: Path, profiles: list[str] | None) -> list[tuple[str, Path]]:
    if profiles:
        candidates = [(profile, root / profile / "History") for profile in profiles]
    else:
        candidates = []
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child.name == "Default" or child.name.startswith("Profile ")):
                candidates.append((child.name, child / "History"))
    return [(profile, path) for profile, path in candidates if path.exists()]


def chrome_timestamp_to_iso(value: int) -> str:
    current = CHROME_EPOCH + timedelta(microseconds=value)
    return current.astimezone().isoformat()


def normalize_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def collapse_visits(rows: list[tuple[str, str, str, int, int]]) -> list[dict[str, object]]:
    collapsed: dict[tuple[str, str], dict[str, object]] = {}
    for profile, normalized_url, title, last_visit_time, visit_count in rows:
        key = (profile, normalized_url)
        timestamp = chrome_timestamp_to_iso(last_visit_time)
        current = collapsed.get(key)
        if current is None:
            collapsed[key] = {
                "profile": profile,
                "url": normalized_url,
                "title": title or "",
                "timestamp": timestamp,
                "visit_count": visit_count,
            }
            continue

        current["visit_count"] = int(current["visit_count"]) + visit_count
        if timestamp > str(current["timestamp"]):
            current["timestamp"] = timestamp
            current["title"] = title or str(current["title"])
    return list(collapsed.values())


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        root = Path(args.root).expanduser().resolve()
        start = parse_datetime(args.since, bound="start")
        end = parse_datetime(args.until, bound="end")

        if not root.exists():
            emit(skipped_response(SOURCE_NAME, "not_found", root=str(root)))
            return

        history_files = discover_history_files(root, args.profile)
        if not history_files:
            emit(skipped_response(SOURCE_NAME, "not_found", root=str(root)))
            return

        raw_rows: list[tuple[str, str, str, int, int]] = []
        checked_profiles = []
        for profile, history_path in history_files:
            checked_profiles.append(profile)
            with tempfile.NamedTemporaryFile(prefix=f"daytrace-{profile}-", suffix=".sqlite", delete=False) as temp_file:
                temp_path = Path(temp_file.name)
            try:
                shutil.copy2(history_path, temp_path)
                connection = sqlite3.connect(temp_path)
                cursor = connection.cursor()
                cursor.execute(
                    """
                    SELECT urls.url, urls.title, urls.last_visit_time, urls.visit_count
                    FROM urls
                    WHERE urls.last_visit_time > 0
                    ORDER BY urls.last_visit_time DESC
                    """
                )
                for url, title, last_visit_time, visit_count in cursor.fetchall():
                    timestamp = chrome_timestamp_to_iso(last_visit_time)
                    if not within_range(timestamp, start, end):
                        continue

                    normalized_url = normalize_url(url)
                    raw_rows.append((profile, normalized_url, title or "", last_visit_time, visit_count))
                connection.close()
            finally:
                temp_path.unlink(missing_ok=True)

        events = []
        for row in collapse_visits(raw_rows):
            events.append(
                {
                    "source": SOURCE_NAME,
                    "timestamp": str(row["timestamp"]),
                    "type": "browser_visit",
                    "summary": summarize_text(str(row["title"]) or str(row["url"]), 140),
                    "details": {
                        "profile": row["profile"],
                        "url": row["url"],
                        "title": row["title"],
                        "visit_count": row["visit_count"],
                    },
                    "confidence": "low",
                }
            )
        events.sort(key=lambda event: event["timestamp"], reverse=True)
        emit(
            success_response(
                SOURCE_NAME,
                apply_limit(events, args.limit),
                profiles=checked_profiles,
                since=args.since,
                until=args.until,
            )
        )
    except PermissionError as exc:
        emit(skipped_response(SOURCE_NAME, "permission_denied", root=str(root), message=str(exc)))
    except Exception as exc:
        emit(error_response(SOURCE_NAME, str(exc)))


if __name__ == "__main__":
    main()
