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
from skill_miner_common import (
    ASSISTANT_HIGHLIGHT_LIMIT,
    DEFAULT_GAP_HOURS,
    MAX_ASSISTANT_HIGHLIGHTS,
    MAX_USER_HIGHLIGHTS,
    USER_HIGHLIGHT_LIMIT,
    build_claude_logical_packets,
    build_claude_session_ref,
    build_packet,
    head_tail_excerpts,
)


SOURCE_NAME = "claude-history"
DEFAULT_ROOT = Path.home() / ".claude" / "projects"


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


def claude_message_text(message: object) -> str:
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
            filtered_records: list[dict[str, object]] = []
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
                    record_type = record.get("type")
                    if record_type not in {"user", "assistant"}:
                        continue
                    if record.get("isMeta"):
                        continue
                    timestamp = record.get("timestamp")
                    if not within_range(timestamp, start, end):
                        continue
                    filtered_records.append(record)

            all_logical_packets = build_claude_logical_packets(filtered_records, DEFAULT_GAP_HOURS)
            matched_packets = [
                lp for lp in all_logical_packets
                if not workspace or is_within_path(lp.get("cwd"), workspace)
            ]
            if not matched_packets:
                continue

            group = empty_group(path)
            serialized_packets: list[dict[str, object]] = []
            aggregated_user_messages: list[str] = []
            aggregated_assistant_messages: list[str] = []
            aggregated_tools: list[str] = []
            aggregated_tool_calls: list[dict[str, object]] = []
            aggregated_referenced_files: list[str] = []
            for packet_index, logical_packet in enumerate(matched_packets):
                group["session_id"] = logical_packet.get("session_id") or group["session_id"]
                group["cwd"] = logical_packet.get("cwd") or group["cwd"]
                group["is_sidechain"] = bool(logical_packet.get("is_sidechain")) or bool(group["is_sidechain"])
                group["timestamps"].extend(logical_packet.get("timestamps", []))
                group["message_count"] = int(group["message_count"]) + int(logical_packet.get("message_count") or 0)
                group["user_count"] = int(group["user_count"]) + int(logical_packet.get("user_message_count") or 0)
                group["assistant_count"] = int(group["assistant_count"]) + int(logical_packet.get("assistant_message_count") or 0)
                aggregated_user_messages.extend([str(message) for message in logical_packet.get("user_messages", [])])
                aggregated_assistant_messages.extend([str(message) for message in logical_packet.get("assistant_messages", [])])
                aggregated_tools.extend([str(tool) for tool in logical_packet.get("tools", [])])
                aggregated_tool_calls.extend([detail for detail in logical_packet.get("tool_calls", []) if isinstance(detail, dict)])
                aggregated_referenced_files.extend(
                    [str(value) for value in logical_packet.get("referenced_files", []) if str(value or "").strip()]
                )

                user_excerpts = head_tail_excerpts(
                    [str(m) for m in logical_packet.get("user_messages", [])],
                    limit=USER_HIGHLIGHT_LIMIT,
                    max_items=MAX_USER_HIGHLIGHTS,
                )
                for excerpt in user_excerpts:
                    append_excerpt(group["user_excerpts"], excerpt, limit=USER_HIGHLIGHT_LIMIT, max_items=MAX_USER_HIGHLIGHTS)
                assistant_excerpts = head_tail_excerpts(
                    [str(m) for m in logical_packet.get("assistant_messages", [])],
                    limit=ASSISTANT_HIGHLIGHT_LIMIT,
                    max_items=MAX_ASSISTANT_HIGHLIGHTS,
                )
                for excerpt in assistant_excerpts:
                    append_excerpt(group["assistant_excerpts"], excerpt, limit=ASSISTANT_HIGHLIGHT_LIMIT, max_items=MAX_ASSISTANT_HIGHLIGHTS)

                assistant_summary = assistant_excerpts[-1] if assistant_excerpts else None
                packet_start = logical_packet.get("started_at")
                skill_miner_packet = build_packet(
                    packet_id=f"claude:{path.parent.name}:{path.stem}:{packet_index:03d}",
                    source=SOURCE_NAME,
                    session_ref=build_claude_session_ref(str(path), packet_start),
                    session_id=logical_packet.get("session_id"),
                    workspace=logical_packet.get("cwd"),
                    timestamp=packet_start,
                    user_messages=[str(message) for message in logical_packet.get("user_messages", [])],
                    assistant_messages=[str(message) for message in logical_packet.get("assistant_messages", [])],
                    tools=[str(tool) for tool in logical_packet.get("tools", [])],
                    tool_call_details=[detail for detail in logical_packet.get("tool_calls", []) if isinstance(detail, dict)],
                    referenced_files=logical_packet.get("referenced_files", []),
                    is_sidechain=bool(logical_packet.get("is_sidechain")),
                )
                serialized_packets.append(
                    {
                        "packet_index": packet_index,
                        "started_at": packet_start,
                        "ended_at": logical_packet.get("ended_at"),
                        "session_id": logical_packet.get("session_id"),
                        "cwd": logical_packet.get("cwd"),
                        "is_sidechain": logical_packet.get("is_sidechain"),
                        "message_count": logical_packet.get("message_count"),
                        "user_message_count": logical_packet.get("user_message_count"),
                        "assistant_message_count": logical_packet.get("assistant_message_count"),
                        "user_highlights": user_excerpts,
                        "assistant_highlights": assistant_excerpts,
                        "assistant_summary": assistant_summary,
                        "tool_signals": list(logical_packet.get("tools", [])),
                        "tool_call_details": [detail for detail in logical_packet.get("tool_calls", []) if isinstance(detail, dict)],
                        "ai_observation": skill_miner_packet,
                        "skill_miner_packet": skill_miner_packet,
                    }
                )

            timestamps = sorted(group["timestamps"])
            if not timestamps:
                continue

            ai_observation_packets = [
                packet["skill_miner_packet"]
                for packet in serialized_packets
                if isinstance(packet.get("skill_miner_packet"), dict)
            ]
            merged_ai_observation = build_packet(
                packet_id=f"claude:{path.parent.name}:{path.stem}:summary",
                source=SOURCE_NAME,
                session_ref=build_claude_session_ref(str(path), timestamps[0]),
                session_id=str(group["session_id"] or "") or None,
                workspace=str(group["cwd"] or "") or None,
                timestamp=timestamps[0],
                user_messages=aggregated_user_messages or [str(excerpt) for excerpt in group["user_excerpts"]],
                assistant_messages=aggregated_assistant_messages or [str(excerpt) for excerpt in group["assistant_excerpts"]],
                tools=aggregated_tools,
                tool_call_details=[dict(detail) for detail in aggregated_tool_calls],
                referenced_files=list(dict.fromkeys(aggregated_referenced_files))[:20],
                is_sidechain=bool(group["is_sidechain"]),
            )
            first_user = group["user_excerpts"][0] if group["user_excerpts"] else "No user prompt captured"
            summary = f"Claude session: {summarize_text(first_user, 96)}"
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
                "logical_packets": serialized_packets,
                "logical_packet_count": len(serialized_packets),
                "ai_observation": merged_ai_observation,
                "ai_observation_packets": ai_observation_packets,
            }
            if group["assistant_excerpts"]:
                details["assistant_summary"] = group["assistant_excerpts"][-1]

            events.append(
                {
                    "source": SOURCE_NAME,
                    "timestamp": timestamps[-1],
                    "type": "session_summary",
                    "summary": summary,
                    "details": details,
                    "confidence": "medium",
                }
            )
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
                            "ai_observation": merged_ai_observation,
                            "ai_observation_packets": ai_observation_packets,
                        },
                        "confidence": "medium",
                    }
                )
            tool_counter = Counter(
                str(detail.get("name") or "").strip().lower()
                for detail in aggregated_tool_calls
                if str(detail.get("name") or "").strip()
            )
            if tool_counter:
                latest_tool_timestamp = next(
                    (
                        str(detail.get("timestamp"))
                        for detail in reversed(aggregated_tool_calls)
                        if str(detail.get("timestamp") or "").strip()
                    ),
                    timestamps[-1],
                )
                tool_summary = ", ".join(f"{name} x{count}" for name, count in tool_counter.most_common(5))
                events.append(
                    {
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
                            "tool_call_details": aggregated_tool_calls[:8],
                            "ai_observation": merged_ai_observation,
                            "ai_observation_packets": ai_observation_packets,
                        },
                        "confidence": "high",
                    }
                )

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
