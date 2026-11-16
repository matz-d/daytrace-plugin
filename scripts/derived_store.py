from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from common import parse_datetime, to_storage_timestamp
from store import bootstrap_store, connect_store, resolve_store_path


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
    return to_storage_timestamp(parsed) if parsed is not None else None


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
    daily_runs_only: bool = False,
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
    if daily_runs_only:
        # Runs made with --date. Ad-hoc --since/--until runs (e.g. the removed
        # 7-day pattern scan) summarize sessions differently and would duplicate them.
        clauses.append("sr.requested_date IS NOT NULL")
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
