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


def _observation_filters(
    *,
    observation_kinds: list[str],
    workspace: str | None,
    requested_date: str | None,
    daily_runs_only: bool,
    since: str | None,
    until: str | None,
    all_sessions: bool | None,
    source_names: list[str] | None,
    source_run_ids: list[int] | None,
) -> tuple[list[str], list[Any]]:
    """WHERE clauses and their parameters for get_observations. `since`/`until` are storage timestamps."""
    clauses: list[str] = []
    parameters: list[Any] = []

    def add_in(column: str, values: list[Any]) -> None:
        clauses.append(f"{column} IN ({', '.join('?' for _ in values)})")
        parameters.extend(values)

    add_in("o.observation_kind", observation_kinds)
    if workspace is not None:
        clauses.append("sr.workspace = ?")
        parameters.append(workspace)
    if requested_date is not None:
        clauses.append("sr.requested_date = ?")
        parameters.append(requested_date)
    if daily_runs_only:
        # Runs made with --date. Ad-hoc --since/--until runs (e.g. the removed
        # 7-day pattern scan) summarize sessions differently and would duplicate them.
        clauses.append("sr.requested_date IS NOT NULL")
    if since is not None:
        clauses.append("o.occurred_at >= ?")
        parameters.append(since)
    if until is not None:
        clauses.append("o.occurred_at <= ?")
        parameters.append(until)
    if all_sessions is not None:
        clauses.append("sr.all_sessions = ?")
        parameters.append(1 if all_sessions else 0)
    if source_names:
        add_in("o.source_name", source_names)
    if source_run_ids is not None:
        add_in("o.source_run_id", source_run_ids)
    return clauses, parameters


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

    clauses, parameters = _observation_filters(
        observation_kinds=selected_observation_kinds,
        workspace=normalized_workspace,
        requested_date=requested_date if selected_source_run_ids is None else None,
        daily_runs_only=daily_runs_only,
        since=_normalize_time_filter(since, bound="start"),
        until=_normalize_time_filter(until, bound="end"),
        all_sessions=all_sessions,
        source_names=source_names,
        source_run_ids=selected_source_run_ids,
    )
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
