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
FLOW_GAP_MINUTES = 2


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


def normalized_host(raw_url: str) -> str:
    host = urlsplit(raw_url).netloc.lower().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def flow_key(raw_url: str, title: str) -> str:
    parsed = urlsplit(raw_url)
    host = normalized_host(raw_url)
    path = parsed.path.lower()
    segments = [segment for segment in path.split("/") if segment]
    lowered_title = title.lower()

    login_tokens = ("/login", "/authorize", "/callback", "/signin", "/logout")
    if host.endswith("auth0.com") or any(token in path for token in login_tokens) or "log in" in lowered_title or "login" in lowered_title:
        return "login"
    if host in {"x.com", "twitter.com"}:
        if "/status/" in path:
            return "status"
        if "/photo/" in path:
            return "media"
        if segments and segments[0] in {"home", "explore", "search"}:
            return segments[0]
        return segments[0] if segments else "root"
    if host in {"google.com", "google.co.jp"} and parsed.path == "/search":
        return "search"
    return segments[0] if segments else "root"


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
                "host": normalized_host(normalized_url),
                "flow_key": flow_key(normalized_url, title or ""),
            }
            continue

        current["visit_count"] = int(current["visit_count"]) + visit_count
        if timestamp > str(current["timestamp"]):
            current["timestamp"] = timestamp
            current["title"] = title or str(current["title"])
            current["flow_key"] = flow_key(normalized_url, title or "")
    return list(collapsed.values())


def compress_visit_flows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    if not rows:
        return []

    ordered = sorted(
        rows,
        key=lambda item: (str(item.get("profile") or ""), str(item.get("timestamp") or "")),
    )
    max_gap = timedelta(minutes=FLOW_GAP_MINUTES)
    compressed: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    current_end: datetime | None = None

    def start_group(row: dict[str, object]) -> tuple[dict[str, object], datetime]:
        timestamp = datetime.fromisoformat(str(row["timestamp"]))
        return (
            {
                **row,
                "page_count": 1,
                "merged_urls": [str(row["url"])],
                "first_timestamp": str(row["timestamp"]),
                "last_timestamp": str(row["timestamp"]),
                "compressed": False,
            },
            timestamp,
        )

    def flush_group(group: dict[str, object] | None) -> None:
        if group is None:
            return
        merged_urls = [str(item) for item in group.get("merged_urls", []) if str(item).strip()]
        unique_urls = list(dict.fromkeys(merged_urls))
        group["merged_urls"] = unique_urls[:6]
        group["compressed"] = int(group.get("page_count", 1) or 1) > 1
        compressed.append(group)

    for row in ordered:
        row_time = datetime.fromisoformat(str(row["timestamp"]))
        if current is None or current_end is None:
            current, current_end = start_group(row)
            continue

        same_profile = str(current.get("profile") or "") == str(row.get("profile") or "")
        same_host = str(current.get("host") or "") == str(row.get("host") or "")
        same_flow = str(current.get("flow_key") or "") == str(row.get("flow_key") or "")
        close_in_time = row_time - current_end <= max_gap
        if same_profile and same_host and same_flow and close_in_time:
            current["visit_count"] = int(current.get("visit_count", 0) or 0) + int(row.get("visit_count", 0) or 0)
            current["page_count"] = int(current.get("page_count", 1) or 1) + 1
            current["last_timestamp"] = str(row["timestamp"])
            merged_urls = current.setdefault("merged_urls", [])
            if isinstance(merged_urls, list):
                merged_urls.append(str(row["url"]))
            if str(row.get("timestamp") or "") >= str(current.get("timestamp") or ""):
                current["timestamp"] = str(row["timestamp"])
                current["title"] = str(row.get("title") or current.get("title") or "")
                current["url"] = str(row.get("url") or current.get("url") or "")
            current_end = row_time
            continue

        flush_group(current)
        current, current_end = start_group(row)

    flush_group(current)
    return compressed


def flow_summary(row: dict[str, object]) -> str:
    title = str(row.get("title") or "").strip()
    url = str(row.get("url") or "").strip()
    host = str(row.get("host") or "").strip()
    flow = str(row.get("flow_key") or "").strip()
    page_count = int(row.get("page_count", 1) or 1)
    if page_count <= 1:
        return summarize_text(title or url, 140)

    if flow == "login":
        return summarize_text(f"{title or host} ログインフロー", 140)
    if host in {"x.com", "twitter.com"}:
        return summarize_text(title or "X 閲覧フロー", 140)
    if host in {"google.com", "google.co.jp"} and flow == "search":
        return summarize_text(title or "Google 検索フロー", 140)
    return summarize_text(f"{title or host}（{page_count}ページ）", 140)


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
        for row in compress_visit_flows(collapse_visits(raw_rows)):
            events.append(
                {
                    "source": SOURCE_NAME,
                    "timestamp": str(row["timestamp"]),
                    "type": "browser_visit",
                    "summary": flow_summary(row),
                    "details": {
                        "profile": row["profile"],
                        "url": row["url"],
                        "title": row["title"],
                        "visit_count": row["visit_count"],
                        "host": row.get("host"),
                        "flow_key": row.get("flow_key"),
                        "page_count": row.get("page_count", 1),
                        "compressed": bool(row.get("compressed", False)),
                        "merged_urls": row.get("merged_urls", []),
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
