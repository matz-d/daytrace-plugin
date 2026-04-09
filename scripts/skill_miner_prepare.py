#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aggregate_core import load_expected_sources, resolve_sources_file_path
from common import LOCAL_TZ, current_platform, emit, ensure_datetime, error_response, resolve_workspace
from derived_store import (
    SLICE_COMPLETE,
    SLICE_STALE,
    evaluate_slice_completeness,
    get_observations,
    persist_patterns_from_prepare,
)
from skill_miner_common import (
    PRIMARY_INTENT_SOURCE_HIGHLIGHT,
    PRIMARY_INTENT_SOURCE_SUMMARY,
    CLAUDE_SOURCE,
    CODEX_SOURCE,
    DEFAULT_DECISION_LOG_PATH,
    DEFAULT_GAP_HOURS,
    DEFAULT_MAX_UNCLUSTERED,
    DEFAULT_RESEARCH_REF_LIMIT,
    DEFAULT_TOP_N,
    GENERIC_TASK_SHAPES,
    GENERIC_TOOL_SIGNATURES,
    INTENT_STOP_WORDS,
    OVERSIZED_CLUSTER_MIN_PACKETS,
    OVERSIZED_CLUSTER_MIN_SHARE,
    PREPARE_SOURCE,
    extract_known_commands,
    build_claude_session_ref,
    build_codex_session_ref,
    build_candidate_quality,
    build_candidate_content_key,
    build_candidate_decision_key,
    build_claude_logical_packets,
    build_codex_logical_packets,
    build_tool_call_detail,
    build_observation_contract,
    build_research_brief,
    build_research_targets,
    build_packet,
    candidate_label,
    candidate_score,
    candidate_sort_key,
    claude_message_text,
    codex_command_names,
    codex_message_text,
    compact_snippet,
    compare_iso_timestamps,
    earliest_iso_timestamp,
    jaccard_score,
    load_jsonl,
    overlap_score,
    packet_sort_key,
    packet_user_rule_hints,
    parse_session_ref,
    recent_packet_count,
    skill_miner_packet_is_v2,
    stable_block_keys,
    tokenize,
    annotate_unclustered_packet,
    workspace_matches,
)
from store import resolve_store_path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CLAUDE_ROOT = Path.home() / ".claude" / "projects"
DEFAULT_CODEX_HISTORY = Path.home() / ".codex" / "history.jsonl"
DEFAULT_CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
DEFAULT_SOURCES_FILE = SCRIPT_DIR / "sources.json"
DEFAULT_OBSERVATION_DAYS = 7
WORKSPACE_ADAPTIVE_EXPANDED_DAYS = 30
WORKSPACE_ADAPTIVE_MIN_PACKETS = 4
WORKSPACE_ADAPTIVE_MIN_CANDIDATES = 1
CLUSTER_MERGE_THRESHOLD = 0.55
CLUSTER_NEAR_MATCH_THRESHOLD = 0.45
COMPLETE_LINK_AUDIT_THRESHOLD = 0.5
STORE_HYDRATE_TIMEOUT_SEC = 90
COMPARE_LEGACY_OVERLAP_WARNING_THRESHOLD = 0.5

FIDELITY_ORIGINAL = "original"
FIDELITY_APPROXIMATE = "approximate"
FIDELITY_CANONICAL = "canonical"

# v2 target mix:
# - task_shapes: 0.30 (split between shared-shape overlap and exact specific-shape bonus)
# - snippet / intent: 0.25
# - artifacts: 0.20
# - rules: 0.20
# - tools: 0.05
SIMILARITY_WEIGHT_BUDGET = {
    "task_shapes": 0.22,
    "specific_shape_bonus": 0.08,
    "intent": 0.15,
    "snippet": 0.10,
    "artifacts": 0.20,
    "rules": 0.20,
    "tools": 0.05,
}
SIMILARITY_TASK_SHAPES_WEIGHT = SIMILARITY_WEIGHT_BUDGET["task_shapes"]
SIMILARITY_SPECIFIC_SHAPE_BONUS = SIMILARITY_WEIGHT_BUDGET["specific_shape_bonus"]
SIMILARITY_INTENT_WEIGHT = SIMILARITY_WEIGHT_BUDGET["intent"]
SIMILARITY_SNIPPET_WEIGHT = SIMILARITY_WEIGHT_BUDGET["snippet"]
SIMILARITY_ARTIFACT_WEIGHT = SIMILARITY_WEIGHT_BUDGET["artifacts"]
SIMILARITY_RULE_WEIGHT = SIMILARITY_WEIGHT_BUDGET["rules"]
SIMILARITY_TOOL_WEIGHT = SIMILARITY_WEIGHT_BUDGET["tools"]
SIMILARITY_GENERIC_ONLY_PENALTY = 0.08  # legacy single-tier value; kept for reference

# Staged generic-only penalty: full when both task AND tool are generic with no
# artifact/rule signal; partial when only one side is generic or when artifact/rule
# partially compensates.
GENERIC_PENALTY_FULL = 0.10
GENERIC_PENALTY_PARTIAL = 0.04
SIMILARITY_WEIGHT_TOTAL = sum(SIMILARITY_WEIGHT_BUDGET.values())

# Generic task shapes matching other generic shapes contribute only 30% of
# their overlap credit.  This prevents review_changes↔review_changes from
# inflating similarity to the same degree as implement_feature↔implement_feature.
GENERIC_SHAPE_DISCOUNT = 0.7


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare compressed skill-miner candidates from raw Claude/Codex history.")
    parser.add_argument("--workspace", default=".", help="Workspace path to filter by. Ignored with --all-sessions.")
    parser.add_argument("--all-sessions", action="store_true", help="Ignore workspace filtering while keeping the configured day window.")
    parser.add_argument("--days", type=int, default=DEFAULT_OBSERVATION_DAYS, help="Limit packets to the last N days.")
    parser.add_argument(
        "--input-source",
        choices=["raw", "store", "auto"],
        default="raw",
        help="Choose raw history, store-backed observations, or auto fallback.",
    )
    parser.add_argument("--store-path", help="Path to the DayTrace SQLite store. Used for store-backed prepare and pattern persistence.")
    parser.add_argument("--decision-log-path", help="Path to the persisted decision log used for next-run carry-forward handling.")
    parser.add_argument("--sources-file", help="Path to sources.json used when validating store-backed auto input.")
    parser.add_argument("--claude-root", default=str(DEFAULT_CLAUDE_ROOT), help="Claude projects root.")
    parser.add_argument("--codex-history-file", default=str(DEFAULT_CODEX_HISTORY), help="Codex history.jsonl path.")
    parser.add_argument("--codex-sessions-root", default=str(DEFAULT_CODEX_SESSIONS), help="Codex sessions root.")
    parser.add_argument("--gap-hours", type=int, default=DEFAULT_GAP_HOURS, help="Hours of inactivity that split Claude logical sessions.")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="Maximum number of candidates to return.")
    parser.add_argument(
        "--max-unclustered",
        type=int,
        default=DEFAULT_MAX_UNCLUSTERED,
        help="Maximum number of unclustered packets to include.",
    )
    parser.add_argument(
        "--dump-intents",
        action="store_true",
        help="Include anonymized primary_intent samples and summary metrics for B0 observation.",
    )
    parser.add_argument(
        "--compare-legacy",
        action="store_true",
        help="When using store-backed prepare, also compute a raw-history comparison summary.",
    )
    parser.add_argument(
        "--reference-date",
        default=None,
        help="Override today's date for the observation window cutoff (YYYY-MM-DD). Intended for testing.",
    )
    return parser


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.members = [{index} for index in range(size)]

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def union(self, left: int, right: int) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left != root_right:
            self.parent[root_right] = root_left
            self.members[root_left].update(self.members[root_right])
            self.members[root_right] = set()


def source_status(name: str, status: str, **extra: Any) -> dict[str, Any]:
    payload = {"name": name, "status": status}
    payload.update(extra)
    return payload


def resolve_decision_log_path(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else DEFAULT_DECISION_LOG_PATH.resolve()


RESURFACE_JACCARD_THRESHOLD = 0.3
RESURFACE_SUPPORT_MULTIPLIER = 2
RESURFACE_DAYS_ELAPSED = 30


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _should_resurface(candidate: dict[str, Any], prior_state: dict[str, Any]) -> bool:
    """Check if a user_rejected candidate should resurface based on evidence change."""
    # Condition 1: evidence_changed — intent_trace Jaccard distance > threshold
    prior_trace = set(prior_state.get("intent_trace") or [])
    current_trace = set(candidate.get("intent_trace") or [])
    if prior_trace and current_trace:
        union = prior_trace | current_trace
        intersection = prior_trace & current_trace
        jaccard_distance = 1.0 - (len(intersection) / len(union)) if union else 0.0
        if jaccard_distance > RESURFACE_JACCARD_THRESHOLD:
            return True

    # Condition 2: support_grew — packet count doubled since rejection
    prior_count = _coerce_int(prior_state.get("observation_count"), 0)
    current_support = candidate.get("support", {})
    current_count = (
        _coerce_int(current_support.get("total_packets", current_support.get("packets", 0)), 0)
        if isinstance(current_support, dict)
        else 0
    )
    if prior_count > 0 and current_count >= prior_count * RESURFACE_SUPPORT_MULTIPLIER:
        return True

    # Condition 3: time_elapsed — enough days since rejection
    decision_ts = prior_state.get("user_decision_timestamp")
    if decision_ts:
        try:
            decided_at = ensure_datetime(decision_ts)
            if decided_at:
                elapsed = (datetime.now(tz=timezone.utc) - decided_at).days
                if elapsed >= RESURFACE_DAYS_ELAPSED:
                    return True
        except (ValueError, TypeError):
            pass

    return False


def _decision_row_content_key(row: dict[str, Any]) -> str:
    explicit = str(row.get("content_key") or "").strip()
    if explicit:
        return explicit
    return build_candidate_content_key(row)


def _state_from_decision_row(row: dict[str, Any], *, decision_key: str, content_key: str) -> dict[str, Any]:
    carry_forward = row.get("carry_forward")
    if not isinstance(carry_forward, bool):
        carry_forward = True
    return {
        "decision_key": decision_key,
        "content_key": content_key,
        "candidate_id": row.get("candidate_id"),
        "label": row.get("label"),
        "suggested_kind": row.get("suggested_kind"),
        "intent_trace": list(row.get("intent_trace", [])) if isinstance(row.get("intent_trace"), list) else [],
        "constraints": list(row.get("constraints", [])) if isinstance(row.get("constraints"), list) else [],
        "acceptance_criteria": (
            list(row.get("acceptance_criteria", [])) if isinstance(row.get("acceptance_criteria"), list) else []
        ),
        "user_decision": row.get("user_decision"),
        "user_decision_timestamp": row.get("user_decision_timestamp"),
        "carry_forward": carry_forward,
        "observation_count": _coerce_int(row.get("observation_count"), 0),
        "prior_observation_count": _coerce_int(row.get("prior_observation_count"), 0),
        "observation_delta": _coerce_int(row.get("observation_delta"), 0),
        "recorded_at": row.get("recorded_at"),
    }


def load_latest_decision_states(
    path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    if not path.exists():
        empty_status = {"path": str(path), "status": "missing", "loaded_entries": 0, "active_entries": 0}
        return {}, {}, {**empty_status, "content_key_entries": 0}

    try:
        latest_by_key: dict[str, dict[str, Any]] = {}
        latest_by_content_key: dict[str, dict[str, Any]] = {}
        loaded_entries = 0
        for row in load_jsonl(path):
            if row.get("record_type") != "skill_miner_decision_stub":
                continue
            loaded_entries += 1
            decision_key = str(row.get("decision_key") or build_candidate_decision_key(row)).strip()
            if not decision_key:
                continue
            content_key = _decision_row_content_key(row)
            state = _state_from_decision_row(row, decision_key=decision_key, content_key=content_key)
            latest_by_key[decision_key] = state
            if content_key:
                latest_by_content_key[content_key] = state
        status = {
            "path": str(path),
            "status": "loaded",
            "loaded_entries": loaded_entries,
            "active_entries": len(latest_by_key),
            "content_key_entries": len(latest_by_content_key),
        }
        return latest_by_key, latest_by_content_key, status
    except Exception as exc:
        return {}, {}, {
            "path": str(path),
            "status": "error",
            "loaded_entries": 0,
            "active_entries": 0,
            "content_key_entries": 0,
            "message": str(exc),
        }


def apply_decision_states_to_candidates(
    candidates: list[dict[str, Any]],
    decision_states_by_key: dict[str, dict[str, Any]],
    decision_states_by_content_key: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    suppressed_items: list[dict[str, Any]] = []
    matched_candidates = 0
    content_key_migrations = 0

    for candidate in candidates:
        annotated = dict(candidate)
        content_key = build_candidate_content_key(annotated)
        annotated["content_key"] = content_key
        decision_key = build_candidate_decision_key(annotated)
        annotated["decision_key"] = decision_key
        state = decision_states_by_key.get(decision_key)
        if state is None:
            prior = decision_states_by_content_key.get(content_key)
            prior_kind = str(prior.get("suggested_kind") if prior else "").strip()
            current_kind = str(annotated.get("suggested_kind") or "").strip()
            if prior is not None and prior_kind != current_kind:
                state = prior
                annotated["classification_migrated"] = True
                content_key_migrations += 1
        if state is None:
            retained.append(annotated)
            continue
        matched_candidates += 1
        annotated["prior_decision_state"] = dict(state)
        if not state.get("carry_forward", True):
            suppressed_items.append(
                {
                    "decision_key": decision_key,
                    "label": annotated.get("label"),
                    "user_decision": state.get("user_decision"),
                    "recorded_at": state.get("recorded_at"),
                }
            )
            continue

        # Resurface check for user_rejected candidates
        user_decision = state.get("user_decision")
        if user_decision == "reject":
            if not _should_resurface(annotated, state):
                suppressed_items.append(
                    {
                        "decision_key": decision_key,
                        "label": annotated.get("label"),
                        "user_decision": "reject",
                        "recorded_at": state.get("recorded_at"),
                        "suppress_reason": "resurface_conditions_not_met",
                    }
                )
                continue

        retained.append(annotated)

    return retained, {
        "matched_candidates": matched_candidates,
        "content_key_migrations": content_key_migrations,
        "suppressed_candidates": len(suppressed_items),
        "suppressed_items": suppressed_items[:10],
    }


def _tag_fidelity(packet: dict[str, Any], fidelity: str) -> dict[str, Any]:
    packet["_fidelity"] = fidelity
    return packet


def _keep_claude_packet(packet: dict[str, Any]) -> bool:
    if bool(packet.get("is_sidechain")):
        return False
    return str(packet.get("primary_intent_source") or "").strip() != PRIMARY_INTENT_SOURCE_SUMMARY


def _combine_origin_hints(origin_hints: list[str]) -> str:
    normalized = [str(value).strip() for value in origin_hints if str(value).strip()]
    distinct = set(normalized)
    if not distinct:
        return ""
    if len(distinct) == 1:
        return next(iter(distinct))
    if "human" in distinct:
        return "mixed"
    if "parent_ai" in distinct:
        return "parent_ai"
    return "unknown"


def _combine_user_signal_strength(levels: list[str]) -> str:
    normalized = {str(value).strip() for value in levels if str(value).strip()}
    if not normalized:
        return "unknown"
    if "low" in normalized:
        return "low"
    if "medium" in normalized:
        return "medium"
    if "unknown" in normalized:
        return "unknown"
    return "high"


def _claude_store_session_key(observation: dict[str, Any]) -> str:
    details = observation.get("details", {})
    if not isinstance(details, dict):
        details = {}

    file_path = str(details.get("file_path") or "").strip()
    if file_path:
        return f"path:{file_path}"

    session_ref = str(details.get("session_ref") or "").strip()
    if session_ref.startswith("claude:"):
        try:
            _kind, ref_path, _epoch = parse_session_ref(session_ref)
        except (TypeError, ValueError):
            ref_path = ""
        if ref_path:
            return f"path:{ref_path}"

    session_id = str(details.get("session_id") or "").strip()
    if session_id:
        return f"session:{session_id}"

    packet_id = str(details.get("packet_id") or "").strip()
    if packet_id.startswith("claude:"):
        packet_prefix, _separator, _packet_index = packet_id.rpartition(":")
        if packet_prefix:
            return f"packet:{packet_prefix}"

    return str(observation.get("event_fingerprint") or packet_id or id(observation))


def read_claude_packets(root: Path, workspace: Path | None, gap_hours: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not root.exists():
        return [], source_status(CLAUDE_SOURCE, "skipped", reason="not_found", root=str(root))

    packets: list[dict[str, Any]] = []
    try:
        jsonl_files = sorted(root.glob("**/*.jsonl"))
        for path in jsonl_files:
            records = load_jsonl(path)
            logical_packets = build_claude_logical_packets(records, gap_hours)
            matched_packets = [
                lp for lp in logical_packets
                if workspace_matches(lp.get("cwd"), workspace)
            ]
            for packet_index, logical_packet in enumerate(matched_packets):
                packet_start = logical_packet.get("started_at")
                session_ref = build_claude_session_ref(str(path), packet_start)
                packets.append(
                    _tag_fidelity(
                        build_packet(
                            packet_id=f"claude:{path.parent.name}:{path.stem}:{packet_index:03d}",
                            source=CLAUDE_SOURCE,
                            session_ref=session_ref,
                            session_id=logical_packet.get("session_id"),
                            workspace=logical_packet.get("cwd"),
                            timestamp=packet_start,
                            user_messages=list(logical_packet.get("user_messages", [])),
                            assistant_messages=list(logical_packet.get("assistant_messages", [])),
                            tools=list(logical_packet.get("tools", [])),
                            tool_call_details=list(logical_packet.get("tool_calls", [])),
                            referenced_files=list(logical_packet.get("referenced_files", [])),
                            is_sidechain=bool(logical_packet.get("is_sidechain")),
                        ),
                        FIDELITY_ORIGINAL,
                    )
                )
        packets = [packet for packet in packets if _keep_claude_packet(packet)]
        return packets, source_status(CLAUDE_SOURCE, "success", packets_count=len(packets))
    except PermissionError as exc:
        return [], source_status(CLAUDE_SOURCE, "skipped", reason="permission_denied", message=str(exc), root=str(root))
    except Exception as exc:  # pragma: no cover - defensive surface
        return [], source_status(CLAUDE_SOURCE, "error", message=str(exc), root=str(root))


def read_codex_packets(
    history_file: Path,
    sessions_root: Path,
    workspace: Path | None,
    gap_hours: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not history_file.exists() or not sessions_root.exists():
        return [], source_status(
            CODEX_SOURCE,
            "skipped",
            reason="not_found",
            history_file=str(history_file),
            sessions_root=str(sessions_root),
        )

    packets: list[dict[str, Any]] = []
    try:
        history_by_session: dict[str, dict[str, Any]] = defaultdict(lambda: {"user_messages": [], "timestamps": []})
        for record in load_jsonl(history_file):
            session_id = record.get("session_id")
            if not session_id:
                continue
            history_by_session[session_id]["timestamps"].append(record.get("ts"))
            text = str(record.get("text") or "")
            if text:
                history_by_session[session_id]["user_messages"].append(text)

        rollout_files = sorted(sessions_root.glob("**/rollout-*.jsonl"))
        for rollout in rollout_files:
            records = load_jsonl(rollout)
            meta = None
            for record in records:
                if record.get("type") == "session_meta":
                    payload = record.get("payload", {})
                    if payload.get("id"):
                        meta = payload
                        break
            if not meta:
                continue
            cwd = meta.get("cwd")
            if not workspace_matches(cwd, workspace):
                continue

            session_id = str(meta.get("id"))
            history_entry = history_by_session.get(session_id, {})
            logical_packets = build_codex_logical_packets(
                records,
                session_id=session_id,
                workspace=str(cwd) if cwd else None,
                history_user_messages=list(history_entry.get("user_messages", [])),
                history_timestamps=list(history_entry.get("timestamps", [])),
                session_started_at=meta.get("timestamp"),
                gap_hours=gap_hours,
            )
            for packet_index, logical_packet in enumerate(logical_packets):
                if not logical_packet.get("user_messages") and not logical_packet.get("assistant_messages"):
                    continue
                packet = _tag_fidelity(
                    build_packet(
                        packet_id=f"codex:{session_id}:{packet_index:03d}",
                        source=CODEX_SOURCE,
                        session_ref=build_codex_session_ref(session_id, logical_packet.get("started_at")),
                        session_id=session_id,
                        workspace=str(cwd) if cwd else None,
                        timestamp=str(logical_packet.get("started_at") or "") or None,
                        user_messages=[str(message) for message in logical_packet.get("user_messages", [])],
                        assistant_messages=[str(message) for message in logical_packet.get("assistant_messages", [])],
                        tools=[str(tool) for tool in logical_packet.get("tools", [])],
                        tool_call_details=[detail for detail in logical_packet.get("tool_calls", []) if isinstance(detail, dict)],
                        referenced_files=list(logical_packet.get("referenced_files", [])),
                    ),
                    FIDELITY_ORIGINAL,
                )
                packets.append(packet)
        return packets, source_status(CODEX_SOURCE, "success", packets_count=len(packets))
    except PermissionError as exc:
        return [], source_status(
            CODEX_SOURCE,
            "skipped",
            reason="permission_denied",
            message=str(exc),
            history_file=str(history_file),
            sessions_root=str(sessions_root),
        )
    except Exception as exc:  # pragma: no cover - defensive surface
        return [], source_status(CODEX_SOURCE, "error", message=str(exc), history_file=str(history_file), sessions_root=str(sessions_root))


def collect_raw_packets(
    *,
    workspace: Path | None,
    claude_root: Path,
    codex_history_file: Path,
    codex_sessions_root: Path,
    gap_hours: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    claude_packets, claude_status = read_claude_packets(claude_root, workspace, gap_hours)
    codex_packets, codex_status = read_codex_packets(codex_history_file, codex_sessions_root, workspace, gap_hours)
    return claude_packets + codex_packets, [claude_status, codex_status]


def _dedupe_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_by_key: dict[tuple[str, ...], dict[str, Any]] = {}

    def observation_key(observation: dict[str, Any]) -> tuple[str, ...]:
        source_name = str(observation.get("source_name") or "")
        event_type = str(observation.get("event_type") or "")
        observation_kind = str(observation.get("observation_kind") or "event")
        details = observation.get("details", {})
        if not isinstance(details, dict):
            details = {}
        if observation_kind == "packet":
            packet_id = str(details.get("packet_id") or details.get("session_ref") or observation.get("event_fingerprint") or "")
            return (source_name, observation_kind, packet_id)
        occurred_at = str(observation.get("occurred_at") or "")
        if source_name == CLAUDE_SOURCE:
            file_path = str(details.get("file_path") or details.get("session_id") or observation.get("event_fingerprint") or "")
            return (source_name, event_type, file_path, occurred_at)
        if source_name == CODEX_SOURCE:
            session_id = str(details.get("session_id") or observation.get("event_fingerprint") or "")
            return (source_name, event_type, session_id, occurred_at)
        return (source_name, event_type, str(observation.get("event_fingerprint") or ""))

    for observation in observations:
        key = observation_key(observation)
        current = latest_by_key.get(key)
        if current is None:
            latest_by_key[key] = observation
            continue
        current_collected = compare_iso_timestamps(current.get("collected_at"))
        candidate_collected = compare_iso_timestamps(observation.get("collected_at"))
        if candidate_collected > current_collected:
            latest_by_key[key] = observation
            continue
        if candidate_collected == current_collected and int(observation.get("observation_id") or 0) > int(current.get("observation_id") or 0):
            latest_by_key[key] = observation
    return sorted(latest_by_key.values(), key=lambda item: int(item.get("observation_id") or 0))


def _append_unique_texts(bucket: list[str], values: list[Any]) -> None:
    for value in values:
        text = str(value or "").strip()
        if text and text not in bucket:
            bucket.append(text)


def _stored_skill_miner_packet(value: Any, *, source_name: str, fallback_workspace: str | None = None) -> dict[str, Any] | None:
    if not skill_miner_packet_is_v2(value):
        return None
    packet = dict(value)
    packet["user_rule_hints"] = list(packet.get("user_rule_hints", packet.get("user_repeated_rules", [])))
    packet["assistant_rule_hints"] = list(packet.get("assistant_rule_hints", packet.get("assistant_repeated_rules", [])))
    packet["tool_trace"] = list(packet.get("tool_trace", packet.get("tool_signature", [])))
    packet["tool_argument_patterns"] = list(packet.get("tool_argument_patterns", []))
    packet["tool_call_examples"] = list(packet.get("tool_call_examples", []))
    intent_tool_alignment = packet.get("intent_tool_alignment")
    packet["intent_tool_alignment"] = dict(intent_tool_alignment) if isinstance(intent_tool_alignment, dict) else {
        "status": "unknown",
        "matched_tools": [],
        "expected_tools": [],
        "reason": "missing",
    }
    workflow_signals = packet.get("workflow_signals")
    packet["workflow_signals"] = dict(workflow_signals) if isinstance(workflow_signals, dict) else {
        "flags": [],
        "counts": {"failure": 0, "retry": 0, "pivot": 0},
        "failure_hints": [],
        "retry_hints": [],
        "pivot_hints": [],
    }
    packet["source"] = source_name
    packet["repeated_rules"] = list(packet.get("user_repeated_rules", []))
    intent_trace = packet.get("intent_trace")
    if not isinstance(intent_trace, list):
        intent_trace = []
    if not intent_trace:
        fallback_trace = [
            str(packet.get("full_user_intent") or "").strip(),
            str(packet.get("primary_intent") or "").strip(),
        ]
        intent_trace = [value for value in fallback_trace if value]
    packet["intent_trace"] = intent_trace[:4]
    constraints = packet.get("constraints")
    packet["constraints"] = list(constraints) if isinstance(constraints, list) else []
    acceptance_criteria = packet.get("acceptance_criteria")
    packet["acceptance_criteria"] = list(acceptance_criteria) if isinstance(acceptance_criteria, list) else []
    if fallback_workspace and not packet.get("workspace"):
        packet["workspace"] = fallback_workspace
    packet["_fidelity"] = str(packet.get("_fidelity") or FIDELITY_CANONICAL)
    return packet


def _store_packets_fidelity(packets: list[dict[str, Any]]) -> str:
    if not packets:
        return FIDELITY_APPROXIMATE
    return FIDELITY_APPROXIMATE if any(str(packet.get("_fidelity") or "") != FIDELITY_CANONICAL for packet in packets) else FIDELITY_CANONICAL


def _packet_from_packet_observation(observation: dict[str, Any]) -> dict[str, Any] | None:
    details = observation.get("details", {})
    if not isinstance(details, dict):
        details = {}
    source_name = str(observation.get("source_name") or "")
    fallback_workspace = str(details.get("workspace") or observation.get("workspace") or "").strip() or None
    return _stored_skill_miner_packet(
        details.get("skill_miner_packet") or details,
        source_name=source_name,
        fallback_workspace=fallback_workspace,
    )


def _packet_from_claude_observation(observation: dict[str, Any]) -> list[dict[str, Any]]:
    details = observation.get("details", {})
    if not isinstance(details, dict):
        details = {}
    session_id = str(details.get("session_id") or "").strip() or None
    file_path = str(details.get("file_path") or "").strip()
    summary = str(observation.get("summary") or "")
    summary_prefix = "Claude session: "
    first_prompt = summary[len(summary_prefix) :] if summary.startswith(summary_prefix) else summary
    workspace = str(details.get("cwd") or observation.get("workspace") or "")
    ai_observation_packets = details.get("ai_observation_packets")
    if isinstance(ai_observation_packets, list) and ai_observation_packets:
        rebuilt_packets = [
            stored_packet
            for stored_packet in (
                _stored_skill_miner_packet(
                    packet,
                    source_name=CLAUDE_SOURCE,
                    fallback_workspace=workspace or None,
                )
                for packet in ai_observation_packets
                if isinstance(packet, dict)
            )
            if stored_packet is not None
        ]
        if rebuilt_packets:
            return rebuilt_packets
    stored_summary_packet = _stored_skill_miner_packet(
        details.get("ai_observation"),
        source_name=CLAUDE_SOURCE,
        fallback_workspace=workspace or None,
    )
    if stored_summary_packet is not None:
        return [stored_summary_packet]
    logical_packets = details.get("logical_packets")
    if isinstance(logical_packets, list) and logical_packets:
        rebuilt_packets: list[dict[str, Any]] = []
        for packet_index, logical_packet in enumerate(logical_packets):
            if not isinstance(logical_packet, dict):
                continue
            stored_packet = _stored_skill_miner_packet(
                logical_packet.get("ai_observation") or logical_packet.get("skill_miner_packet"),
                source_name=CLAUDE_SOURCE,
                fallback_workspace=workspace or None,
            )
            if stored_packet is not None:
                rebuilt_packets.append(stored_packet)
                continue
            user_messages: list[str] = []
            assistant_messages: list[str] = []
            tools: list[str] = []
            user_message_source = PRIMARY_INTENT_SOURCE_HIGHLIGHT
            user_highlights = logical_packet.get("user_highlights")
            if isinstance(user_highlights, list):
                _append_unique_texts(user_messages, user_highlights)
            assistant_highlights = logical_packet.get("assistant_highlights")
            if isinstance(assistant_highlights, list):
                _append_unique_texts(assistant_messages, assistant_highlights)
            assistant_summary = str(logical_packet.get("assistant_summary") or "").strip()
            if assistant_summary and assistant_summary not in assistant_messages:
                assistant_messages.append(assistant_summary)
            tool_signals = logical_packet.get("tool_signals")
            if isinstance(tool_signals, list):
                tools.extend(str(item).strip() for item in tool_signals if str(item or "").strip())
            if not user_messages and first_prompt:
                user_messages.append(first_prompt)
                user_message_source = PRIMARY_INTENT_SOURCE_SUMMARY
            packet_start = str(logical_packet.get("started_at") or observation["occurred_at"])
            tool_call_details = logical_packet.get("tool_call_details")
            rebuilt_packets.append(
                _tag_fidelity(
                    build_packet(
                        packet_id=f"claude-store:{session_id or observation['event_fingerprint']}:{packet_index:03d}",
                        source=CLAUDE_SOURCE,
                        session_ref=build_claude_session_ref(
                            file_path or f"store:{session_id or observation['event_fingerprint']}",
                            packet_start,
                        ),
                        session_id=str(logical_packet.get("session_id") or session_id or "").strip() or None,
                        workspace=str(logical_packet.get("cwd") or workspace or ""),
                        timestamp=packet_start,
                        user_messages=user_messages,
                        assistant_messages=assistant_messages,
                        tools=tools,
                        tool_call_details=[detail for detail in tool_call_details if isinstance(detail, dict)] if isinstance(tool_call_details, list) else [],
                        user_message_source=user_message_source,
                        is_sidechain=bool(logical_packet.get("is_sidechain")),
                    ),
                    FIDELITY_APPROXIMATE,
                )
            )
        if rebuilt_packets:
            return rebuilt_packets

    user_messages: list[str] = []
    user_message_source = PRIMARY_INTENT_SOURCE_HIGHLIGHT
    user_highlights = details.get("user_highlights")
    if isinstance(user_highlights, list):
        _append_unique_texts(user_messages, user_highlights)
    elif not user_messages:
        highlights = details.get("highlights")
        if isinstance(highlights, list):
            _append_unique_texts(user_messages, highlights)
    if not user_messages and first_prompt:
        user_messages.append(first_prompt)
        user_message_source = PRIMARY_INTENT_SOURCE_SUMMARY

    assistant_messages: list[str] = []
    assistant_highlights = details.get("assistant_highlights")
    if isinstance(assistant_highlights, list):
        _append_unique_texts(assistant_messages, assistant_highlights)
    assistant_summary = str(details.get("assistant_summary") or "").strip()
    if assistant_summary and assistant_summary not in assistant_messages:
        assistant_messages.append(assistant_summary)

    session_ref = build_claude_session_ref(
        file_path or f"store:{session_id or observation['event_fingerprint']}",
        str(observation["occurred_at"]),
    )
    return [
        _tag_fidelity(
            build_packet(
                packet_id=f"claude-store:{session_id or observation['event_fingerprint']}",
                source=CLAUDE_SOURCE,
                session_ref=session_ref,
                session_id=session_id,
                workspace=workspace,
                timestamp=str(observation["occurred_at"]),
                user_messages=user_messages,
                assistant_messages=assistant_messages,
                tools=[],
                user_message_source=user_message_source,
                is_sidechain=bool(details.get("is_sidechain")),
            ),
            FIDELITY_APPROXIMATE,
        )
    ]


def _packet_from_codex_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(observations, key=lambda item: compare_iso_timestamps(item.get("occurred_at")))
    anchor = ordered[-1]
    rebuilt_packets: list[dict[str, Any]] = []
    for observation in ordered:
        details = observation.get("details", {})
        if not isinstance(details, dict):
            continue
        ai_observation_packets = details.get("ai_observation_packets")
        if isinstance(ai_observation_packets, list) and ai_observation_packets:
            packets = [
                stored_packet
                for stored_packet in (
                    _stored_skill_miner_packet(
                        packet,
                        source_name=CODEX_SOURCE,
                        fallback_workspace=str(details.get("cwd") or "").strip() or None,
                    )
                    for packet in ai_observation_packets
                    if isinstance(packet, dict)
                )
                if stored_packet is not None
            ]
            if packets:
                return packets
        logical_packets = details.get("logical_packets")
        if isinstance(logical_packets, list) and logical_packets:
            packets = [
                stored_packet
                for stored_packet in (
                    _stored_skill_miner_packet(
                        logical_packet.get("ai_observation") or logical_packet.get("skill_miner_packet"),
                        source_name=CODEX_SOURCE,
                        fallback_workspace=str(logical_packet.get("cwd") or details.get("cwd") or "").strip() or None,
                    )
                    for logical_packet in logical_packets
                    if isinstance(logical_packet, dict)
                )
                if stored_packet is not None
            ]
            if packets:
                return packets
        stored_packet = _stored_skill_miner_packet(
            details.get("ai_observation") or details.get("skill_miner_packet"),
            source_name=CODEX_SOURCE,
            fallback_workspace=str(details.get("cwd") or "").strip() or None,
        )
        if stored_packet is not None:
            return [stored_packet]

    session_id = None
    workspace = None
    timestamps: list[str] = []
    user_messages: list[str] = []
    assistant_messages: list[str] = []
    tools: list[str] = []
    tool_call_details: list[dict[str, Any]] = []
    user_message_source = PRIMARY_INTENT_SOURCE_HIGHLIGHT

    for observation in ordered:
        details = observation.get("details", {})
        if not isinstance(details, dict):
            details = {}
        timestamps.append(str(observation["occurred_at"]))
        if session_id is None:
            raw_session_id = str(details.get("session_id") or "").strip()
            session_id = raw_session_id or None
        if workspace is None:
            raw_workspace = str(details.get("cwd") or "").strip()
            workspace = raw_workspace or None

        event_type = str(observation.get("event_type") or "")
        if event_type == "commentary":
            user_highlights = details.get("user_highlights")
            if isinstance(user_highlights, list):
                _append_unique_texts(user_messages, user_highlights)
            assistant_highlights = details.get("assistant_highlights")
            if isinstance(assistant_highlights, list):
                _append_unique_texts(assistant_messages, assistant_highlights)
            if not user_messages and not assistant_messages:
                summary = str(observation.get("summary") or "").strip()
                if summary:
                    user_messages.append(summary)
                    user_message_source = PRIMARY_INTENT_SOURCE_SUMMARY
        elif event_type == "tool_call":
            tool_items = details.get("tools")
            if isinstance(tool_items, list):
                for tool in tool_items:
                    if not isinstance(tool, dict):
                        continue
                    name = str(tool.get("name") or "").strip().lower()
                    try:
                        count = int(tool.get("count") or 0)
                    except (TypeError, ValueError):
                        count = 0
                    if name and count > 0:
                        tools.extend([name] * count)
            raw_tool_call_details = details.get("tool_call_details")
            if isinstance(raw_tool_call_details, list):
                tool_call_details.extend([detail for detail in raw_tool_call_details if isinstance(detail, dict)])

    timestamp = earliest_iso_timestamp(timestamps) or str(anchor["occurred_at"])
    session_ref = build_codex_session_ref(session_id or f"store-{anchor['event_fingerprint']}", timestamp)
    rebuilt_packets.append(
        _tag_fidelity(
            build_packet(
                packet_id=f"codex-store:{session_id or anchor['event_fingerprint']}",
                source=CODEX_SOURCE,
                session_ref=session_ref,
                session_id=session_id,
                workspace=workspace,
                timestamp=timestamp,
                user_messages=user_messages,
                assistant_messages=assistant_messages,
                tools=tools,
                tool_call_details=tool_call_details,
                user_message_source=user_message_source,
            ),
            FIDELITY_APPROXIMATE,
        )
    )
    return rebuilt_packets


def read_store_packets(
    store_path: Path,
    *,
    workspace: Path | None,
    all_sessions: bool,
    max_days: int,
    reference_now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    effective_now = reference_now or datetime.now(timezone.utc).astimezone()
    since_date, _until_date = _store_slice_bounds(reference_now=effective_now, days=max_days)
    packet_observations = get_observations(
        store_path,
        workspace=workspace,
        since=since_date,
        all_sessions=all_sessions,
        source_names=[CLAUDE_SOURCE, CODEX_SOURCE],
        observation_kinds=["packet"],
    )
    packets: list[dict[str, Any]] = []
    claude_packet_count = 0
    codex_packet_count = 0
    deduped_packet_observations = _dedupe_observations(packet_observations)
    packet_observations_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in deduped_packet_observations:
        source_name = str(observation.get("source_name") or "")
        if source_name in {CLAUDE_SOURCE, CODEX_SOURCE}:
            packet_observations_by_source[source_name].append(observation)

    fallback_sources = {CLAUDE_SOURCE, CODEX_SOURCE}
    claude_fallback_session_keys: set[str] = set()
    claude_packet_observations = packet_observations_by_source.get(CLAUDE_SOURCE, [])
    if claude_packet_observations:
        claude_packets_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for observation in claude_packet_observations:
            session_key = _claude_store_session_key(observation)
            stored_packet = _packet_from_packet_observation(observation)
            if stored_packet is None or not _keep_claude_packet(stored_packet):
                claude_fallback_session_keys.add(session_key)
                continue
            claude_packets_by_session[session_key].append(stored_packet)
        for session_key, source_packets in claude_packets_by_session.items():
            if session_key in claude_fallback_session_keys:
                continue
            packets.extend(source_packets)
            claude_packet_count += len(source_packets)
        if not claude_fallback_session_keys:
            fallback_sources.discard(CLAUDE_SOURCE)

    for source_name in (CODEX_SOURCE,):
        source_observations = packet_observations_by_source.get(source_name, [])
        if not source_observations:
            continue
        source_packets: list[dict[str, Any]] = []
        invalid_packet_found = False
        for observation in source_observations:
            stored_packet = _packet_from_packet_observation(observation)
            if stored_packet is None:
                invalid_packet_found = True
                break
            source_packets.append(stored_packet)
        if invalid_packet_found or not source_packets:
            continue
        packets.extend(source_packets)
        fallback_sources.discard(source_name)
        codex_packet_count += len(source_packets)

    if fallback_sources:
        observations = get_observations(
            store_path,
            workspace=workspace,
            since=since_date,
            all_sessions=all_sessions,
            source_names=sorted(fallback_sources),
            observation_kinds=["event"],
        )
        deduped_observations = _dedupe_observations(observations)
        claude_observations = [observation for observation in deduped_observations if observation["source_name"] == CLAUDE_SOURCE]
        codex_observations = [observation for observation in deduped_observations if observation["source_name"] == CODEX_SOURCE]
        if claude_fallback_session_keys:
            claude_observations = [
                observation
                for observation in claude_observations
                if _claude_store_session_key(observation) in claude_fallback_session_keys
            ]

        claude_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for observation in claude_observations:
            claude_groups[_claude_store_session_key(observation)].append(observation)
        for group in claude_groups.values():
            preferred_observation = next(
                (item for item in group if str(item.get("event_type") or "") == "session_summary"),
                group[-1],
            )
            claude_packets = [packet for packet in _packet_from_claude_observation(preferred_observation) if _keep_claude_packet(packet)]
            packets.extend(claude_packets)
            claude_packet_count += len(claude_packets)

        codex_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for observation in codex_observations:
            details = observation.get("details", {})
            session_id = None
            if isinstance(details, dict):
                raw_session_id = str(details.get("session_id") or "").strip()
                session_id = raw_session_id or None
            codex_groups[session_id or str(observation["event_fingerprint"])].append(observation)
        for group in codex_groups.values():
            codex_packets = _packet_from_codex_observations(group)
            packets.extend(codex_packets)
            codex_packet_count += len(codex_packets)

    statuses = [
        source_status(
            CLAUDE_SOURCE,
            "success" if claude_packet_count else "skipped",
            packets_count=claude_packet_count,
            reason=None if claude_packet_count else "store_empty",
        ),
        source_status(
            CODEX_SOURCE,
            "success" if codex_packet_count else "skipped",
            packets_count=codex_packet_count,
            reason=None if codex_packet_count else "store_empty",
        ),
    ]
    return packets, statuses


SKILL_MINER_EXPECTED_SOURCES = {CLAUDE_SOURCE, CODEX_SOURCE}


def _is_store_slice_sufficient(
    packets: list[dict[str, Any]],
    completeness: dict[str, Any] | None,
) -> bool:
    return bool(packets) and completeness is not None and completeness["status"] == SLICE_COMPLETE and _store_packets_fidelity(packets) == FIDELITY_CANONICAL


def _store_slice_bounds(*, reference_now: datetime, days: int) -> tuple[str, str]:
    local_now = reference_now.astimezone()
    # Add 1-day buffer on both ends to capture data whose local date
    # differs from LOCAL_TZ date due to timezone offsets.
    # The precise filtering is handled by filter_packets_by_days.
    start_date = (local_now - timedelta(days=days + 1)).date().isoformat()
    end_date = (local_now + timedelta(days=1)).date().isoformat()
    return start_date, end_date


def _store_reference_now(reference_date: str | None) -> datetime:
    if reference_date:
        return datetime.combine(date.fromisoformat(reference_date), datetime.min.time(), tzinfo=LOCAL_TZ)
    return datetime.now(timezone.utc).astimezone()


def _hydrate_store_slice(
    store_path: Path,
    *,
    workspace: Path,
    all_sessions: bool,
    since: str,
    until: str,
    sources_file: str | None,
) -> None:
    # We intentionally allow overlapping hydrate windows here.
    # `evaluate_slice_completeness()` already reuses broader covering slices before hydration,
    # and `read_store_packets()` collapses overlapping observations across source_run boundaries.
    source_names = _resolve_skill_miner_source_names(
        sources_file=sources_file,
        workspace=workspace,
    )
    command = [
        "python3",
        str(SCRIPT_DIR / "aggregate.py"),
        "--workspace",
        str(workspace),
        "--since",
        since,
        "--until",
        until,
        "--store-path",
        str(store_path),
    ]
    for source_name in source_names:
        command.extend(["--source", source_name])
    if sources_file:
        command.extend(["--sources-file", str(resolve_sources_file_path(sources_file, default_sources_file=DEFAULT_SOURCES_FILE))])
    if all_sessions:
        command.append("--all-sessions")
    try:
        completed = subprocess.run(
            command,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=STORE_HYDRATE_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"skill-miner store hydration timed out after {STORE_HYDRATE_TIMEOUT_SEC}s") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        message = stderr or stdout or "no aggregate output"
        raise RuntimeError(f"skill-miner store hydration failed: {message}")


def _evaluate_store_slice_completeness(
    store_path: Path,
    *,
    workspace: Path | None,
    expected_sources_workspace: Path,
    all_sessions: bool,
    since: str,
    until: str,
    sources_file: str | None,
) -> dict[str, Any] | None:
    """Return completeness metadata for the current store slice."""
    expected_source_metadata = _load_skill_miner_expected_source_metadata(
        sources_file=sources_file,
        workspace=expected_sources_workspace,
    )
    if expected_source_metadata is None:
        return None
    expected_names, expected_fingerprints = expected_source_metadata
    return evaluate_slice_completeness(
        store_path,
        workspace=workspace,
        since=since,
        until=until,
        all_sessions=all_sessions,
        expected_source_names=expected_names,
        expected_fingerprints=expected_fingerprints or None,
    )


def _load_skill_miner_expected_source_metadata(
    *,
    sources_file: str | None,
    workspace: Path,
) -> tuple[set[str], dict[str, str]] | None:
    resolved_sources_file = resolve_sources_file_path(
        sources_file,
        default_sources_file=DEFAULT_SOURCES_FILE,
    )
    try:
        return load_expected_sources(
            resolved_sources_file,
            platform_name=current_platform(),
            workspace=workspace,
            script_dir=SCRIPT_DIR,
            restrict_to_names=SKILL_MINER_EXPECTED_SOURCES,
        )
    except Exception as exc:
        print(f"[warn] failed to load sources from {resolved_sources_file}: {exc}", file=sys.stderr)
        return None


def _resolve_skill_miner_source_names(
    *,
    sources_file: str | None,
    workspace: Path,
) -> list[str]:
    expected_source_metadata = _load_skill_miner_expected_source_metadata(
        sources_file=sources_file,
        workspace=workspace,
    )
    if expected_source_metadata is None:
        return sorted(SKILL_MINER_EXPECTED_SOURCES)
    expected_names, _expected_fingerprints = expected_source_metadata
    return sorted(expected_names) or sorted(SKILL_MINER_EXPECTED_SOURCES)


def _should_persist_patterns(
    *,
    selected_input_source: str,
    input_fidelity: str,
    source_statuses: list[dict[str, Any]],
    top_candidates: list[dict[str, Any]],
    no_sources_available: bool,
    store_slice_completeness: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    if not top_candidates:
        return False, "no_candidates"
    if no_sources_available:
        return False, "no_sources_available"
    unsafe_sources = [str(status.get("name") or "unknown") for status in source_statuses if status.get("status") != "success"]
    if unsafe_sources:
        return False, f"source_status_not_success:{','.join(sorted(unsafe_sources))}"
    if input_fidelity == FIDELITY_APPROXIMATE:
        return False, "input_fidelity_approximate"
    if selected_input_source == "store" and store_slice_completeness is not None:
        if store_slice_completeness.get("status") == SLICE_STALE:
            return False, f"store_slice_{store_slice_completeness.get('status', 'unknown')}"
    return True, None


def _build_similarity_features(packet: dict[str, Any]) -> dict[str, Any]:
    workspace = packet.get("workspace")
    snippets = packet.get("representative_snippets", [])
    snippet_tokens = set().union(*(tokenize(compact_snippet(item, workspace)) for item in snippets)) if snippets else set()
    intent_corpus = " ".join(
        [
            str(packet.get("primary_intent") or ""),
            *[str(value) for value in packet.get("intent_trace", [])[:2] if value],
            *[str(value) for value in packet.get("constraints", [])[:2] if value],
            *[str(value) for value in packet.get("acceptance_criteria", [])[:2] if value],
        ]
    )
    intent_tokens = tokenize(intent_corpus) - INTENT_STOP_WORDS
    task_shape_set = set(packet.get("task_shape", []))
    tool_set = set(packet.get("tool_signature", []))
    tool_pattern_set = set(packet.get("tool_argument_patterns", []))
    task_shapes_strs = [str(shape) for shape in packet.get("task_shape", []) if shape]
    primary_non_generic = next((shape for shape in task_shapes_strs if shape not in GENERIC_TASK_SHAPES), "")
    rule_names = {
        str(item.get("normalized") or "")
        for item in packet_user_rule_hints(packet)
        if isinstance(item, dict) and item.get("normalized")
    }
    return {
        "snippet_tokens": snippet_tokens,
        "intent_tokens": intent_tokens,
        "task_shape_set": task_shape_set,
        "tool_set": tool_set,
        "tool_pattern_set": tool_pattern_set,
        "artifact_set": set(packet.get("artifact_hints", [])),
        "primary_artifact": next((str(value) for value in packet.get("artifact_hints", []) if value), ""),
        "rule_names": rule_names,
        "primary_non_generic_shape": primary_non_generic,
        "generic_task_only": bool(task_shape_set) and task_shape_set <= GENERIC_TASK_SHAPES,
        "generic_tool_only": bool(tool_set) and tool_set <= GENERIC_TOOL_SIGNATURES,
    }


def _generic_discounted_overlap(left: set[str], right: set[str]) -> float:
    """Overlap score for task shapes, discounting generic-only intersections.

    Specific shape matches receive full credit while generic-generic matches
    (e.g. review_changes ↔ review_changes) are scaled by GENERIC_SHAPE_DISCOUNT.
    Uses max(len) as divisor to stay consistent with overlap_score().
    """
    if not left or not right:
        return 0.0
    divisor = max(len(left), len(right))
    if not divisor:
        return 0.0
    intersection = left & right
    specific_count = len(intersection - GENERIC_TASK_SHAPES)
    generic_count = len(intersection & GENERIC_TASK_SHAPES)
    return (specific_count + generic_count * GENERIC_SHAPE_DISCOUNT) / divisor


def _similarity_score_from_features(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_snippet_tokens = left.get("snippet_tokens", set())
    right_snippet_tokens = right.get("snippet_tokens", set())
    left_intent_tokens = left.get("intent_tokens", set())
    right_intent_tokens = right.get("intent_tokens", set())
    left_task_shapes = left.get("task_shape_set", set())
    right_task_shapes = right.get("task_shape_set", set())
    left_tool_set = left.get("tool_set", set())
    right_tool_set = right.get("tool_set", set())
    left_tool_patterns = left.get("tool_pattern_set", set())
    right_tool_patterns = right.get("tool_pattern_set", set())
    left_artifacts = left.get("artifact_set", set())
    right_artifacts = right.get("artifact_set", set())
    left_rule_names = left.get("rule_names", set())
    right_rule_names = right.get("rule_names", set())
    snippet = jaccard_score(left_snippet_tokens, right_snippet_tokens)
    intent = jaccard_score(left_intent_tokens, right_intent_tokens)
    task_shapes = _generic_discounted_overlap(left_task_shapes, right_task_shapes)
    tools = max(
        jaccard_score(left_tool_set, right_tool_set),
        jaccard_score(left_tool_patterns, right_tool_patterns),
    )
    left_primary_artifact = str(left.get("primary_artifact") or "")
    right_primary_artifact = str(right.get("primary_artifact") or "")
    primary_artifact_match = 1.0 if left_primary_artifact and left_primary_artifact == right_primary_artifact else 0.0
    artifacts = max(overlap_score(left_artifacts, right_artifacts), primary_artifact_match)
    rules = overlap_score(left_rule_names, right_rule_names)
    left_specific = str(left.get("primary_non_generic_shape") or "")
    right_specific = str(right.get("primary_non_generic_shape") or "")
    same_specific_shape = 1.0 if left_specific and left_specific == right_specific else 0.0
    generic_task_only = bool(left.get("generic_task_only")) and bool(right.get("generic_task_only"))
    generic_tool_only = bool(left.get("generic_tool_only")) and bool(right.get("generic_tool_only"))
    score = (
        (task_shapes * SIMILARITY_TASK_SHAPES_WEIGHT)
        + (intent * SIMILARITY_INTENT_WEIGHT)
        + (snippet * SIMILARITY_SNIPPET_WEIGHT)
        + (artifacts * SIMILARITY_ARTIFACT_WEIGHT)
        + (rules * SIMILARITY_RULE_WEIGHT)
        + (tools * SIMILARITY_TOOL_WEIGHT)
        + (same_specific_shape * SIMILARITY_SPECIFIC_SHAPE_BONUS)
    )
    if generic_task_only and generic_tool_only:
        if artifacts == 0.0 and rules == 0.0:
            score -= GENERIC_PENALTY_FULL
        else:
            score -= GENERIC_PENALTY_PARTIAL
    elif (generic_task_only or generic_tool_only) and artifacts == 0.0 and rules == 0.0:
        score -= GENERIC_PENALTY_PARTIAL
    return round(max(0.0, min(score, 1.0)), 3)


def similarity_score(left: dict[str, Any], right: dict[str, Any]) -> float:
    return _similarity_score_from_features(
        _build_similarity_features(left),
        _build_similarity_features(right),
    )


def filter_packets_by_days(packets: list[dict[str, Any]], days: int, reference_date: date | None = None) -> tuple[list[dict[str, Any]], str | None]:
    if days <= 0:
        raise ValueError("--days must be a positive integer")
    today = reference_date if reference_date is not None else datetime.now(LOCAL_TZ).date()
    threshold_date = today - timedelta(days=days)
    filtered: list[dict[str, Any]] = []
    for packet in packets:
        timestamp = ensure_datetime(packet.get("timestamp"))
        if timestamp is None:
            continue
        if timestamp.astimezone(LOCAL_TZ).date() >= threshold_date:
            filtered.append(packet)
    return filtered, datetime.combine(threshold_date, datetime.min.time(), tzinfo=LOCAL_TZ).isoformat()


def prepare_window_result(packets: list[dict[str, Any]], days: int, reference_date: date | None = None) -> dict[str, Any]:
    filtered_packets, date_window_start = filter_packets_by_days(packets, days, reference_date)
    candidates, unclustered, stats = cluster_packets(filtered_packets)
    return {
        "packets": filtered_packets,
        "candidates": candidates,
        "unclustered": unclustered,
        "stats": stats,
        "date_window_start": date_window_start,
        "days": days,
    }


def adaptive_window_decision(window_result: dict[str, Any], initial_days: int) -> tuple[bool, str | None]:
    if initial_days >= WORKSPACE_ADAPTIVE_EXPANDED_DAYS:
        return False, None
    packet_count = len(window_result["packets"])
    candidate_count = len(window_result["candidates"])
    if packet_count < WORKSPACE_ADAPTIVE_MIN_PACKETS and candidate_count < WORKSPACE_ADAPTIVE_MIN_CANDIDATES:
        return True, "insufficient_packets"
    if candidate_count < WORKSPACE_ADAPTIVE_MIN_CANDIDATES:
        return True, "insufficient_candidates"
    return False, None


def evidence_summary_text(packet: dict[str, Any]) -> str:
    primary_intent = str(packet.get("primary_intent") or "").strip()
    if primary_intent:
        return compact_snippet(primary_intent, packet.get("workspace"), limit=96)
    snippets = packet.get("representative_snippets") or []
    for snippet in snippets:
        text = str(snippet or "").strip()
        if text:
            return compact_snippet(text, packet.get("workspace"), limit=96)
    return candidate_label(packet)


def build_evidence_items(group_packets: list[dict[str, Any]], limit: int = 3) -> list[dict[str, str]]:
    ranked_packets = sorted(
        group_packets,
        key=lambda packet: (
            1 if str(packet.get("primary_intent") or "").strip() else 0,
            int(packet.get("support", {}).get("message_count", 0)),
            compare_iso_timestamps(packet.get("timestamp")),
            str(packet.get("packet_id") or ""),
        ),
        reverse=True,
    )

    selected: list[dict[str, str]] = []
    selected_refs: set[str] = set()
    selected_summaries: set[str] = set()
    used_sources: set[str] = set()

    def try_add(packet: dict[str, Any], *, prefer_new_source: bool) -> bool:
        session_ref = str(packet.get("session_ref") or "").strip()
        timestamp = str(packet.get("timestamp") or "").strip()
        source = str(packet.get("source") or "").strip()
        summary = evidence_summary_text(packet)
        summary_key = summary.lower()
        if not session_ref or not timestamp or not source or not summary:
            return False
        if session_ref in selected_refs or summary_key in selected_summaries:
            return False
        if prefer_new_source and used_sources and source in used_sources:
            return False
        selected.append(
            {
                "session_ref": session_ref,
                "timestamp": timestamp,
                "source": source,
                "summary": summary,
            }
        )
        selected_refs.add(session_ref)
        selected_summaries.add(summary_key)
        used_sources.add(source)
        return True

    if not ranked_packets:
        return []

    # 1) First pick the most representative packet.
    representative = ranked_packets[0]
    try_add(representative, prefer_new_source=False)

    # 2) Then pick a supporting packet, preferring a different source/session.
    for packet in ranked_packets[1:]:
        if len(selected) >= limit:
            break
        if try_add(packet, prefer_new_source=True):
            break
    for packet in ranked_packets[1:]:
        if len(selected) >= limit:
            break
        if try_add(packet, prefer_new_source=False):
            break

    # 3) Finally pick the most heterogeneous supporting packet still inside the same candidate.
    anchor_summary = evidence_summary_text(representative)
    anchor_tokens = tokenize(anchor_summary)
    heterogeneous_packets = sorted(
        ranked_packets[1:],
        key=lambda packet: (
            jaccard_score(anchor_tokens, tokenize(evidence_summary_text(packet))),
            -int(packet.get("support", {}).get("message_count", 0)),
        ),
    )
    for packet in heterogeneous_packets:
        if len(selected) >= limit:
            break
        try_add(packet, prefer_new_source=False)

    return selected[:limit]


def classify_intent_specificity(packet: dict[str, Any]) -> str:
    intent = str(packet.get("primary_intent") or "").strip()
    tokens = tokenize(intent)
    task_shapes = [str(shape) for shape in packet.get("task_shape", []) if shape]
    has_non_generic_shape = any(shape not in GENERIC_TASK_SHAPES for shape in task_shapes)
    if has_non_generic_shape and len(tokens) >= 6:
        return "high"
    if has_non_generic_shape or len(tokens) >= 4:
        return "medium"
    return "low"


def is_generic_intent(packet: dict[str, Any]) -> bool:
    intent = str(packet.get("primary_intent") or "").strip()
    tokens = tokenize(intent)
    task_shapes = [str(shape) for shape in packet.get("task_shape", []) if shape]
    return not intent or len(tokens) < 4 or (bool(task_shapes) and all(shape in GENERIC_TASK_SHAPES for shape in task_shapes[:2]))


def estimate_synonym_split_rate(packets: list[dict[str, Any]]) -> float:
    unique_intents: list[str] = []
    token_sets: list[set[str]] = []
    for packet in packets:
        intent = str(packet.get("primary_intent") or "").strip()
        if not intent:
            continue
        lowered = intent.lower()
        if lowered in {value.lower() for value in unique_intents}:
            continue
        unique_intents.append(intent)
        token_sets.append(tokenize(intent))

    pair_count = 0
    near_pair_count = 0
    for index, left_tokens in enumerate(token_sets):
        for right_tokens in token_sets[index + 1 :]:
            pair_count += 1
            score = jaccard_score(left_tokens, right_tokens)
            if 0.25 <= score < 0.85:
                near_pair_count += 1
    if pair_count == 0:
        return 0.0
    return round(near_pair_count / pair_count, 3)


def build_intent_analysis(packets: list[dict[str, Any]], limit: int = 10) -> dict[str, Any]:
    specificity_distribution = Counter({"high": 0, "medium": 0, "low": 0})
    generic_count = 0
    items: list[dict[str, Any]] = []

    ordered_packets = sorted(
        packets,
        key=lambda packet: (
            compare_iso_timestamps(packet.get("timestamp")),
            str(packet.get("packet_id") or ""),
        ),
        reverse=True,
    )

    for index, packet in enumerate(ordered_packets[:limit], start=1):
        specificity = classify_intent_specificity(packet)
        specificity_distribution[specificity] += 1
        generic = is_generic_intent(packet)
        if generic:
            generic_count += 1
        items.append(
            {
                "sample_id": f"intent-{index:03d}",
                "timestamp": packet.get("timestamp"),
                "source": packet.get("source"),
                "primary_intent": evidence_summary_text(packet),
                "specificity": specificity,
                "is_generic": generic,
            }
        )

    for packet in ordered_packets[limit:]:
        specificity_distribution[classify_intent_specificity(packet)] += 1
        if is_generic_intent(packet):
            generic_count += 1

    total_packets = len(ordered_packets)
    generic_rate = round(generic_count / total_packets, 3) if total_packets else 0.0
    synonym_split_rate = estimate_synonym_split_rate(ordered_packets)

    return {
        "summary": {
            "total_packets": total_packets,
            "generic_rate": generic_rate,
            "synonym_split_rate": synonym_split_rate,
            "specificity_distribution": dict(specificity_distribution),
        },
        "items": items,
    }


def _pair_similarity(
    left_index: int,
    right_index: int,
    *,
    features_by_index: list[dict[str, Any]],
    similarity_cache: dict[tuple[int, int], float],
) -> float:
    pair = (min(left_index, right_index), max(left_index, right_index))
    cached = similarity_cache.get(pair)
    if cached is not None:
        return cached
    score = _similarity_score_from_features(features_by_index[pair[0]], features_by_index[pair[1]])
    similarity_cache[pair] = score
    return score


def _component_merge_allowed(
    left_index: int,
    right_index: int,
    *,
    union_find: UnionFind,
    features_by_index: list[dict[str, Any]],
    similarity_cache: dict[tuple[int, int], float],
) -> bool:
    left_root = union_find.find(left_index)
    right_root = union_find.find(right_index)
    if left_root == right_root:
        return False
    left_members = union_find.members[left_root]
    right_members = union_find.members[right_root]
    if not left_members or not right_members:
        return False
    if len(left_members) == 1 and len(right_members) == 1:
        return True
    for left_member in left_members:
        for right_member in right_members:
            if _pair_similarity(
                left_member,
                right_member,
                features_by_index=features_by_index,
                similarity_cache=similarity_cache,
            ) <= COMPLETE_LINK_AUDIT_THRESHOLD:
                return False
    return True


def _split_label(packet: dict[str, Any]) -> str | None:
    task_shapes = [str(shape) for shape in packet.get("task_shape", []) if shape]
    primary_shape = next((shape for shape in task_shapes if shape not in GENERIC_TASK_SHAPES), "")
    if not primary_shape and task_shapes:
        primary_shape = task_shapes[0]
    artifact_hints = [str(value) for value in packet.get("artifact_hints", []) if value]
    rule_hints = [
        str(item.get("normalized") or "")
        for item in packet_user_rule_hints(packet)
        if isinstance(item, dict) and item.get("normalized")
    ]
    if primary_shape and artifact_hints:
        return f"{primary_shape.replace('_', ' ')} / {artifact_hints[0]}"
    if primary_shape and rule_hints:
        return f"{primary_shape.replace('_', ' ')} / {rule_hints[0]}"
    if primary_shape:
        return primary_shape.replace("_", " ")
    if artifact_hints:
        return artifact_hints[0]
    if rule_hints:
        return rule_hints[0]
    return None


def build_split_suggestions(group_packets: list[dict[str, Any]], limit: int = 3) -> list[str]:
    counts: Counter[str] = Counter()
    for packet in group_packets:
        label = _split_label(packet)
        if label:
            counts[label] += 1
    return [label for label, count in counts.most_common(limit) if count >= 2]


# ---------------------------------------------------------------------------
# Oversized cluster subdivision
# ---------------------------------------------------------------------------

# Tighter merge threshold used when re-clustering an oversized group.
# Must be above CLUSTER_MERGE_THRESHOLD (0.55) to produce finer-grained clusters.
SUBDIVISION_THRESHOLD = 0.65


def _build_secondary_features(packet: dict[str, Any]) -> dict[str, Any]:
    """Extract features unused in primary similarity for oversized cluster re-splitting."""
    return {
        "file_set": frozenset(str(f) for f in packet.get("referenced_files", [])[:10] if f),
        "has_failure": bool(packet.get("workflow_signals", {}).get("failure_hints")),
        "has_retry": bool(packet.get("workflow_signals", {}).get("retry_hints")),
        "origin": str(packet.get("origin_hint") or "unknown"),
        "alignment_status": str(packet.get("intent_tool_alignment", {}).get("status") or "unknown"),
    }


def _secondary_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Multiplicative modifier (0.0–1.0) based on secondary features.

    Applied on top of primary similarity to surface differences invisible
    to the first-pass clustering.
    """
    score = 1.0
    left_files = left.get("file_set", frozenset())
    right_files = right.get("file_set", frozenset())
    if left_files and right_files:
        file_jac = jaccard_score(set(left_files), set(right_files))
        if file_jac < 0.1:
            score *= 0.7
    if left.get("has_failure") != right.get("has_failure"):
        score *= 0.85
    if left.get("origin") != right.get("origin"):
        score *= 0.9
    return score


def subdivide_oversized_cluster(
    group_indices: list[int],
    *,
    sorted_packets: list[dict[str, Any]],
    features_by_index: list[dict[str, Any]],
    similarity_cache: dict[tuple[int, int], float],
    tighter_threshold: float = SUBDIVISION_THRESHOLD,
    min_sub_cluster_size: int = 2,
) -> list[list[int]]:
    """Re-cluster an oversized group at a tighter threshold with secondary features.

    Returns a list of sub-groups (each a list of global indices).
    Singletons after re-clustering are returned as single-element lists so the
    caller can route them to unclustered.
    """
    if len(group_indices) < min_sub_cluster_size * 2:
        return [group_indices]

    secondary_features = {idx: _build_secondary_features(sorted_packets[idx]) for idx in group_indices}
    local_uf = UnionFind(len(group_indices))
    idx_map = {global_idx: local_idx for local_idx, global_idx in enumerate(group_indices)}

    for i, gi in enumerate(group_indices):
        for j in range(i + 1, len(group_indices)):
            gj = group_indices[j]
            primary_sim = _pair_similarity(
                gi, gj,
                features_by_index=features_by_index,
                similarity_cache=similarity_cache,
            )
            adjusted = primary_sim * _secondary_similarity(secondary_features[gi], secondary_features[gj])
            if adjusted < tighter_threshold:
                continue
            li, lj = idx_map[gi], idx_map[gj]
            lr, rr = local_uf.find(li), local_uf.find(lj)
            if lr == rr:
                continue
            # Complete-link guard within the local sub-clustering
            allowed = True
            for lm in local_uf.members[lr]:
                for rm in local_uf.members[rr]:
                    gm_l, gm_r = group_indices[lm], group_indices[rm]
                    pair_sim = _pair_similarity(
                        gm_l, gm_r,
                        features_by_index=features_by_index,
                        similarity_cache=similarity_cache,
                    )
                    adj_sim = pair_sim * _secondary_similarity(secondary_features[gm_l], secondary_features[gm_r])
                    if adj_sim < COMPLETE_LINK_AUDIT_THRESHOLD:
                        allowed = False
                        break
                if not allowed:
                    break
            if allowed:
                local_uf.union(li, lj)

    sub_groups_map: dict[int, list[int]] = defaultdict(list)
    for local_idx, global_idx in enumerate(group_indices):
        sub_groups_map[local_uf.find(local_idx)].append(global_idx)

    return list(sub_groups_map.values())


def cluster_packets(packets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    if not packets:
        return [], [], {"block_count": 0, "block_comparisons": 0}

    sorted_packets = sorted(packets, key=packet_sort_key, reverse=True)
    packet_lookup = {str(packet.get("packet_id")): packet for packet in sorted_packets}
    features_by_index = [_build_similarity_features(packet) for packet in sorted_packets]
    blocks: dict[str, list[int]] = defaultdict(list)
    for index, packet in enumerate(sorted_packets):
        for key in stable_block_keys(packet):
            blocks[key].append(index)

    union_find = UnionFind(len(sorted_packets))
    near_matches_by_index: dict[int, list[dict[str, Any]]] = defaultdict(list)
    block_comparisons = 0
    seen_pairs: set[tuple[int, int]] = set()
    similarity_cache: dict[tuple[int, int], float] = {}

    def register_near_match(left_index: int, right_index: int, score: float, *, reason: str | None = None) -> None:
        left_payload = {
            "packet_id": sorted_packets[right_index]["packet_id"],
            "score": score,
            "primary_intent": sorted_packets[right_index]["primary_intent"],
            "session_ref": sorted_packets[right_index].get("session_ref"),
        }
        right_payload = {
            "packet_id": sorted_packets[left_index]["packet_id"],
            "score": score,
            "primary_intent": sorted_packets[left_index]["primary_intent"],
            "session_ref": sorted_packets[left_index].get("session_ref"),
        }
        if reason:
            left_payload["reason"] = reason
            right_payload["reason"] = reason
        near_matches_by_index[left_index].append(left_payload)
        near_matches_by_index[right_index].append(right_payload)

    for block_indexes in blocks.values():
        for offset, left_index in enumerate(block_indexes):
            for right_index in block_indexes[offset + 1 :]:
                pair = (min(left_index, right_index), max(left_index, right_index))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                block_comparisons += 1
                score = _pair_similarity(
                    left_index,
                    right_index,
                    features_by_index=features_by_index,
                    similarity_cache=similarity_cache,
                )
                if score >= CLUSTER_MERGE_THRESHOLD:
                    if _component_merge_allowed(
                        left_index,
                        right_index,
                        union_find=union_find,
                        features_by_index=features_by_index,
                        similarity_cache=similarity_cache,
                    ):
                        union_find.union(left_index, right_index)
                    else:
                        register_near_match(left_index, right_index, score, reason="complete_link_guard")
                elif CLUSTER_NEAR_MATCH_THRESHOLD <= score < CLUSTER_MERGE_THRESHOLD:
                    # Plain near-matches stay as research input only. Downstream blocking is
                    # reserved for complete-link guard failures, which indicate a bridge-like
                    # merge attempt that looked acceptable pairwise but was rejected at the
                    # component level.
                    register_near_match(left_index, right_index, score)

    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(sorted_packets)):
        groups[union_find.find(index)].append(index)

    # --- Phase: subdivide oversized clusters ---
    total_packets_all = len(sorted_packets)
    final_groups: list[tuple[list[int], dict[str, Any] | None]] = []  # (indices, subdivision_origin)
    for _root, indexes in groups.items():
        is_oversized = (
            len(indexes) >= OVERSIZED_CLUSTER_MIN_PACKETS
            and len(indexes) / total_packets_all >= OVERSIZED_CLUSTER_MIN_SHARE
        )
        if is_oversized:
            sub_groups = subdivide_oversized_cluster(
                indexes,
                sorted_packets=sorted_packets,
                features_by_index=features_by_index,
                similarity_cache=similarity_cache,
            )
            parent_id = sorted_packets[indexes[0]]["packet_id"].replace(":", "-")
            origin = {
                "parent_candidate_id": parent_id,
                "original_size": len(indexes),
                "subdivision_threshold": SUBDIVISION_THRESHOLD,
                "sub_cluster_count": len(sub_groups),
            }
            for sg in sub_groups:
                final_groups.append((sg, origin))
        else:
            final_groups.append((indexes, None))

    latest_timestamp = max(
        (str(packet.get("timestamp")) for packet in sorted_packets if packet.get("timestamp")),
        key=compare_iso_timestamps,
        default=None,
    )
    candidates: list[dict[str, Any]] = []
    unclustered: list[dict[str, Any]] = []

    for indexes, subdivision_origin in final_groups:
        group_packets = [sorted_packets[index] for index in indexes]
        if len(group_packets) == 1:
            unclustered.append(annotate_unclustered_packet(group_packets[0]))
            continue
        timestamps = [str(packet.get("timestamp") or "") for packet in group_packets if packet.get("timestamp")]
        workspace_strs = [
            str(packet.get("workspace") or "").strip() for packet in group_packets if str(packet.get("workspace") or "").strip()
        ]
        workspace_counter = Counter(workspace_strs)
        dominant_workspace = workspace_counter.most_common(1)[0][0] if workspace_counter else None
        workspace_paths = sorted(set(workspace_strs))[:12]
        support = {
            "total_packets": len(group_packets),
            "claude_packets": sum(1 for packet in group_packets if packet.get("source") == CLAUDE_SOURCE),
            "codex_packets": sum(1 for packet in group_packets if packet.get("source") == CODEX_SOURCE),
            "total_tool_calls": sum(int(packet.get("support", {}).get("tool_call_count", 0)) for packet in group_packets),
            "unique_workspaces": len({packet.get("workspace") for packet in group_packets if packet.get("workspace")}),
            "recent_packets_7d": recent_packet_count(timestamps, latest_timestamp),
            "contaminated_packets": sum(1 for packet in group_packets if packet.get("contamination_signals")),
        }
        task_shapes = _top_values([shape for packet in group_packets for shape in packet.get("task_shape", [])], 3)
        tool_signatures = _top_values([tool for packet in group_packets for tool in packet.get("tool_signature", [])], 5)
        artifact_hints = _top_values([hint for packet in group_packets for hint in packet.get("artifact_hints", [])], 3)
        rule_hints = _top_values(
            [item.get("normalized") for packet in group_packets for item in packet_user_rule_hints(packet) if item.get("normalized")],
            3,
        )
        representative_examples = _top_values([packet.get("primary_intent") for packet in group_packets if packet.get("primary_intent")], 2)
        if len(representative_examples) < 2:
            snippets = _top_values(
                [snippet for packet in group_packets for snippet in packet.get("representative_snippets", []) if snippet],
                2,
            )
            for snippet in snippets:
                if snippet not in representative_examples:
                    representative_examples.append(snippet)
                if len(representative_examples) >= 2:
                    break
        session_refs = [packet.get("session_ref") for packet in group_packets if packet.get("session_ref")]
        nearest_values: list[dict[str, Any]] = []
        for index in indexes:
            nearest_values.extend(near_matches_by_index.get(index, []))
        nearest = sorted(
            _dedupe_matches(nearest_values),
            key=lambda item: (float(item["score"]), str(item["packet_id"])),
            reverse=True,
        )[:3]
        research_targets = build_research_targets(
            group_packets,
            near_matches=nearest,
            packet_lookup=packet_lookup,
            limit=DEFAULT_RESEARCH_REF_LIMIT,
        )
        candidate = {
            "candidate_id": group_packets[0]["packet_id"].replace(":", "-"),
            "label": candidate_label(
                {
                    "common_task_shapes": task_shapes,
                    "artifact_hints": artifact_hints,
                    "rule_hints": rule_hints,
                    "primary_intent": group_packets[0].get("primary_intent"),
                }
            ),
            "score": 0.0,
            "dominant_workspace": dominant_workspace,
            "workspace_paths": workspace_paths,
            "support": support,
            "common_task_shapes": task_shapes,
            "common_tool_signatures": tool_signatures,
            "artifact_hints": artifact_hints,
            "rule_hints": rule_hints,
            "representative_examples": representative_examples,
            "session_refs": session_refs,
            "near_matches": nearest,
            "research_targets": research_targets,
            "evidence_items": build_evidence_items(group_packets),
            "split_suggestions": build_split_suggestions(group_packets),
            "intent_trace": _aggregate_candidate_list_field(group_packets, "intent_trace", 4),
            "constraints": _aggregate_candidate_list_field(group_packets, "constraints", 4),
            "acceptance_criteria": _aggregate_candidate_list_field(group_packets, "acceptance_criteria", 4),
            "origin_hint": _combine_origin_hints([str(packet.get("origin_hint") or "") for packet in group_packets]),
            "contamination_signals": _top_values(
                [signal for packet in group_packets for signal in packet.get("contamination_signals", []) if signal],
                4,
            ),
            "user_signal_strength": _combine_user_signal_strength(
                [str(packet.get("user_signal_strength") or "") for packet in group_packets]
            ),
        }
        if subdivision_origin:
            candidate["subdivision_origin"] = subdivision_origin
        candidate["score"] = candidate_score(support)
        candidate.update(build_candidate_quality(candidate, total_packets_all=total_packets_all))
        candidate["research_brief"] = build_research_brief(candidate)
        candidates.append(candidate)

    candidates.sort(key=candidate_sort_key, reverse=True)
    unclustered.sort(key=lambda packet: compare_iso_timestamps(packet.get("timestamp")), reverse=True)
    return candidates, unclustered, {"block_count": len(blocks), "block_comparisons": block_comparisons}


def _top_values(values: list[Any], limit: int) -> list[Any]:
    counts = defaultdict(int)
    ordered: list[Any] = []
    for value in values:
        if value is None:
            continue
        counts[value] += 1
        if value not in ordered:
            ordered.append(value)
    ordered.sort(key=lambda item: (counts[item], str(item)), reverse=True)
    return ordered[:limit]


def _aggregate_candidate_list_field(group_packets: list[dict[str, Any]], field: str, limit: int) -> list[str]:
    values: list[str] = []
    for packet in group_packets:
        raw = packet.get(field)
        if not isinstance(raw, list):
            continue
        for item in raw:
            text = str(item).strip()
            if text:
                values.append(text)
    return _top_values(values, limit)


def _dedupe_matches(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for value in values:
        packet_id = str(value.get("packet_id"))
        if packet_id in seen:
            continue
        seen.add(packet_id)
        deduped.append(value)
    return deduped


def build_candidate_comparison(selected_candidates: list[dict[str, Any]], legacy_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    selected_labels = [str(candidate.get("label") or "") for candidate in selected_candidates if candidate.get("label")]
    legacy_labels = [str(candidate.get("label") or "") for candidate in legacy_candidates if candidate.get("label")]
    selected_set = set(selected_labels)
    legacy_set = set(legacy_labels)
    shared_labels = sorted(selected_set & legacy_set)
    overlap = overlap_score(selected_set, legacy_set)
    jaccard = jaccard_score(selected_set, legacy_set)
    warnings: list[str] = []
    if selected_set and legacy_set and overlap < COMPARE_LEGACY_OVERLAP_WARNING_THRESHOLD:
        warnings.append(
            "store/raw candidate overlap is below threshold "
            f"({overlap:.2f} < {COMPARE_LEGACY_OVERLAP_WARNING_THRESHOLD:.2f})"
        )
    return {
        "selected_candidate_count": len(selected_candidates),
        "legacy_candidate_count": len(legacy_candidates),
        "shared_labels": shared_labels,
        "selected_only_labels": sorted(selected_set - legacy_set),
        "legacy_only_labels": sorted(legacy_set - selected_set),
        "label_overlap_ratio": round(overlap, 3),
        "label_jaccard_ratio": round(jaccard, 3),
        "warnings": warnings,
    }


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        resolved_workspace = resolve_workspace(args.workspace)
        workspace = None if args.all_sessions else resolved_workspace
        claude_root = Path(args.claude_root).expanduser().resolve()
        codex_history_file = Path(args.codex_history_file).expanduser().resolve()
        codex_sessions_root = Path(args.codex_sessions_root).expanduser().resolve()
        max_window_days = WORKSPACE_ADAPTIVE_EXPANDED_DAYS if not args.all_sessions else args.days
        resolved_store_path = resolve_store_path(args.store_path) if args.store_path else None

        selected_input_source = "raw"
        source_statuses: list[dict[str, Any]] = []
        store_slice_completeness: dict[str, Any] | None = None
        store_hydration: dict[str, Any] | None = None
        if args.input_source in {"store", "auto"}:
            if resolved_store_path is None:
                raise ValueError("--store-path is required when --input-source is store or auto")
            store_window_days = max(max_window_days, args.days)
            store_now = _store_reference_now(args.reference_date)
            store_since, store_until = _store_slice_bounds(reference_now=store_now, days=store_window_days)
            store_packets, store_statuses = read_store_packets(
                resolved_store_path,
                workspace=workspace,
                all_sessions=args.all_sessions,
                max_days=store_window_days,
                reference_now=store_now,
            )
            store_slice_completeness = _evaluate_store_slice_completeness(
                resolved_store_path,
                workspace=workspace,
                expected_sources_workspace=resolved_workspace,
                all_sessions=args.all_sessions,
                since=store_since,
                until=store_until,
                sources_file=args.sources_file,
            )
            store_packets_are_approximate = bool(store_packets) and _store_packets_fidelity(store_packets) == FIDELITY_APPROXIMATE
            store_slice_sufficient = _is_store_slice_sufficient(store_packets, store_slice_completeness)
            store_slice_complete = (
                store_slice_completeness is not None
                and store_slice_completeness.get("status") == SLICE_COMPLETE
            )
            skip_hydration_for_stale_store = (
                store_packets_are_approximate
                and store_slice_complete
            )
            before_hydration_status = store_slice_completeness.get("status") if store_slice_completeness else None
            store_hydration = {
                "attempted": False,
                "status": "stale_canonical" if skip_hydration_for_stale_store else ("not_needed" if store_slice_sufficient else "not_attempted"),
                "before_status": before_hydration_status,
            }
            if not store_slice_sufficient and not skip_hydration_for_stale_store:
                try:
                    _hydrate_store_slice(
                        resolved_store_path,
                        workspace=resolved_workspace,
                        all_sessions=args.all_sessions,
                        since=store_since,
                        until=store_until,
                        sources_file=args.sources_file,
                    )
                except Exception as hydrate_exc:
                    store_hydration = {
                        "attempted": True,
                        "status": "failed",
                        "before_status": before_hydration_status,
                        "message": str(hydrate_exc),
                    }
                    print(f"[warn] store hydration failed: {hydrate_exc}", file=sys.stderr)
                    if args.input_source == "store":
                        raise
                else:
                    store_packets, store_statuses = read_store_packets(
                        resolved_store_path,
                        workspace=workspace,
                        all_sessions=args.all_sessions,
                        max_days=store_window_days,
                        reference_now=store_now,
                    )
                    store_slice_completeness = _evaluate_store_slice_completeness(
                        resolved_store_path,
                        workspace=workspace,
                        expected_sources_workspace=resolved_workspace,
                        all_sessions=args.all_sessions,
                        since=store_since,
                        until=store_until,
                        sources_file=args.sources_file,
                    )
                    store_slice_sufficient = _is_store_slice_sufficient(store_packets, store_slice_completeness)
                    store_slice_complete = (
                        store_slice_completeness is not None
                        and store_slice_completeness.get("status") == SLICE_COMPLETE
                    )
                    store_hydration = {
                        "attempted": True,
                        "status": "hydrated",
                        "before_status": before_hydration_status,
                        "after_status": store_slice_completeness.get("status") if store_slice_completeness else None,
                        "sufficient": store_slice_sufficient,
                    }
            if args.input_source == "store":
                all_packets = store_packets
                source_statuses = store_statuses
                selected_input_source = "store"
            elif store_slice_sufficient:
                all_packets = store_packets
                source_statuses = store_statuses
                selected_input_source = "store"
            else:
                raw_packets, raw_source_statuses = collect_raw_packets(
                    workspace=workspace,
                    claude_root=claude_root,
                    codex_history_file=codex_history_file,
                    codex_sessions_root=codex_sessions_root,
                    gap_hours=args.gap_hours,
                )
                if not raw_packets and store_packets and store_slice_complete:
                    all_packets = store_packets
                    source_statuses = store_statuses
                    selected_input_source = "store"
                else:
                    all_packets = raw_packets
                    source_statuses = raw_source_statuses
                    selected_input_source = "raw"
        else:
            all_packets, source_statuses = collect_raw_packets(
                workspace=workspace,
                claude_root=claude_root,
                codex_history_file=codex_history_file,
                codex_sessions_root=codex_sessions_root,
                gap_hours=args.gap_hours,
            )

        selected_input_fidelity = FIDELITY_ORIGINAL
        if selected_input_source == "store":
            selected_input_fidelity = _store_packets_fidelity(all_packets)

        reference_date: date | None = date.fromisoformat(args.reference_date) if args.reference_date else None
        initial_window = prepare_window_result(all_packets, args.days, reference_date)
        effective_window = initial_window
        adaptive_expanded = False
        adaptive_reason = None
        if not args.all_sessions:
            should_expand, adaptive_reason = adaptive_window_decision(initial_window, args.days)
            if should_expand:
                effective_window = prepare_window_result(all_packets, WORKSPACE_ADAPTIVE_EXPANDED_DAYS, reference_date)
                adaptive_expanded = True

        all_packets = effective_window["packets"]
        candidates = effective_window["candidates"]
        unclustered = effective_window["unclustered"]
        stats = effective_window["stats"]
        date_window_start = effective_window["date_window_start"]
        resolved_decision_log_path = resolve_decision_log_path(args.decision_log_path)
        decision_states_by_key, decision_states_by_content_key, decision_log_status = load_latest_decision_states(
            resolved_decision_log_path
        )
        candidates, decision_log_application = apply_decision_states_to_candidates(
            candidates, decision_states_by_key, decision_states_by_content_key
        )
        top_candidates = candidates[: max(0, args.top_n)]
        limited_unclustered = unclustered[: max(0, args.max_unclustered)]

        payload = {
            "status": "success",
            "source": PREPARE_SOURCE,
            "candidates": top_candidates,
            "unclustered": limited_unclustered,
            "sources": source_statuses,
            "summary": {
                "total_packets": len(all_packets),
                "total_candidates": len(candidates),
                "returned_candidates": len(top_candidates),
                "returned_unclustered": len(limited_unclustered),
                "block_count": stats["block_count"],
                "block_comparisons": stats["block_comparisons"],
                "no_sources_available": len(all_packets) == 0,
                "decision_log_matched_candidates": decision_log_application["matched_candidates"],
                "decision_log_content_key_migrations": decision_log_application["content_key_migrations"],
                "decision_log_suppressed_candidates": decision_log_application["suppressed_candidates"],
            },
            "config": {
                "days": args.days,
                "effective_days": effective_window["days"],
                "gap_hours": args.gap_hours,
                "top_n": args.top_n,
                "max_unclustered": args.max_unclustered,
                "workspace": str(workspace) if workspace else None,
                "invocation_workspace": str(resolved_workspace),
                "all_sessions": args.all_sessions,
                "observation_mode": "all-sessions" if args.all_sessions else "workspace",
                "input_source": selected_input_source,
                "input_fidelity": selected_input_fidelity,
                "date_window_start": date_window_start,
                "adaptive_window": {
                    "enabled": not args.all_sessions,
                    "expanded": adaptive_expanded,
                    "fallback_days": WORKSPACE_ADAPTIVE_EXPANDED_DAYS,
                    "packet_threshold": WORKSPACE_ADAPTIVE_MIN_PACKETS,
                    "candidate_threshold": WORKSPACE_ADAPTIVE_MIN_CANDIDATES,
                    "reason": adaptive_reason if adaptive_expanded else None,
                    "initial_days": initial_window["days"],
                    "initial_packet_count": len(initial_window["packets"]),
                    "initial_candidate_count": len(initial_window["candidates"]),
                },
                "decision_log": {
                    **decision_log_status,
                    **decision_log_application,
                },
                **({"store_hydration": store_hydration} if store_hydration else {}),
                **({"input_completeness": store_slice_completeness} if selected_input_source == "store" and store_slice_completeness else {}),
            },
        }
        payload["observation_contract"] = build_observation_contract(payload)
        if args.compare_legacy and selected_input_source == "store":
            legacy_packets, _legacy_statuses = collect_raw_packets(
                workspace=workspace,
                claude_root=claude_root,
                codex_history_file=codex_history_file,
                codex_sessions_root=codex_sessions_root,
                gap_hours=args.gap_hours,
            )
            legacy_window = prepare_window_result(legacy_packets, effective_window["days"], reference_date)
            payload["comparison"] = {
                "legacy_input_source": "raw",
                **build_candidate_comparison(top_candidates, legacy_window["candidates"][: max(0, args.top_n)]),
            }
        if args.dump_intents:
            payload["intent_analysis"] = build_intent_analysis(all_packets)
        if resolved_store_path is not None:
            no_sources = payload.get("summary", {}).get("no_sources_available", False)
            should_persist, persist_skip_reason = _should_persist_patterns(
                selected_input_source=selected_input_source,
                input_fidelity=selected_input_fidelity,
                source_statuses=source_statuses,
                top_candidates=top_candidates,
                no_sources_available=no_sources,
                store_slice_completeness=store_slice_completeness,
            )
            payload["config"]["pattern_persist"] = {
                "attempted": False,
                "status": "skipped" if not should_persist else "pending",
                **({"reason": persist_skip_reason} if persist_skip_reason else {}),
            }
            if should_persist:
                try:
                    persist_patterns_from_prepare(payload, store_path=resolved_store_path)
                except Exception as persist_exc:
                    payload["config"]["pattern_persist"] = {
                        "attempted": True,
                        "status": "failed",
                        "message": str(persist_exc),
                    }
                    print(f"[warn] pattern persistence failed: {persist_exc}", file=sys.stderr)
                else:
                    payload["config"]["pattern_persist"] = {
                        "attempted": True,
                        "status": "persisted",
                    }
        emit(payload)
    except Exception as exc:
        emit(error_response(PREPARE_SOURCE, str(exc)))


if __name__ == "__main__":
    main()
