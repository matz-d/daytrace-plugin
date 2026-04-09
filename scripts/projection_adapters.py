from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from aggregate_core import (
    DEFAULT_GROUP_WINDOW_MINUTES,
    DEFAULT_MAX_SPAN_MINUTES,
    build_summary,
    load_expected_sources,
    resolve_date_filters,
    resolve_sources_file_path,
)
from common import current_platform, isoformat, resolve_workspace
from derived_store import (
    SLICE_COMPLETE,
    evaluate_slice_completeness,
    get_activities,
    get_observations,
    get_patterns,
    get_slice_source_runs,
)
from store import resolve_store_path


SCRIPT_DIR = Path(__file__).resolve().parent
AGGREGATE = SCRIPT_DIR / "aggregate.py"
DEFAULT_SOURCES_FILE = SCRIPT_DIR / "sources.json"
HYDRATE_TIMEOUT_SEC = 90
REPORT_DAY_BOUNDARY_HOUR = 6


def artifact_output_paths(
    *,
    normalized_date: str | None,
    resolved_since: str | None,
    resolved_until: str | None,
) -> tuple[str | None, str | None]:
    """Canonical local report_date (YYYY-MM-DD) and ~/.daytrace/output/<date>/ for Layer 3 artifacts."""
    if normalized_date:
        report_date = normalized_date
    elif resolved_since and resolved_until and resolved_since == resolved_until:
        report_date = resolved_since
    else:
        return None, None
    output_dir = str(Path.home() / ".daytrace" / "output" / report_date)
    return report_date, output_dir


def ensure_artifact_output_dir(output_dir: str | None) -> None:
    """Create output_dir (mkdir -p) when Layer 3 artifact paths are in use."""
    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)


def _resolve_expected_sources(
    *,
    sources_file: str | None,
    workspace: Path,
) -> tuple[set[str], dict[str, str]]:
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
        )
    except Exception as exc:
        raise RuntimeError(f"failed to load sources from {resolved_sources_file}: {exc}") from exc


def _source_run_summary(source_run: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "name": source_run["source_name"],
        "status": source_run["status"],
        "scope": source_run["scope_mode"],
        "events_count": source_run["events_count"],
    }
    for key in ("reason", "message", "duration_sec"):
        value = source_run.get(key)
        if value is not None:
            summary[key] = value
    command = source_run.get("command")
    if command:
        summary["command"] = command
    return summary


def _matching_source_runs(
    store_path: Path,
    *,
    workspace: Path,
    requested_date: str | None,
    since: str | None,
    until: str | None,
    all_sessions: bool,
) -> list[dict[str, Any]]:
    return get_slice_source_runs(
        store_path,
        workspace=workspace,
        requested_date=requested_date,
        since=since,
        until=until,
        all_sessions=all_sessions,
    )


def _hydrate_store(
    store_path: Path,
    *,
    workspace: Path,
    sources_file: str | None,
    requested_date: str | None,
    raw_since: str | None,
    raw_until: str | None,
    all_sessions: bool,
    max_span_minutes: int,
) -> None:
    command = [
        "python3",
        str(AGGREGATE),
        "--workspace",
        str(workspace),
        "--store-path",
        str(store_path),
    ]
    if sources_file is not None:
        command.extend(["--sources-file", sources_file])
    if requested_date is not None:
        command.extend(["--date", requested_date])
    else:
        if raw_since is not None:
            command.extend(["--since", raw_since])
        if raw_until is not None:
            command.extend(["--until", raw_until])
    if all_sessions:
        command.append("--all-sessions")
    command.extend(["--max-span", str(max_span_minutes)])

    try:
        completed = subprocess.run(
            command,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=HYDRATE_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"aggregate.py hydration failed: timed out after {HYDRATE_TIMEOUT_SEC} seconds") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        message = stderr or stdout or "no aggregate output"
        raise RuntimeError(f"aggregate.py hydration failed: {message}")


def _source_run_as_result(source_run: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": source_run["status"],
        "source": source_run["source_name"],
        "scope": source_run["scope_mode"],
        "events": [],
    }


def _payload_scope_mode(groups: list[dict[str, Any]], source_runs: list[dict[str, Any]]) -> str:
    if any(bool(group.get("mixed_scope")) for group in groups):
        return "mixed"

    group_scopes = {
        str(scope).strip()
        for group in groups
        if isinstance(group, dict)
        for scope in group.get("scope_breakdown", [])
        if str(scope).strip()
    }
    if len(group_scopes) > 1:
        return "mixed"

    observed_source_scopes = {
        str(source_run.get("scope_mode") or "").strip()
        for source_run in source_runs
        if isinstance(source_run, dict)
        and str(source_run.get("status") or "") == "success"
        and int(source_run.get("events_count", 0)) > 0
        and str(source_run.get("scope_mode") or "").strip()
    }
    return "mixed" if len(observed_source_scopes) > 1 else "single"


def build_projection_payload(
    *,
    workspace: str | Path = ".",
    date: str | None = None,
    since: str | None = None,
    until: str | None = None,
    all_sessions: bool = False,
    store_path: str | None = None,
    sources_file: str | None = None,
    group_window_minutes: int = DEFAULT_GROUP_WINDOW_MINUTES,
    max_span_minutes: int = DEFAULT_MAX_SPAN_MINUTES,
    hydrate_missing: bool = True,
    include_patterns: bool = False,
    pattern_days: int = 7,
) -> dict[str, Any]:
    resolved_workspace = resolve_workspace(workspace)
    resolved_store_path = resolve_store_path(store_path)
    resolved_since, resolved_until = resolve_date_filters(date, since, until)
    # Normalize requested_date to ISO the same way the write path does
    # (aggregate.py stores since_arg when --date is used, never "today"/"yesterday")
    normalized_date = resolved_since if date else None

    expected_names, expected_fingerprints = _resolve_expected_sources(
        sources_file=sources_file,
        workspace=resolved_workspace,
    )
    slice_completeness: dict[str, Any] | None = None

    source_runs = _matching_source_runs(
        resolved_store_path,
        workspace=resolved_workspace,
        requested_date=normalized_date,
        since=resolved_since,
        until=resolved_until,
        all_sessions=all_sessions,
    )
    needs_hydrate = False
    if not source_runs:
        needs_hydrate = True
    elif expected_names:
        slice_completeness = evaluate_slice_completeness(
            resolved_store_path,
            workspace=resolved_workspace,
            requested_date=normalized_date,
            since=resolved_since,
            until=resolved_until,
            all_sessions=all_sessions,
            expected_source_names=expected_names,
            expected_fingerprints=expected_fingerprints or None,
        )
        needs_hydrate = hydrate_missing and slice_completeness["status"] != SLICE_COMPLETE

    if needs_hydrate and hydrate_missing:
        _hydrate_store(
            resolved_store_path,
            workspace=resolved_workspace,
            sources_file=sources_file,
            requested_date=date,
            raw_since=since,
            raw_until=until,
            all_sessions=all_sessions,
            max_span_minutes=max_span_minutes,
        )
        source_runs = _matching_source_runs(
            resolved_store_path,
            workspace=resolved_workspace,
            requested_date=normalized_date,
            since=resolved_since,
            until=resolved_until,
            all_sessions=all_sessions,
        )
        if expected_names:
            slice_completeness = evaluate_slice_completeness(
                resolved_store_path,
                workspace=resolved_workspace,
                requested_date=normalized_date,
                since=resolved_since,
                until=resolved_until,
                all_sessions=all_sessions,
                expected_source_names=expected_names,
                expected_fingerprints=expected_fingerprints or None,
            )

    observations = get_observations(
        resolved_store_path,
        workspace=resolved_workspace,
        since=resolved_since,
        until=resolved_until,
        all_sessions=all_sessions,
        source_run_ids=[int(source_run["source_run_id"]) for source_run in source_runs],
    )
    activities = get_activities(
        resolved_store_path,
        workspace=resolved_workspace,
        requested_date=normalized_date,
        since=resolved_since,
        until=resolved_until,
        all_sessions=all_sessions,
        group_window_minutes=group_window_minutes,
        max_span_minutes=max_span_minutes,
        preloaded_observations=observations,
    )
    timeline = [dict(observation["event"]) for observation in observations]
    groups = [dict(activity["activity"]) for activity in activities]

    fp_to_group_id: dict[str, str] = {}
    for activity in activities:
        group_id = activity["activity_id"]
        for fp in activity.get("observation_fingerprints", []):
            fp_to_group_id[fp] = group_id
    for i, observation in enumerate(observations):
        fp = observation.get("event_fingerprint")
        if fp and fp in fp_to_group_id:
            timeline[i]["group_id"] = fp_to_group_id[fp]

    source_results = [_source_run_as_result(source_run) for source_run in source_runs]
    payload_scope_mode = _payload_scope_mode(groups, source_runs)

    report_date, output_dir = artifact_output_paths(
        normalized_date=normalized_date,
        resolved_since=resolved_since,
        resolved_until=resolved_until,
    )
    ensure_artifact_output_dir(output_dir)

    payload: dict[str, Any] = {
        "status": "success",
        "generated_at": isoformat(datetime.now().astimezone()),
        "workspace": str(resolved_workspace),
        "report_date": report_date,
        "report_date_context": {
            "timezone_basis": "local",
            "day_boundary_hour": REPORT_DAY_BOUNDARY_HOUR,
        },
        "output_dir": output_dir,
        "scope_mode": payload_scope_mode,
        "filters": {
            "since": since,
            "until": until,
            "date": date,
            "all_sessions": all_sessions,
            "group_window": group_window_minutes,
            "max_span": max_span_minutes,
        },
        "config": {
            "store_path": str(resolved_store_path),
            "group_window_minutes": group_window_minutes,
            "max_span_minutes": max_span_minutes,
            "projection_source": "store-backed",
            "hydrate_missing": hydrate_missing,
            **({"slice_completeness": slice_completeness} if slice_completeness else {}),
        },
        "sources": [_source_run_summary(source_run) for source_run in source_runs],
        "timeline": timeline,
        "groups": groups,
        "summary": build_summary(source_results, timeline, groups),
    }
    if include_patterns:
        observation_mode = "all-sessions" if all_sessions else "workspace"
        payload["patterns"] = get_patterns(
            resolved_store_path,
            workspace=None if all_sessions else resolved_workspace,
            observation_mode=observation_mode,
            days=pattern_days,
        )
        payload["pattern_context"] = {
            "observation_mode": observation_mode,
            "days": pattern_days,
        }
    return payload
