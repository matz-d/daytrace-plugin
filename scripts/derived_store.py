from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from aggregate_core import DEFAULT_GROUP_WINDOW_MINUTES, DEFAULT_MAX_SPAN_MINUTES, build_groups
from common import ensure_datetime, isoformat_or_now, parse_datetime
from store import bootstrap_store, canonical_json, connect_store, resolve_store_path, stable_hash


ACTIVITY_DERIVATION_VERSION = "activities-v3"
PATTERN_DERIVATION_VERSION = "skill-miner-candidate-v1"


def _decode_warning(field: str, reason: str) -> str:
    return f"{field}:{reason}"


def _safe_json_loads(
    row: sqlite3.Row,
    field: str,
    *,
    default: Any,
    warnings: list[str],
    expected_type: Any = None,
) -> Any:
    raw_value = row[field]
    if raw_value is None:
        return default
    try:
        decoded = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        warnings.append(_decode_warning(field, "invalid_json"))
        return default
    if expected_type is not None and not isinstance(decoded, expected_type):
        expected_name = (
            ",".join(item.__name__ for item in expected_type)
            if isinstance(expected_type, tuple)
            else expected_type.__name__
        )
        warnings.append(
            _decode_warning(
                field,
                f"unexpected_type:{type(decoded).__name__}:expected:{expected_name}",
            )
        )
        return default
    return decoded


def _attach_decode_warnings(payload: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    if warnings:
        payload["decode_warnings"] = warnings
    return payload


def _coerce_int(
    value: Any,
    *,
    default: int = 0,
    warnings: list[str] | None = None,
    warning_field: str | None = None,
) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        if warnings is not None and warning_field:
            warnings.append(_decode_warning(warning_field, "invalid_int"))
        return default


def _load_confidence_categories(row: sqlite3.Row, field: str, warnings: list[str]) -> list[str]:
    raw_categories = _safe_json_loads(row, field, default=[], warnings=warnings)
    if isinstance(raw_categories, list):
        return [str(item) for item in raw_categories]
    if raw_categories in (None, ""):
        return []
    return [str(raw_categories)]


def _normalize_workspace_filter(workspace: str | Path | None) -> str | None:
    if workspace is None:
        return None
    return str(Path(workspace).expanduser().resolve())


def _normalize_time_filter(value: str | None, *, bound: str) -> str | None:
    if value is None:
        return None
    parsed = parse_datetime(value, bound=bound)
    return parsed.isoformat() if parsed is not None else None


def _row_to_observation(row: sqlite3.Row) -> dict[str, Any]:
    warnings: list[str] = []
    payload = {
        "observation_id": int(row["id"]),
        "source_run_id": int(row["source_run_id"]),
        "run_fingerprint": str(row["run_fingerprint"]),
        "event_fingerprint": str(row["event_fingerprint"]),
        "observation_kind": str(row["observation_kind"]),
        "source_name": str(row["source_name"]),
        "scope_mode": str(row["scope_mode"]),
        "workspace": str(row["workspace"]),
        "requested_date": row["requested_date"],
        "since_value": row["since_value"],
        "until_value": row["until_value"],
        "all_sessions": bool(row["all_sessions"]),
        "occurred_at": str(row["occurred_at"]),
        "event_type": str(row["event_type"]),
        "summary": str(row["summary"]),
        "confidence": str(row["confidence"]),
        "details": _safe_json_loads(row, "details_json", default={}, warnings=warnings, expected_type=dict),
        "event": _safe_json_loads(row, "event_json", default={}, warnings=warnings, expected_type=dict),
        "confidence_categories": _load_confidence_categories(row, "confidence_categories_json", warnings),
        "collected_at": str(row["collected_at"]),
    }
    return _attach_decode_warnings(payload, warnings)


def _row_to_source_run(row: sqlite3.Row) -> dict[str, Any]:
    warnings: list[str] = []
    payload = {
        "source_run_id": int(row["id"]),
        "run_fingerprint": str(row["run_fingerprint"]),
        "source_name": str(row["source_name"]),
        "source_id": str(row["source_id"]),
        "identity_version": str(row["identity_version"]),
        "manifest_fingerprint": str(row["manifest_fingerprint"]),
        "confidence_categories": _load_confidence_categories(row, "confidence_categories_json", warnings),
        "command_fingerprint": str(row["command_fingerprint"]),
        "status": str(row["status"]),
        "scope_mode": str(row["scope_mode"]),
        "workspace": str(row["workspace"]),
        "requested_date": row["requested_date"],
        "since_value": row["since_value"],
        "until_value": row["until_value"],
        "all_sessions": bool(row["all_sessions"]),
        "filters": _safe_json_loads(row, "filters_json", default={}, warnings=warnings, expected_type=dict),
        "command": _safe_json_loads(row, "command_json", default=[], warnings=warnings, expected_type=list),
        "reason": row["reason"],
        "message": row["message"],
        "duration_sec": float(row["duration_sec"]),
        "events_count": int(row["events_count"]),
        "collected_at": str(row["collected_at"]),
    }
    return _attach_decode_warnings(payload, warnings)


def _row_to_activity(row: sqlite3.Row) -> dict[str, Any]:
    warnings: list[str] = []
    activity_payload = _safe_json_loads(row, "activity_json", default={}, warnings=warnings, expected_type=dict)
    confidence_breakdown = activity_payload.get("confidence_breakdown", {}) if isinstance(activity_payload, dict) else {}
    if not isinstance(confidence_breakdown, dict):
        warnings.append(_decode_warning("activity_json.confidence_breakdown", "unexpected_type"))
        confidence_breakdown = {}
    normalized_confidence_breakdown: dict[str, int] = {}
    invalid_confidence_breakdown_value = False
    for key, value in confidence_breakdown.items():
        if not isinstance(key, str):
            continue
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            invalid_confidence_breakdown_value = True
            continue
        normalized_confidence_breakdown[str(key)] = coerced
    if invalid_confidence_breakdown_value:
        warnings.append(_decode_warning("activity_json.confidence_breakdown", "invalid_int_value"))
    event_confidence_breakdown = activity_payload.get("event_confidence_breakdown", {}) if isinstance(activity_payload, dict) else {}
    if not isinstance(event_confidence_breakdown, dict):
        warnings.append(_decode_warning("activity_json.event_confidence_breakdown", "unexpected_type"))
        event_confidence_breakdown = {}
    normalized_event_confidence_breakdown: dict[str, int] = {}
    invalid_event_confidence_value = False
    for key, value in event_confidence_breakdown.items():
        if not isinstance(key, str):
            continue
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            invalid_event_confidence_value = True
            continue
        normalized_event_confidence_breakdown[str(key)] = coerced
    if invalid_event_confidence_value:
        warnings.append(_decode_warning("activity_json.event_confidence_breakdown", "invalid_int_value"))
    scope_breakdown = activity_payload.get("scope_breakdown", []) if isinstance(activity_payload, dict) else []
    if not isinstance(scope_breakdown, list):
        warnings.append(_decode_warning("activity_json.scope_breakdown", "unexpected_type"))
        scope_breakdown = []
    confidence_basis = activity_payload.get("confidence_basis", {}) if isinstance(activity_payload, dict) else {}
    if not isinstance(confidence_basis, dict):
        warnings.append(_decode_warning("activity_json.confidence_basis", "unexpected_type"))
        confidence_basis = {}
    payload = {
        "activity_id": str(row["activity_id"]),
        "derivation_version": str(row["derivation_version"]),
        "input_fingerprint": str(row["input_fingerprint"]),
        "workspace": row["workspace"],
        "since_value": row["since_value"],
        "until_value": row["until_value"],
        "group_window_minutes": int(row["group_window_minutes"]),
        "start_timestamp": str(row["start_timestamp"]),
        "end_timestamp": str(row["end_timestamp"]),
        "summary": str(row["summary"]),
        "confidence": str(row["confidence"]),
        "confidence_breakdown": normalized_confidence_breakdown,
        "event_confidence_breakdown": normalized_event_confidence_breakdown,
        "confidence_basis": {str(key): str(value) for key, value in confidence_basis.items()},
        "sources": _safe_json_loads(row, "sources_json", default=[], warnings=warnings, expected_type=list),
        "confidence_categories": _load_confidence_categories(row, "confidence_categories_json", warnings),
        "scope_breakdown": [str(item) for item in scope_breakdown],
        "mixed_scope": bool(activity_payload.get("mixed_scope", False)) if isinstance(activity_payload, dict) else False,
        "source_count": int(row["source_count"]),
        "event_count": int(row["event_count"]),
        "evidence": _safe_json_loads(row, "evidence_json", default=[], warnings=warnings, expected_type=list),
        "evidence_overflow_count": (
            _coerce_int(
                activity_payload.get("evidence_overflow_count", 0),
                default=0,
                warnings=warnings,
                warning_field="activity_json.evidence_overflow_count",
            )
            if isinstance(activity_payload, dict)
            else 0
        ),
        "observation_fingerprints": _safe_json_loads(
            row,
            "observation_fingerprints_json",
            default=[],
            warnings=warnings,
            expected_type=list,
        ),
        "activity": activity_payload,
        "derived_at": str(row["derived_at"]),
    }
    return _attach_decode_warnings(payload, warnings)


def _row_to_pattern(row: sqlite3.Row) -> dict[str, Any]:
    warnings: list[str] = []
    payload = {
        "pattern_kind": str(row["pattern_kind"]),
        "pattern_key": str(row["pattern_key"]),
        "derivation_version": str(row["derivation_version"]),
        "input_fingerprint": str(row["input_fingerprint"]),
        "workspace": row["workspace"],
        "observation_mode": row["observation_mode"],
        "days": row["days"],
        "label": str(row["label"]),
        "score": float(row["score"]),
        "support": _safe_json_loads(row, "support_json", default={}, warnings=warnings, expected_type=dict),
        "pattern": _safe_json_loads(row, "pattern_json", default={}, warnings=warnings, expected_type=dict),
        "derived_at": str(row["derived_at"]),
    }
    return _attach_decode_warnings(payload, warnings)


def get_source_runs(
    store_path: str | Path | None = None,
    *,
    workspace: str | Path | None = None,
    requested_date: str | None = None,
    since: str | None = None,
    until: str | None = None,
    all_sessions: bool | None = None,
    source_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    normalized_store_path = resolve_store_path(store_path)
    bootstrap_store(normalized_store_path)
    normalized_workspace = _normalize_workspace_filter(workspace)

    clauses = []
    parameters: list[Any] = []
    if normalized_workspace is not None:
        clauses.append("workspace = ?")
        parameters.append(normalized_workspace)
    if requested_date is not None:
        clauses.append("requested_date = ?")
        parameters.append(requested_date)
    if since is not None:
        clauses.append("since_value = ?")
        parameters.append(since)
    if until is not None:
        clauses.append("until_value = ?")
        parameters.append(until)
    if all_sessions is not None:
        clauses.append("all_sessions = ?")
        parameters.append(1 if all_sessions else 0)
    if source_names:
        placeholders = ", ".join("?" for _ in source_names)
        clauses.append(f"source_name IN ({placeholders})")
        parameters.extend(source_names)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT *
        FROM source_runs
        {where_sql}
        ORDER BY source_name ASC, collected_at DESC, id DESC
    """
    with connect_store(normalized_store_path) as connection:
        rows = connection.execute(sql, parameters).fetchall()
    return [_row_to_source_run(row) for row in rows]


def _query_time_bounds(
    *,
    requested_date: str | None,
    since: str | None,
    until: str | None,
) -> tuple[str | None, str | None]:
    effective_since = since if since is not None else requested_date
    effective_until = until if until is not None else requested_date
    return (
        _normalize_time_filter(effective_since, bound="start"),
        _normalize_time_filter(effective_until, bound="end"),
    )


def _source_run_covers_query(
    source_run: dict[str, Any],
    *,
    requested_date: str | None,
    normalized_since: str | None,
    normalized_until: str | None,
) -> bool:
    if requested_date is None and normalized_since is None and normalized_until is None:
        return True

    run_since = _normalize_time_filter(source_run.get("since_value"), bound="start")
    run_until = _normalize_time_filter(source_run.get("until_value"), bound="end")
    if normalized_since is not None:
        if run_since is None:
            return False
        run_since_dt = parse_datetime(run_since, bound="start")
        query_since_dt = parse_datetime(normalized_since, bound="start")
        if run_since_dt is None or query_since_dt is None or run_since_dt > query_since_dt:
            return False
    if normalized_until is not None:
        if run_until is None:
            return False
        run_until_dt = parse_datetime(run_until, bound="end")
        query_until_dt = parse_datetime(normalized_until, bound="end")
        if run_until_dt is None or query_until_dt is None or run_until_dt < query_until_dt:
            return False
    return True


def _source_run_priority(source_run: dict[str, Any], *, requested_date: str | None) -> tuple[float, float, float, int]:
    run_since = parse_datetime(source_run.get("since_value"), bound="start")
    run_until = parse_datetime(source_run.get("until_value"), bound="end")
    if run_since is None or run_until is None:
        window_span = float("inf")
    else:
        window_span = max((run_until - run_since).total_seconds(), 0.0)
    collected_at = parse_datetime(source_run.get("collected_at"), bound="end")
    collected_at_key = -(collected_at.timestamp()) if collected_at is not None else float("inf")
    requested_date_match = 0.0 if requested_date is not None and source_run.get("requested_date") == requested_date else 1.0
    return (
        requested_date_match,
        window_span,
        collected_at_key,
        -int(source_run["source_run_id"]),
    )


def get_slice_source_runs(
    store_path: str | Path | None = None,
    *,
    workspace: str | Path | None = None,
    requested_date: str | None = None,
    since: str | None = None,
    until: str | None = None,
    all_sessions: bool | None = None,
    source_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    normalized_store_path = resolve_store_path(store_path)
    bootstrap_store(normalized_store_path)
    candidate_runs = get_source_runs(
        normalized_store_path,
        workspace=workspace,
        all_sessions=all_sessions,
        source_names=source_names,
    )
    normalized_since, normalized_until = _query_time_bounds(
        requested_date=requested_date,
        since=since,
        until=until,
    )

    if requested_date is None and normalized_since is None and normalized_until is None:
        latest_by_source: dict[str, dict[str, Any]] = {}
        for source_run in candidate_runs:
            source_name = str(source_run["source_name"])
            if source_name not in latest_by_source:
                latest_by_source[source_name] = source_run
        return [latest_by_source[name] for name in sorted(latest_by_source)]

    selected_by_source: dict[str, dict[str, Any]] = {}
    for source_run in candidate_runs:
        if not _source_run_covers_query(
            source_run,
            requested_date=requested_date,
            normalized_since=normalized_since,
            normalized_until=normalized_until,
        ):
            continue
        source_name = str(source_run["source_name"])
        current = selected_by_source.get(source_name)
        if current is None or _source_run_priority(source_run, requested_date=requested_date) < _source_run_priority(
            current,
            requested_date=requested_date,
        ):
            selected_by_source[source_name] = source_run
    return [selected_by_source[name] for name in sorted(selected_by_source)]


SLICE_COMPLETE = "complete"
SLICE_PARTIAL = "partial"
SLICE_DEGRADED = "degraded"
SLICE_STALE = "stale"
SLICE_EMPTY = "empty"


def evaluate_slice_completeness(
    store_path: str | Path | None = None,
    *,
    workspace: str | Path | None = None,
    requested_date: str | None = None,
    since: str | None = None,
    until: str | None = None,
    all_sessions: bool = False,
    expected_source_names: set[str],
    expected_fingerprints: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Evaluate how complete a stored slice is relative to expected sources.

    Returns a dict with:
      status: complete / partial / degraded / stale / empty
      present_sources: set of source names in the slice
      missing_sources: set of expected sources not in the slice
      error_sources: set of sources with error status
      skipped_sources: set of sources with skipped status
      stale_sources: set of sources with mismatched manifest fingerprint
    """
    source_runs = get_slice_source_runs(
        store_path,
        workspace=workspace,
        requested_date=requested_date,
        since=since,
        until=until,
        all_sessions=all_sessions,
    )
    latest_by_source: dict[str, dict[str, Any]] = {}
    for run in source_runs:
        name = run["source_name"]
        if name not in latest_by_source:
            latest_by_source[name] = run

    present = set(latest_by_source.keys())
    relevant_present = present & expected_source_names
    missing = expected_source_names - present
    error_sources = {
        name for name, run in latest_by_source.items()
        if name in expected_source_names and run["status"] == "error"
    }
    skipped_sources = {
        name for name, run in latest_by_source.items()
        if name in expected_source_names and run["status"] == "skipped"
    }
    stale_sources: set[str] = set()
    if expected_fingerprints:
        for name, run in latest_by_source.items():
            if name in expected_fingerprints:
                if run["manifest_fingerprint"] != expected_fingerprints[name]:
                    stale_sources.add(name)

    if not relevant_present:
        status = SLICE_EMPTY
    elif stale_sources:
        status = SLICE_STALE
    elif missing:
        status = SLICE_PARTIAL
    elif error_sources:
        status = SLICE_DEGRADED
    else:
        success_sources = {
            name for name, run in latest_by_source.items()
            if name in expected_source_names and run["status"] == "success"
        }
        status = SLICE_COMPLETE if success_sources == expected_source_names else SLICE_DEGRADED

    return {
        "status": status,
        "present_sources": sorted(relevant_present),
        "missing_sources": sorted(missing),
        "error_sources": sorted(error_sources),
        "skipped_sources": sorted(skipped_sources),
        "stale_sources": sorted(stale_sources),
        "source_run_count": len(source_runs),
    }


def get_observations(
    store_path: str | Path | None = None,
    *,
    workspace: str | Path | None = None,
    requested_date: str | None = None,
    since: str | None = None,
    until: str | None = None,
    all_sessions: bool | None = None,
    source_names: list[str] | None = None,
    source_run_ids: list[int] | None = None,
    observation_kinds: list[str] | None = None,
) -> list[dict[str, Any]]:
    normalized_store_path = resolve_store_path(store_path)
    bootstrap_store(normalized_store_path)
    normalized_workspace = _normalize_workspace_filter(workspace)
    selected_source_run_ids = list(source_run_ids) if source_run_ids is not None else None
    if selected_source_run_ids is not None and not selected_source_run_ids:
        return []
    selected_observation_kinds = ["event"] if observation_kinds is None else list(observation_kinds)
    if not selected_observation_kinds:
        return []

    clauses = []
    parameters: list[Any] = []
    placeholders = ", ".join("?" for _ in selected_observation_kinds)
    clauses.append(f"o.observation_kind IN ({placeholders})")
    parameters.extend(selected_observation_kinds)
    if normalized_workspace is not None:
        clauses.append("sr.workspace = ?")
        parameters.append(normalized_workspace)
    if requested_date is not None and selected_source_run_ids is None:
        clauses.append("sr.requested_date = ?")
        parameters.append(requested_date)
    normalized_since = _normalize_time_filter(since, bound="start")
    normalized_until = _normalize_time_filter(until, bound="end")
    if normalized_since is not None:
        clauses.append("o.occurred_at >= ?")
        parameters.append(normalized_since)
    if normalized_until is not None:
        clauses.append("o.occurred_at <= ?")
        parameters.append(normalized_until)
    if all_sessions is not None:
        clauses.append("sr.all_sessions = ?")
        parameters.append(1 if all_sessions else 0)
    if source_names:
        placeholders = ", ".join("?" for _ in source_names)
        clauses.append(f"o.source_name IN ({placeholders})")
        parameters.extend(source_names)
    if selected_source_run_ids is not None:
        placeholders = ", ".join("?" for _ in selected_source_run_ids)
        clauses.append(f"o.source_run_id IN ({placeholders})")
        parameters.extend(selected_source_run_ids)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # Use a window function to deduplicate observations that share the same
    # event_fingerprint across different source_runs.  For each duplicate group
    # we keep only the row from the most recent source_run (highest collected_at,
    # then highest observation id as tie-breaker).
    sql = f"""
        SELECT * FROM (
            SELECT
                o.*,
                sr.run_fingerprint,
                sr.workspace,
                sr.requested_date,
                sr.since_value,
                sr.until_value,
                sr.all_sessions,
                sr.confidence_categories_json,
                ROW_NUMBER() OVER (
                    PARTITION BY o.event_fingerprint
                    ORDER BY sr.collected_at DESC, o.id DESC
                ) AS _dedup_rank
            FROM observations o
            JOIN source_runs sr ON sr.id = o.source_run_id
            {where_sql}
        )
        WHERE _dedup_rank = 1
        ORDER BY occurred_at ASC, source_name ASC, event_fingerprint ASC
    """
    with connect_store(normalized_store_path) as connection:
        rows = connection.execute(sql, parameters).fetchall()
    return [_row_to_observation(row) for row in rows]


def compute_activity_query_fingerprint(
    *,
    workspace: str | Path | None,
    requested_date: str | None,
    since: str | None,
    until: str | None,
    all_sessions: bool | None,
    group_window_minutes: int,
    max_span_minutes: int,
) -> str:
    return stable_hash(
        {
            "derivation_version": ACTIVITY_DERIVATION_VERSION,
            "workspace": _normalize_workspace_filter(workspace),
            "requested_date": requested_date,
            "since": since,
            "until": until,
            "all_sessions": all_sessions,
            "group_window_minutes": group_window_minutes,
            "max_span_minutes": max_span_minutes,
        }
    )


def compute_activities_input_fingerprint(
    observations: list[dict[str, Any]],
    *,
    group_window_minutes: int,
    max_span_minutes: int,
    confidence_categories_by_source: dict[str, list[str]] | None = None,
) -> str:
    if confidence_categories_by_source is None:
        confidence_categories_by_source = _build_confidence_categories_by_source(observations)
    return stable_hash(
        {
            "derivation_version": ACTIVITY_DERIVATION_VERSION,
            "group_window_minutes": group_window_minutes,
            "max_span_minutes": max_span_minutes,
            "observation_fingerprints": [observation["event_fingerprint"] for observation in observations],
            "confidence_categories_by_source": confidence_categories_by_source,
        }
    )


def _build_confidence_categories_by_source(observations: list[dict[str, Any]]) -> dict[str, list[str]]:
    confidence_categories_by_source: dict[str, list[str]] = {}
    for observation in observations:
        source_name = str(observation["source_name"])
        categories = [str(item) for item in observation.get("confidence_categories", [])]
        confidence_categories_by_source.setdefault(source_name, [])
        for category in categories:
            if category not in confidence_categories_by_source[source_name]:
                confidence_categories_by_source[source_name].append(category)
    return confidence_categories_by_source


def _build_scope_mode_by_source(observations: list[dict[str, Any]]) -> dict[str, str]:
    scope_mode_by_source: dict[str, str] = {}
    for observation in observations:
        source_name = str(observation["source_name"])
        scope_mode = observation.get("scope_mode")
        if scope_mode and source_name not in scope_mode_by_source:
            scope_mode_by_source[source_name] = str(scope_mode)
    return scope_mode_by_source


def derive_activities_from_observations(
    observations: list[dict[str, Any]],
    *,
    group_window_minutes: int = DEFAULT_GROUP_WINDOW_MINUTES,
    max_span_minutes: int = DEFAULT_MAX_SPAN_MINUTES,
) -> tuple[list[dict[str, Any]], str]:
    confidence_categories_by_source = _build_confidence_categories_by_source(observations)
    scope_mode_by_source = _build_scope_mode_by_source(observations)
    timeline = []
    for observation in observations:
        event = dict(observation["event"])
        event["_observation_fingerprint"] = observation["event_fingerprint"]
        timeline.append(event)

    timeline.sort(key=lambda event: ensure_datetime(event["timestamp"]))
    input_fingerprint = compute_activities_input_fingerprint(
        observations,
        group_window_minutes=group_window_minutes,
        max_span_minutes=max_span_minutes,
        confidence_categories_by_source=confidence_categories_by_source,
    )
    groups = build_groups(
        timeline,
        group_window_minutes=group_window_minutes,
        confidence_categories_by_source=confidence_categories_by_source,
        max_span_minutes=max_span_minutes,
        scope_mode_by_source=scope_mode_by_source,
    )

    activities = []
    for group in groups:
        observation_fingerprints = []
        cleaned_events = []
        for event in group["events"]:
            cleaned_event = dict(event)
            observation_fingerprints.append(str(cleaned_event.pop("_observation_fingerprint")))
            cleaned_events.append(cleaned_event)
        activity_json = dict(group)
        activity_json["events"] = cleaned_events

        activities.append(
            {
                "activity_id": str(group["id"]),
                "derivation_version": ACTIVITY_DERIVATION_VERSION,
                "input_fingerprint": input_fingerprint,
                "start_timestamp": str(group["start_timestamp"]),
                "end_timestamp": str(group["end_timestamp"]),
                "summary": str(group["summary"]),
                "confidence": str(group["confidence"]),
                "confidence_breakdown": dict(group.get("confidence_breakdown", {})),
                "event_confidence_breakdown": dict(group.get("event_confidence_breakdown", {})),
                "confidence_basis": dict(group.get("confidence_basis", {})),
                "sources": list(group["sources"]),
                "confidence_categories": list(group["confidence_categories"]),
                "scope_breakdown": list(group.get("scope_breakdown", [])),
                "mixed_scope": bool(group.get("mixed_scope", False)),
                "source_count": int(group["source_count"]),
                "event_count": int(group["event_count"]),
                "evidence": list(group["evidence"]),
                "evidence_overflow_count": _coerce_int(group.get("evidence_overflow_count", 0), default=0),
                "observation_fingerprints": observation_fingerprints,
                "activity": activity_json,
            }
        )
    return activities, input_fingerprint


def _persist_activities(
    store_path: Path,
    activities: list[dict[str, Any]],
    *,
    query_fingerprint: str,
    input_fingerprint: str,
    workspace: str | Path | None,
    since: str | None,
    until: str | None,
    group_window_minutes: int,
    derived_at: str,
) -> None:
    normalized_workspace = _normalize_workspace_filter(workspace)
    with connect_store(store_path) as connection:
        connection.execute("DELETE FROM activities WHERE query_fingerprint = ?", (query_fingerprint,))
        for activity in activities:
            connection.execute(
                """
                INSERT INTO activities (
                    query_fingerprint,
                    derivation_version,
                    input_fingerprint,
                    workspace,
                    since_value,
                    until_value,
                    group_window_minutes,
                    activity_id,
                    start_timestamp,
                    end_timestamp,
                    summary,
                    confidence,
                    sources_json,
                    confidence_categories_json,
                    source_count,
                    event_count,
                    evidence_json,
                    observation_fingerprints_json,
                    activity_json,
                    derived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query_fingerprint,
                    activity["derivation_version"],
                    input_fingerprint,
                    normalized_workspace,
                    since,
                    until,
                    group_window_minutes,
                    activity["activity_id"],
                    activity["start_timestamp"],
                    activity["end_timestamp"],
                    activity["summary"],
                    activity["confidence"],
                    canonical_json(activity["sources"]),
                    canonical_json(activity["confidence_categories"]),
                    activity["source_count"],
                    activity["event_count"],
                    canonical_json(activity["evidence"]),
                    canonical_json(activity["observation_fingerprints"]),
                    canonical_json(activity["activity"]),
                    derived_at,
                ),
            )
        connection.commit()


def _read_activities(
    store_path: Path,
    *,
    query_fingerprint: str,
) -> list[dict[str, Any]]:
    with connect_store(store_path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM activities
            WHERE query_fingerprint = ?
            ORDER BY start_timestamp ASC, activity_id ASC
            """,
            (query_fingerprint,),
        ).fetchall()
    activities = []
    for row in rows:
        activities.append(_row_to_activity(row))
    return activities


def get_activities(
    store_path: str | Path | None = None,
    *,
    workspace: str | Path | None = None,
    requested_date: str | None = None,
    since: str | None = None,
    until: str | None = None,
    all_sessions: bool | None = None,
    group_window_minutes: int = DEFAULT_GROUP_WINDOW_MINUTES,
    max_span_minutes: int = DEFAULT_MAX_SPAN_MINUTES,
    refresh: bool = False,
    preloaded_observations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    normalized_store_path = resolve_store_path(store_path)
    bootstrap_store(normalized_store_path)
    query_fingerprint = compute_activity_query_fingerprint(
        workspace=workspace,
        requested_date=requested_date,
        since=since,
        until=until,
        all_sessions=all_sessions,
        group_window_minutes=group_window_minutes,
        max_span_minutes=max_span_minutes,
    )
    if preloaded_observations is not None:
        observations = preloaded_observations
    else:
        selected_source_run_ids = None
        if requested_date is not None or since is not None or until is not None:
            selected_source_run_ids = [
                int(source_run["source_run_id"])
                for source_run in get_slice_source_runs(
                    normalized_store_path,
                    workspace=workspace,
                    requested_date=requested_date,
                    since=since,
                    until=until,
                    all_sessions=all_sessions,
                )
            ]
        observations = get_observations(
            normalized_store_path,
            workspace=workspace,
            requested_date=requested_date,
            since=since,
            until=until,
            all_sessions=all_sessions,
            source_run_ids=selected_source_run_ids,
        )
    current_input_fingerprint = compute_activities_input_fingerprint(
        observations,
        group_window_minutes=group_window_minutes,
        max_span_minutes=max_span_minutes,
    )
    existing = _read_activities(normalized_store_path, query_fingerprint=query_fingerprint)
    existing_input_fingerprint = existing[0]["input_fingerprint"] if existing else None
    if refresh or existing_input_fingerprint != current_input_fingerprint:
        activities, input_fingerprint = derive_activities_from_observations(
            observations,
            group_window_minutes=group_window_minutes,
            max_span_minutes=max_span_minutes,
        )
        _persist_activities(
            normalized_store_path,
            activities,
            query_fingerprint=query_fingerprint,
            input_fingerprint=input_fingerprint,
            workspace=workspace,
            since=since,
            until=until,
            group_window_minutes=group_window_minutes,
            derived_at=datetime.now().astimezone().isoformat(),
        )
        return activities
    return existing


def compute_pattern_query_fingerprint(
    *,
    workspace: str | Path | None,
    observation_mode: str | None,
    days: int | None,
) -> str:
    return stable_hash(
        {
            "workspace": _normalize_workspace_filter(workspace),
            "observation_mode": observation_mode,
            "days": days,
        }
    )


def compute_patterns_input_fingerprint(prepare_payload: dict[str, Any]) -> str:
    config = prepare_payload.get("config", {})
    summary = prepare_payload.get("summary", {})
    candidates = prepare_payload.get("candidates", [])
    compact_candidates = [
        {
            "candidate_id": candidate.get("candidate_id"),
            "label": candidate.get("label"),
            "score": candidate.get("score"),
            "support": candidate.get("support"),
            "session_refs": candidate.get("session_refs"),
            "evidence_items": candidate.get("evidence_items"),
        }
        for candidate in candidates
    ]
    return stable_hash(
        {
            "derivation_version": PATTERN_DERIVATION_VERSION,
            "config": {
                "workspace": config.get("workspace"),
                "observation_mode": config.get("observation_mode"),
                "days": config.get("days"),
                "effective_days": config.get("effective_days"),
            },
            "summary": summary,
            "candidates": compact_candidates,
        }
    )


def persist_patterns_from_prepare(
    prepare_payload: dict[str, Any],
    *,
    store_path: str | Path | None = None,
    derived_at: datetime | None = None,
) -> None:
    normalized_store_path = resolve_store_path(store_path)
    bootstrap_store(normalized_store_path)
    config = prepare_payload.get("config", {})
    candidates = prepare_payload.get("candidates", [])
    workspace = config.get("workspace")
    observation_mode = config.get("observation_mode")
    days = config.get("effective_days", config.get("days"))
    query_fingerprint = compute_pattern_query_fingerprint(
        workspace=workspace,
        observation_mode=observation_mode,
        days=days,
    )
    input_fingerprint = compute_patterns_input_fingerprint(prepare_payload)
    derived_at_iso = isoformat_or_now(derived_at)

    with connect_store(normalized_store_path) as connection:
        existing_fp = connection.execute(
            "SELECT input_fingerprint FROM patterns WHERE query_fingerprint = ? LIMIT 1",
            (query_fingerprint,),
        ).fetchone()
        if existing_fp is not None and str(existing_fp[0]) == input_fingerprint:
            return
        connection.execute("DELETE FROM patterns WHERE query_fingerprint = ?", (query_fingerprint,))
        for candidate in candidates:
            connection.execute(
                """
                INSERT INTO patterns (
                    query_fingerprint,
                    pattern_kind,
                    pattern_key,
                    derivation_version,
                    input_fingerprint,
                    workspace,
                    observation_mode,
                    days,
                    label,
                    score,
                    support_json,
                    pattern_json,
                    derived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query_fingerprint,
                    "skill-miner-candidate",
                    str(candidate.get("candidate_id")),
                    PATTERN_DERIVATION_VERSION,
                    input_fingerprint,
                    _normalize_workspace_filter(workspace) if workspace else None,
                    observation_mode,
                    int(days) if days is not None else None,
                    str(candidate.get("label") or "unlabeled-pattern"),
                    float(candidate.get("score") or 0.0),
                    canonical_json(candidate.get("support", {})),
                    canonical_json(candidate),
                    derived_at_iso,
                ),
            )
        connection.commit()


def get_patterns(
    store_path: str | Path | None = None,
    *,
    workspace: str | Path | None = None,
    observation_mode: str | None = None,
    days: int | None = None,
) -> list[dict[str, Any]]:
    normalized_store_path = resolve_store_path(store_path)
    bootstrap_store(normalized_store_path)
    query_fingerprint = compute_pattern_query_fingerprint(
        workspace=workspace,
        observation_mode=observation_mode,
        days=days,
    )
    with connect_store(normalized_store_path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM patterns
            WHERE query_fingerprint = ?
            ORDER BY score DESC, pattern_key ASC
            """,
            (query_fingerprint,),
        ).fetchall()
    return [_row_to_pattern(row) for row in rows]
