#!/usr/bin/env python3

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from common import (
    apply_limit,
    emit,
    error_response,
    extract_text,
    is_within_path,
    parse_datetime,
    resolve_workspace,
    skipped_response,
    success_response,
    summarize_text,
    within_range,
)
from session_packets import (
    ASSISTANT_HIGHLIGHT_LIMIT,
    DEFAULT_GAP_HOURS,
    MAX_ASSISTANT_HIGHLIGHTS,
    MAX_USER_HIGHLIGHTS,
    USER_HIGHLIGHT_LIMIT,
    build_claude_logical_packets,
    claude_message_is_tool_result_only,
    head_tail_excerpts,
)
from request_text import clean_request_text


SOURCE_NAME = "claude-history"
DEFAULT_ROOT = Path.home() / ".claude" / "projects"


def collect_user_requests(records: list[dict[str, object]], workspace: Path | None) -> list[dict[str, object]]:
    """Each turn the user typed, in order. Subagent (sidechain) prompts are written by Claude, so they are left out."""
    requests: list[dict[str, object]] = []
    for record in records:
        if record.get("type") != "user" or record.get("isSidechain"):
            continue
        if workspace and not is_within_path(record.get("cwd"), workspace):
            continue
        message = record.get("message")
        if claude_message_is_tool_result_only(message):
            continue
        text = clean_request_text(user_turn_text(message))
        if text:
            requests.append({"timestamp": record.get("timestamp"), "text": text})
    return requests


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Emit Claude session summaries as DayTrace events.")
    parser.add_argument("--workspace", default=".", help="Workspace path to filter by. Ignored with --all-sessions.")
    parser.add_argument("--since", help="Start datetime or date (inclusive).")
    parser.add_argument("--until", help="End datetime or date (inclusive).")
    parser.add_argument("--all-sessions", action="store_true", help="Ignore workspace filtering and scan all sessions.")
    parser.add_argument("--limit", type=int, help="Maximum number of events to return.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="Claude history root. Defaults to ~/.claude/projects.")
    return parser


def empty_group(path: Path) -> dict[str, object]:
    return {
        "file_path": str(path),
        "session_id": None,
        "cwd": None,
        "is_sidechain": False,
        "timestamps": [],
        "user_excerpts": [],
        "assistant_excerpts": [],
        "message_count": 0,
        "user_count": 0,
        "assistant_count": 0,
    }


def append_excerpt(bucket: list[str], value: str, *, limit: int, max_items: int) -> None:
    excerpt = summarize_text(value, limit)
    if excerpt and excerpt not in bucket and len(bucket) < max_items:
        bucket.append(excerpt)


def user_turn_text(message: object) -> str:
    """Text of a user turn: its text blocks, or tool-call names when it has no text."""
    if not isinstance(message, dict):
        return extract_text(message)

    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        tool_parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
            elif item.get("type") == "tool_use":
                tool_parts.append(f"{item.get('name', 'tool')} tool call")
        if text_parts:
            return " ".join(text_parts)
        if tool_parts:
            return " ".join(tool_parts)
    return extract_text(message)


def read_session_records(path: Path, start, end) -> list[dict[str, object]]:
    """User and assistant turns of one session file inside the time range."""
    records: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") not in {"user", "assistant"}:
                continue
            if record.get("isMeta") or record.get("isCompactSummary"):
                continue
            if not within_range(record.get("timestamp"), start, end):
                continue
            records.append(record)
    return records


def serialize_packet(packet_index: int, logical_packet: dict[str, object]) -> dict[str, object]:
    user_excerpts = head_tail_excerpts(
        [str(m) for m in logical_packet.get("user_messages", [])],
        limit=USER_HIGHLIGHT_LIMIT,
        max_items=MAX_USER_HIGHLIGHTS,
    )
    assistant_excerpts = head_tail_excerpts(
        [str(m) for m in logical_packet.get("assistant_messages", [])],
        limit=ASSISTANT_HIGHLIGHT_LIMIT,
        max_items=MAX_ASSISTANT_HIGHLIGHTS,
    )
    return {
        "packet_index": packet_index,
        "started_at": logical_packet.get("started_at"),
        "ended_at": logical_packet.get("ended_at"),
        "session_id": logical_packet.get("session_id"),
        "cwd": logical_packet.get("cwd"),
        "is_sidechain": logical_packet.get("is_sidechain"),
        "message_count": logical_packet.get("message_count"),
        "user_message_count": logical_packet.get("user_message_count"),
        "assistant_message_count": logical_packet.get("assistant_message_count"),
        "user_highlights": user_excerpts,
        "assistant_highlights": assistant_excerpts,
        "assistant_summary": assistant_excerpts[-1] if assistant_excerpts else None,
        "tool_signals": list(logical_packet.get("tools", [])),
        "tool_call_details": [detail for detail in logical_packet.get("tool_calls", []) if isinstance(detail, dict)],
        "referenced_files": list(logical_packet.get("referenced_files", [])),
    }


def summarize_session(path: Path, logical_packets: list[dict[str, object]]) -> dict[str, object]:
    """Roll the logical packets of one session file up into one session summary."""
    group = empty_group(path)
    group["packets"] = []
    group["tool_calls"] = []
    referenced_files: list[str] = []
    for packet_index, logical_packet in enumerate(logical_packets):
        group["session_id"] = logical_packet.get("session_id") or group["session_id"]
        group["cwd"] = logical_packet.get("cwd") or group["cwd"]
        group["is_sidechain"] = bool(logical_packet.get("is_sidechain")) or bool(group["is_sidechain"])
        group["timestamps"].extend(logical_packet.get("timestamps", []))
        group["message_count"] = int(group["message_count"]) + int(logical_packet.get("message_count") or 0)
        group["user_count"] = int(group["user_count"]) + int(logical_packet.get("user_message_count") or 0)
        group["assistant_count"] = int(group["assistant_count"]) + int(logical_packet.get("assistant_message_count") or 0)
        group["tool_calls"].extend([detail for detail in logical_packet.get("tool_calls", []) if isinstance(detail, dict)])
        referenced_files.extend(
            [str(value) for value in logical_packet.get("referenced_files", []) if str(value or "").strip()]
        )
        packet = serialize_packet(packet_index, logical_packet)
        for excerpt in packet["user_highlights"]:
            append_excerpt(group["user_excerpts"], excerpt, limit=USER_HIGHLIGHT_LIMIT, max_items=MAX_USER_HIGHLIGHTS)
        for excerpt in packet["assistant_highlights"]:
            append_excerpt(group["assistant_excerpts"], excerpt, limit=ASSISTANT_HIGHLIGHT_LIMIT, max_items=MAX_ASSISTANT_HIGHLIGHTS)
        group["packets"].append(packet)
    group["referenced_files"] = list(dict.fromkeys(referenced_files))[:20]
    return group


def build_session_events(group: dict[str, object], user_requests: list[dict[str, object]]) -> list[dict[str, object]]:
    """session_summary, commentary (when there are excerpts), and tool_call (when tools ran)."""
    timestamps = sorted(group["timestamps"])
    if not timestamps:
        return []
    first_user = group["user_excerpts"][0] if group["user_excerpts"] else "No user prompt captured"
    details = {
        "cwd": group["cwd"],
        "session_id": group["session_id"],
        "file_path": group["file_path"],
        "is_sidechain": group["is_sidechain"],
        "message_count": group["message_count"],
        "user_message_count": group["user_count"],
        "assistant_message_count": group["assistant_count"],
        "user_highlights": group["user_excerpts"],
        "assistant_highlights": group["assistant_excerpts"],
        "highlights": group["user_excerpts"] + group["assistant_excerpts"],
        "user_requests": user_requests,
        "logical_packets": group["packets"],
        "logical_packet_count": len(group["packets"]),
        "referenced_files": group["referenced_files"],
    }
    if group["assistant_excerpts"]:
        details["assistant_summary"] = group["assistant_excerpts"][-1]

    events: list[dict[str, object]] = [
        {
            "source": SOURCE_NAME,
            "timestamp": timestamps[-1],
            "type": "session_summary",
            "summary": f"Claude session: {summarize_text(first_user, 96)}",
            "details": details,
            "confidence": "medium",
        }
    ]
    if group["user_excerpts"] or group["assistant_excerpts"]:
        events.append(
            {
                "source": SOURCE_NAME,
                "timestamp": timestamps[-1],
                "type": "commentary",
                "summary": f"Claude commentary: {summarize_text(first_user, 96)}",
                "details": {
                    "cwd": group["cwd"],
                    "session_id": group["session_id"],
                    "file_path": group["file_path"],
                    "user_highlights": group["user_excerpts"],
                    "assistant_highlights": group["assistant_excerpts"],
                    "assistant_summary": group["assistant_excerpts"][-1] if group["assistant_excerpts"] else None,
                },
                "confidence": "medium",
            }
        )
    tool_event = build_tool_event(group, fallback_timestamp=timestamps[-1])
    if tool_event:
        events.append(tool_event)
    return events


def build_tool_event(group: dict[str, object], *, fallback_timestamp: str) -> dict[str, object] | None:
    tool_calls = group["tool_calls"]
    tool_counter = Counter(
        str(detail.get("name") or "").strip().lower()
        for detail in tool_calls
        if str(detail.get("name") or "").strip()
    )
    if not tool_counter:
        return None
    latest_tool_timestamp = next(
        (
            str(detail.get("timestamp"))
            for detail in reversed(tool_calls)
            if str(detail.get("timestamp") or "").strip()
        ),
        fallback_timestamp,
    )
    tool_summary = ", ".join(f"{name} x{count}" for name, count in tool_counter.most_common(5))
    return {
        "source": SOURCE_NAME,
        "timestamp": latest_tool_timestamp,
        "type": "tool_call",
        "summary": f"Claude tool usage: {tool_summary}",
        "details": {
            "cwd": group["cwd"],
            "session_id": group["session_id"],
            "file_path": group["file_path"],
            "tools": [{"name": name, "count": count} for name, count in tool_counter.most_common()],
            "total_calls": sum(tool_counter.values()),
            "tool_call_details": tool_calls[:8],
        },
        "confidence": "high",
    }


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        root = Path(args.root).expanduser().resolve()
        workspace = None if args.all_sessions else resolve_workspace(args.workspace)
        start = parse_datetime(args.since, bound="start")
        end = parse_datetime(args.until, bound="end")

        if not root.exists():
            emit(skipped_response(SOURCE_NAME, "not_found", root=str(root)))
            return

        jsonl_files = sorted(root.glob("**/*.jsonl"))
        if not jsonl_files:
            emit(skipped_response(SOURCE_NAME, "not_found", root=str(root)))
            return

        events = []
        for path in jsonl_files:
            records = read_session_records(path, start, end)
            logical_packets = [
                packet
                for packet in build_claude_logical_packets(records, DEFAULT_GAP_HOURS)
                if not workspace or is_within_path(packet.get("cwd"), workspace)
            ]
            if not logical_packets:
                continue
            group = summarize_session(path, logical_packets)
            events.extend(build_session_events(group, collect_user_requests(records, workspace)))

        events.sort(key=lambda event: event["timestamp"], reverse=True)
        emit(
            success_response(
                SOURCE_NAME,
                apply_limit(events, args.limit),
                workspace=str(workspace) if workspace else None,
                since=args.since,
                until=args.until,
                all_sessions=args.all_sessions,
                scanned_files=len(jsonl_files),
            )
        )
    except PermissionError as exc:
        emit(skipped_response(SOURCE_NAME, "permission_denied", root=str(args.root), message=str(exc)))
    except Exception as exc:
        emit(error_response(SOURCE_NAME, str(exc)))


if __name__ == "__main__":
    main()
