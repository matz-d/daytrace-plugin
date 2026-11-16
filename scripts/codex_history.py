#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from common import (
    apply_limit,
    emit,
    error_response,
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
    build_codex_logical_packets,
    codex_user_message_text,
    earliest_iso_timestamp,
    head_tail_excerpts,
)


SOURCE_NAME = "codex-history"
DEFAULT_HISTORY = Path.home() / ".codex" / "history.jsonl"
DEFAULT_SESSIONS = Path.home() / ".codex" / "sessions"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Emit Codex session summaries as DayTrace events.")
    parser.add_argument("--workspace", default=".", help="Workspace path to filter by. Ignored with --all-sessions.")
    parser.add_argument("--since", help="Start datetime or date (inclusive).")
    parser.add_argument("--until", help="End datetime or date (inclusive).")
    parser.add_argument("--all-sessions", action="store_true", help="Ignore workspace filtering and scan all sessions.")
    parser.add_argument("--limit", type=int, help="Maximum number of events to return.")
    parser.add_argument("--history-file", default=str(DEFAULT_HISTORY), help="Codex history.jsonl path.")
    parser.add_argument("--sessions-root", default=str(DEFAULT_SESSIONS), help="Codex sessions root.")
    parser.add_argument("--gap-hours", type=int, default=DEFAULT_GAP_HOURS, help="Hours of inactivity that split Codex logical packets.")
    return parser


def append_history_record(index: dict[str, dict[str, object]], session_id: str, timestamp, text) -> None:
    session = index.setdefault(session_id, {"timestamps": [], "user_excerpts": [], "user_messages": []})
    session["timestamps"].append(timestamp)
    text_value = str(text or "")
    if text_value:
        session["user_messages"].append(text_value)


def finalize_history_excerpts(index: dict[str, dict[str, object]]) -> None:
    for session in index.values():
        user_messages = [str(message) for message in session.get("user_messages", []) if str(message or "").strip()]
        session["user_excerpts"] = head_tail_excerpts(
            user_messages,
            limit=USER_HIGHLIGHT_LIMIT,
            max_items=MAX_USER_HIGHLIGHTS,
        )


def load_history_indexes(path: Path, start, end) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    full_index: dict[str, dict[str, object]] = {}
    filtered_index: dict[str, dict[str, object]] = {}
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            timestamp = record.get("ts")
            session_id = record.get("session_id")
            if not session_id:
                continue

            append_history_record(full_index, session_id, timestamp, record.get("text"))
            if within_range(timestamp, start, end):
                append_history_record(filtered_index, session_id, timestamp, record.get("text"))
    finalize_history_excerpts(full_index)
    finalize_history_excerpts(filtered_index)
    return full_index, filtered_index


def session_meta_from_rollout(path: Path) -> dict[str, object] | None:
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "session_meta":
                continue
            payload = record.get("payload", {})
            if payload.get("id"):
                return payload
    return None


def logical_packet_within_range(packet: dict[str, object], start, end) -> bool:
    if start is None and end is None:
        return True
    timestamps = packet.get("timestamps")
    if isinstance(timestamps, list):
        for timestamp in timestamps:
            if within_range(timestamp, start, end):
                return True
    return within_range(packet.get("started_at"), start, end) or within_range(packet.get("ended_at"), start, end)


def extract_codex_turns(records: list[dict[str, object]], start, end) -> list[dict[str, object]]:
    """One entry per Codex turn: what was asked, what started it, and when it started and ended.

    `trigger` is `composer` for a turn the user typed and e.g. `automation_cron_scheduled`
    for a scheduled run. Older rollouts have no turn ids; their requests become
    entries without trigger or end time.
    """
    turns: dict[str, dict[str, object]] = {}
    order: list[str] = []

    def turn_for(turn_id: str) -> dict[str, object]:
        if turn_id not in turns:
            turns[turn_id] = {"turn_id": turn_id, "trigger": None, "started_at": None, "completed_at": None, "requests": []}
            order.append(turn_id)
        return turns[turn_id]

    anonymous = 0
    for record in records:
        payload = record.get("payload")
        if record.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        timestamp = record.get("timestamp")
        payload_type = payload.get("type")
        if payload_type == "task_started":
            attribution = payload.get("turn_attribution") if isinstance(payload.get("turn_attribution"), dict) else {}
            turn_id = str(attribution.get("turn_id") or payload.get("turn_id") or "")
            if turn_id:
                turn = turn_for(turn_id)
                turn["trigger"] = attribution.get("turn_trigger")
                turn["started_at"] = turn["started_at"] or timestamp
            continue
        if payload_type == "task_complete":
            turn_id = str(payload.get("turn_id") or "")
            if turn_id:
                turn_for(turn_id)["completed_at"] = timestamp
            continue
        text = codex_user_message_text(record)
        if not text:
            continue
        turn_id = str(payload.get("turn_id") or "")
        if not turn_id:
            anonymous += 1
            turn_id = f"untracked-{anonymous:03d}"
        turn = turn_for(turn_id)
        turn["started_at"] = turn["started_at"] or timestamp
        turn["requests"].append(text)

    result: list[dict[str, object]] = []
    for turn_id in order:
        turn = turns[turn_id]
        if not turn["requests"] and turn["trigger"] is None:
            continue
        if not within_range(turn["started_at"], start, end):
            continue
        result.append(
            {
                "turn_id": turn_id,
                "trigger": turn["trigger"],
                "started_at": turn["started_at"],
                "completed_at": turn["completed_at"],
                "request": "\n\n".join(str(text) for text in turn["requests"]),
            }
        )
    return result


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        history_file = Path(args.history_file).expanduser().resolve()
        sessions_root = Path(args.sessions_root).expanduser().resolve()
        workspace = None if args.all_sessions else resolve_workspace(args.workspace)
        start = parse_datetime(args.since, bound="start")
        end = parse_datetime(args.until, bound="end")

        if not sessions_root.exists():
            emit(skipped_response(SOURCE_NAME, "not_found", history_file=str(history_file), sessions_root=str(sessions_root)))
            return

        # history.jsonl is optional: Codex stopped updating it in 2026-11, and the
        # rollout files under sessions/ hold every request.
        if history_file.exists():
            full_history_index, filtered_history_index = load_history_indexes(history_file, start, end)
        else:
            full_history_index, filtered_history_index = {}, {}
        rollout_files = sorted(sessions_root.glob("**/rollout-*.jsonl"))
        if not rollout_files:
            emit(skipped_response(SOURCE_NAME, "not_found", history_file=str(history_file), sessions_root=str(sessions_root)))
            return

        mapped_rollouts: dict[str, Path] = {}
        for rollout in rollout_files:
            meta = session_meta_from_rollout(rollout)
            if not meta:
                continue
            session_id = meta.get("id")
            if not session_id:
                continue
            mapped_rollouts[session_id] = rollout

        bounded = start is not None or end is not None
        events = []
        for session_id, rollout in sorted(mapped_rollouts.items()):

            history_entry = (
                filtered_history_index.get(session_id, {"timestamps": [], "user_excerpts": [], "user_messages": []})
                if bounded
                else full_history_index.get(session_id, {"timestamps": [], "user_excerpts": [], "user_messages": []})
            )
            meta_details: dict[str, object] | None = None
            records: list[dict[str, object]] = []

            with rollout.open(encoding="utf-8") as handle:
                for raw_line in handle:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        record = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue

                    records.append(record)
                    record_type = record.get("type")

                    if record_type == "session_meta":
                        payload = record.get("payload", {})
                        session_cwd = payload.get("cwd")
                        if workspace and not is_within_path(session_cwd, workspace):
                            meta_details = None
                            break
                        meta_details = payload

            if meta_details is None:
                continue

            session_workspace = str(meta_details.get("cwd") or "") or None
            turns = extract_codex_turns(records, start, end)
            logical_packets = build_codex_logical_packets(
                records,
                session_id=session_id,
                workspace=session_workspace,
                history_user_messages=[str(message) for message in history_entry.get("user_messages", [])],
                history_timestamps=list(history_entry.get("timestamps", [])),
                session_started_at=meta_details.get("timestamp"),
                gap_hours=args.gap_hours,
            )
            if not logical_packets:
                continue
            scoped_packets = [
                (packet_index, logical_packet)
                for packet_index, logical_packet in enumerate(logical_packets)
                if logical_packet_within_range(logical_packet, start, end)
            ]
            if not scoped_packets:
                continue

            serialized_packets: list[dict[str, object]] = []
            aggregated_tool_calls: list[dict[str, object]] = []
            group_timestamps: list[str] = []
            assistant_excerpts: list[str] = []
            event_user_excerpts: list[str] = []
            commentary_timestamps: list[str] = []
            tool_timestamps: list[str] = []
            tool_counter: Counter[str] = Counter()
            for packet_index, logical_packet in scoped_packets:
                packet_timestamps = [str(timestamp) for timestamp in logical_packet.get("timestamps", []) if str(timestamp or "").strip()]
                group_timestamps.extend(packet_timestamps)
                packet_user_messages = [str(message) for message in logical_packet.get("user_messages", [])]
                packet_assistant_messages = [str(message) for message in logical_packet.get("assistant_messages", [])]
                packet_tool_names = [str(tool) for tool in logical_packet.get("tools", []) if str(tool or "").strip()]
                packet_tool_calls = [detail for detail in logical_packet.get("tool_calls", []) if isinstance(detail, dict)]
                aggregated_tool_calls.extend([dict(detail) for detail in packet_tool_calls])

                user_excerpts = head_tail_excerpts(
                    packet_user_messages,
                    limit=USER_HIGHLIGHT_LIMIT,
                    max_items=MAX_USER_HIGHLIGHTS,
                )
                assistant_packet_excerpts = head_tail_excerpts(
                    packet_assistant_messages,
                    limit=ASSISTANT_HIGHLIGHT_LIMIT,
                    max_items=MAX_ASSISTANT_HIGHLIGHTS,
                )
                for excerpt in user_excerpts:
                    if excerpt not in event_user_excerpts and len(event_user_excerpts) < MAX_USER_HIGHLIGHTS:
                        event_user_excerpts.append(excerpt)
                for excerpt in assistant_packet_excerpts:
                    if excerpt not in assistant_excerpts and len(assistant_excerpts) < MAX_ASSISTANT_HIGHLIGHTS:
                        assistant_excerpts.append(excerpt)
                if packet_timestamps:
                    if user_excerpts or assistant_packet_excerpts:
                        commentary_timestamps.append(packet_timestamps[-1])
                    if packet_tool_calls:
                        tool_timestamps.append(
                            next(
                                (
                                    str(detail.get("timestamp"))
                                    for detail in reversed(packet_tool_calls)
                                    if str(detail.get("timestamp") or "").strip()
                                ),
                                packet_timestamps[-1],
                            )
                        )
                serialized_packets.append(
                    {
                        "packet_index": packet_index,
                        "started_at": logical_packet.get("started_at"),
                        "ended_at": logical_packet.get("ended_at"),
                        "session_id": session_id,
                        "cwd": session_workspace,
                        "message_count": logical_packet.get("message_count"),
                        "user_message_count": logical_packet.get("user_message_count"),
                        "assistant_message_count": logical_packet.get("assistant_message_count"),
                        "user_highlights": user_excerpts,
                        "assistant_highlights": assistant_packet_excerpts,
                        "assistant_summary": assistant_packet_excerpts[-1] if assistant_packet_excerpts else None,
                        "tool_signals": packet_tool_names,
                        "tool_call_details": [dict(detail) for detail in packet_tool_calls],
                        "referenced_files": list(logical_packet.get("referenced_files", [])),
                    }
                )
                for detail in packet_tool_calls:
                    tool_name = str(detail.get("name") or "").strip().lower()
                    if tool_name:
                        tool_counter[tool_name] += 1

            packet_window_start = earliest_iso_timestamp(group_timestamps)
            session_timestamp = (
                packet_window_start
                or (meta_details.get("timestamp") if not bounded else None)
                or ((history_entry["timestamps"][0] if history_entry["timestamps"] else None) if not bounded else None)
                or None
            )
            if session_timestamp is None:
                continue
            commentary_anchor = commentary_timestamps[-1] if commentary_timestamps else session_timestamp
            merged_user_excerpts = list(history_entry["user_excerpts"])
            for excerpt in event_user_excerpts:
                if excerpt not in merged_user_excerpts and len(merged_user_excerpts) < MAX_USER_HIGHLIGHTS:
                    merged_user_excerpts.append(excerpt)
            user_excerpt = merged_user_excerpts[0] if merged_user_excerpts else "No user prompt captured"
            referenced_files = list(
                dict.fromkeys(
                    str(path)
                    for packet in serialized_packets
                    for path in packet.get("referenced_files", [])
                    if str(path or "").strip()
                )
            )[:20]

            session_summary = f"Codex session in {meta_details.get('cwd', 'unknown workspace')}"
            if within_range(session_timestamp, start, end) or (start is None and end is None):
                events.append(
                    {
                        "source": SOURCE_NAME,
                        "timestamp": session_timestamp,
                        "type": "session_meta",
                        "summary": summarize_text(session_summary, 140),
                        "details": {
                            "session_id": session_id,
                            "cwd": meta_details.get("cwd"),
                            "originator": meta_details.get("originator"),
                            "cli_version": meta_details.get("cli_version"),
                            "model_provider": meta_details.get("model_provider"),
                            "git": meta_details.get("git"),
                            "turns": turns,
                            "logical_packets": serialized_packets,
                            "logical_packet_count": len(serialized_packets),
                            "user_highlights": merged_user_excerpts,
                            "assistant_highlights": assistant_excerpts,
                            "referenced_files": referenced_files,
                        },
                        "confidence": "medium",
                    }
                )
            if commentary_anchor and (within_range(commentary_anchor, start, end) or (start is None and end is None)):
                events.append(
                    {
                        "source": SOURCE_NAME,
                        "timestamp": commentary_anchor,
                        "type": "commentary",
                        "summary": f"Codex commentary: {summarize_text(user_excerpt, 96)}",
                        "details": {
                            "session_id": session_id,
                            "cwd": meta_details.get("cwd"),
                            "user_highlights": merged_user_excerpts,
                            "assistant_highlights": assistant_excerpts,
                            "referenced_files": referenced_files,
                        },
                        "confidence": "medium",
                    }
                )

            if tool_counter and tool_timestamps:
                tool_summary = ", ".join(f"{name} x{count}" for name, count in tool_counter.most_common(5))
                events.append(
                    {
                        "source": SOURCE_NAME,
                        "timestamp": tool_timestamps[-1],
                        "type": "tool_call",
                        "summary": f"Codex tool usage: {tool_summary}",
                        "details": {
                            "session_id": session_id,
                            "cwd": meta_details.get("cwd"),
                            "tools": [{"name": name, "count": count} for name, count in tool_counter.most_common()],
                            "total_calls": sum(tool_counter.values()),
                            "tool_call_details": aggregated_tool_calls[:8],
                        },
                        "confidence": "high",
                    }
                )

        events.sort(key=lambda event: event["timestamp"] or "", reverse=True)
        emit(
            success_response(
                SOURCE_NAME,
                apply_limit(events, args.limit),
                workspace=str(workspace) if workspace else None,
                since=args.since,
                until=args.until,
                all_sessions=args.all_sessions,
                scanned_rollouts=len(rollout_files),
            )
        )
    except PermissionError as exc:
        emit(
            skipped_response(
                SOURCE_NAME,
                "permission_denied",
                history_file=str(args.history_file),
                sessions_root=str(args.sessions_root),
                message=str(exc),
            )
        )
    except Exception as exc:
        emit(error_response(SOURCE_NAME, str(exc)))


if __name__ == "__main__":
    main()
