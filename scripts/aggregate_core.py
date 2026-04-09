from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from common import default_chrome_root, ensure_datetime, parse_datetime
from source_registry import DEFAULT_USER_SOURCES_DIR, load_registry


DEFAULT_GROUP_WINDOW_MINUTES = 15
DEFAULT_MAX_SPAN_MINUTES = 60
EVIDENCE_LIMIT = 5

# Priority order for salience-based evidence selection.
# Lower value = higher priority when selecting representative evidence.
EVIDENCE_CATEGORY_PRIORITY: dict[str, int] = {
    "git": 0,
    "ai_history": 1,
    "browser": 2,
    "file_activity": 3,
}
REQUIRED_EVENT_FIELDS = {"source", "timestamp", "type", "summary", "details", "confidence"}
EVENT_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
CONTEXT_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_./+-]+|[一-龥ぁ-んァ-ン]+")
LOW_SIGNAL_CATEGORIES = {"browser", "file_activity"}
STRONG_SIGNAL_CATEGORIES = {"git", "ai_history"}
SHARE_EXCLUDE_BROWSER_EVENT_THRESHOLD = 8
SHARE_EXCLUDE_BROWSER_PAGE_THRESHOLD = 12
SHARE_EXCLUDE_BROWSER_FLOW_THRESHOLD = 4
SHARE_EXCLUDE_BROWSER_COMPRESSED_EVENT_THRESHOLD = 3
SHARE_SENSITIVE_BROWSER_HOSTS = {
    "x.com",
    "twitter.com",
    "facebook.com",
    "instagram.com",
    "google.com",
    "google.co.jp",
}
SHARE_SENSITIVE_BROWSER_HOST_SUFFIXES = ("auth0.com",)


def report_day_for_local_time(now: datetime) -> date:
    """Calendar day used for daily-report output dirs and --date today/yesterday.

    Before 06:00 wall clock in ``now``'s timezone, counts as the previous calendar day
    (late-night session). When ``resolve_date_filters`` is called without ``now``, uses
    ``datetime.now().astimezone()`` so the machine's local offset applies.
    """
    if now.tzinfo is None:
        now = datetime.now().astimezone()
    cal_date = now.date()
    if now.hour < 6:
        return cal_date - timedelta(days=1)
    return cal_date


def resolve_date_filters(
    date_arg: str | None,
    since: str | None,
    until: str | None,
    *,
    now: datetime | None = None,
) -> tuple[str | None, str | None]:
    if date_arg and (since or until):
        raise ValueError("--date cannot be combined with --since or --until")
    if not date_arg:
        return since, until

    lowered = date_arg.strip().lower()
    anchor = now or datetime.now().astimezone()
    report_day = report_day_for_local_time(anchor)
    if lowered == "today":
        target = report_day
    elif lowered == "yesterday":
        target = report_day - timedelta(days=1)
    else:
        target = parse_datetime(date_arg, bound="start").date()
    iso_day = target.isoformat()
    return iso_day, iso_day


def resolve_command_paths(tokens: list[str], *, script_dir: Path) -> list[str]:
    resolved = list(tokens)
    for index, token in enumerate(resolved):
        if token.endswith(".py") or token.endswith(".sh"):
            candidate = script_dir / Path(token).name
            if candidate.exists():
                resolved[index] = str(candidate)
    return resolved


def build_command(
    source: dict[str, Any],
    *,
    workspace: Path,
    since: str | None,
    until: str | None,
    all_sessions: bool,
    script_dir: Path,
) -> list[str]:
    command = resolve_command_paths(shlex.split(source["command"]), script_dir=script_dir)
    command.extend(["--workspace", str(workspace)])
    if source.get("supports_date_range"):
        if since:
            command.extend(["--since", since])
        if until:
            command.extend(["--until", until])
    if all_sessions and source.get("supports_all_sessions"):
        command.append("--all-sessions")
    return command


def summarize_source_result(result: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "name": result["source"],
        "status": result["status"],
        "scope": result["scope"],
        "events_count": len(result.get("events", [])),
    }
    for key in ("reason", "message", "command", "duration_sec"):
        if key in result:
            summary[key] = result[key]
    return summary


def normalize_event(event: dict[str, Any], source_name: str) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    normalized = dict(event)
    normalized["source"] = normalized.get("source") or source_name
    if set(normalized.keys()) & REQUIRED_EVENT_FIELDS != REQUIRED_EVENT_FIELDS:
        missing = REQUIRED_EVENT_FIELDS - set(normalized.keys())
        if missing:
            return None

    timestamp = ensure_datetime(normalized.get("timestamp"))
    if timestamp is None:
        return None
    normalized["timestamp"] = timestamp.isoformat()
    if not isinstance(normalized.get("details"), dict):
        normalized["details"] = {"raw_details": normalized.get("details")}
    return normalized


def error_result(source: dict[str, Any], message: str, command: list[str], duration_sec: float) -> dict[str, Any]:
    return {
        "status": "error",
        "source": source["name"],
        "manifest_source_name": source["name"],
        "scope": source["scope_mode"],
        "message": message,
        "events": [],
        "command": command,
        "duration_sec": round(duration_sec, 3),
    }


def normalize_source_payload(
    source: dict[str, Any],
    payload: dict[str, Any],
    *,
    command: list[str],
    duration_sec: float,
) -> dict[str, Any]:
    status = payload.get("status")
    if status not in {"success", "skipped", "error"}:
        return error_result(source, "Source returned an unknown status", command, duration_sec)

    normalized_events = []
    for raw_event in payload.get("events", []):
        event = normalize_event(raw_event, source["name"])
        if event:
            normalized_events.append(event)

    normalized = {
        "status": status,
        "source": payload.get("source") or source["name"],
        "manifest_source_name": source["name"],
        "scope": source["scope_mode"],
        "events": normalized_events,
        "command": command,
        "duration_sec": round(duration_sec, 3),
    }
    for key in ("reason", "message"):
        if key in payload:
            normalized[key] = payload[key]
    return normalized


def run_source(
    source: dict[str, Any],
    *,
    workspace: Path,
    since: str | None,
    until: str | None,
    all_sessions: bool,
    script_dir: Path,
) -> dict[str, Any]:
    command = build_command(
        source,
        workspace=workspace,
        since=since,
        until=until,
        all_sessions=all_sessions,
        script_dir=script_dir,
    )
    started = datetime.now().timestamp()
    try:
        # `source` is expected to come from `source_registry.load_sources()`,
        # which validates that `timeout_sec` exists and is a positive number.
        completed = subprocess.run(
            command,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=int(source["timeout_sec"]),
            check=False,
        )
    except subprocess.TimeoutExpired:
        duration = datetime.now().timestamp() - started
        return error_result(source, "Source timed out", command, duration)
    except Exception as exc:
        duration = datetime.now().timestamp() - started
        return error_result(source, str(exc), command, duration)

    duration = datetime.now().timestamp() - started
    stdout = completed.stdout.strip()
    if not stdout:
        return error_result(source, completed.stderr.strip() or "Source returned empty stdout", command, duration)

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return error_result(source, "Source returned invalid JSON", command, duration)

    normalized = normalize_source_payload(source, payload, command=command, duration_sec=duration)
    if completed.returncode != 0 and normalized["status"] == "success":
        normalized["status"] = "error"
        normalized["message"] = completed.stderr.strip() or "Source exited with a non-zero status"
        normalized["events"] = []
    return normalized


def select_sources(
    sources: list[dict[str, Any]],
    *,
    source_names: list[str] | None,
    platform_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requested = set(source_names or [])
    selected_sources = list(sources)
    if requested:
        available_names = {source["name"] for source in selected_sources}
        missing = sorted(requested - available_names)
        if missing:
            raise ValueError(f"Unknown source name(s): {', '.join(missing)}")
        selected_sources = [source for source in selected_sources if source["name"] in requested]

    runnable = []
    skipped = []
    for source in selected_sources:
        if platform_name not in source["platforms"]:
            skipped.append(
                {
                    "status": "skipped",
                    "source": source["name"],
                    "manifest_source_name": source["name"],
                    "scope": source["scope_mode"],
                    "reason": "unsupported_platform",
                    "events": [],
                    "command": shlex.split(source["command"]),
                    "duration_sec": 0.0,
                }
            )
            continue
        runnable.append(source)
    return runnable, skipped


def git_repo_available(workspace: Path) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0
    )


def evaluate_prerequisite(prerequisite: dict[str, Any], workspace: Path) -> tuple[bool, str | None]:
    prereq_type = prerequisite.get("type")
    if prereq_type == "git_repo":
        return (git_repo_available(workspace), "not_git_repo")
    if prereq_type == "path_exists":
        path = Path(str(prerequisite["path"])).expanduser()
        return (path.exists(), "not_found")
    if prereq_type == "all_paths_exist":
        paths = [Path(str(path)).expanduser() for path in prerequisite.get("paths", [])]
        return (all(path.exists() for path in paths), "not_found")
    if prereq_type == "glob_exists":
        base = Path(str(prerequisite["base"])).expanduser()
        pattern = str(prerequisite["pattern"])
        return (base.exists() and any(base.glob(pattern)), "not_found")
    if prereq_type == "chrome_history_db":
        chrome_root = default_chrome_root()
        if chrome_root is None:
            return (True, None)
        history_paths = list(chrome_root.glob("Default/History")) + list(chrome_root.glob("Profile */History"))
        return (bool(history_paths), "not_found")
    raise ValueError(f"Unsupported prerequisite type: {prereq_type}")


def source_availability(source: dict[str, Any], workspace: Path, *, script_dir: Path) -> tuple[str, str | None]:
    command = resolve_command_paths(shlex.split(source["command"]), script_dir=script_dir)
    script_token = next((token for token in command if token.endswith(".py") or token.endswith(".sh")), None)
    if script_token and not Path(script_token).exists():
        return "unavailable", "command_missing"

    for prerequisite in source.get("prerequisites", []):
        is_available, reason = evaluate_prerequisite(prerequisite, workspace)
        if not is_available:
            return "unavailable", reason

    return "available", None


def expected_source_names(
    sources: list[dict[str, Any]],
    *,
    platform_name: str,
    workspace: Path,
    script_dir: Path,
) -> set[str]:
    """Return source names expected to produce results.

    Excludes sources that are unsupported on the current platform
    or that fail prerequisite checks, since those would never run.
    """
    runnable, _skipped = select_sources(sources, source_names=None, platform_name=platform_name)
    names: set[str] = set()
    for source in runnable:
        status, _reason = source_availability(source, workspace, script_dir=script_dir)
        if status == "available":
            names.add(source["name"])
    return names


def resolve_sources_file_path(
    sources_file: str | Path | None,
    *,
    default_sources_file: Path,
) -> Path:
    return Path(sources_file).expanduser().resolve() if sources_file else default_sources_file


def load_expected_sources(
    sources_file: Path,
    *,
    platform_name: str,
    workspace: Path,
    script_dir: Path,
    restrict_to_names: set[str] | None = None,
) -> tuple[set[str], dict[str, str]]:
    default_sources_file = (script_dir / "sources.json").resolve()
    resolved_sources_file = sources_file.resolve()
    sources = load_registry(
        resolved_sources_file,
        user_sources_dir=DEFAULT_USER_SOURCES_DIR,
        include_user_sources=resolved_sources_file == default_sources_file,
    )
    names = expected_source_names(
        sources,
        platform_name=platform_name,
        workspace=workspace,
        script_dir=script_dir,
    )
    if restrict_to_names is not None:
        names &= restrict_to_names
    fingerprint_map = {
        source["name"]: source["manifest_fingerprint"]
        for source in sources
        if source["name"] in names
    }
    return names, fingerprint_map


def build_preflight_summary(
    runnable_sources: list[dict[str, Any]],
    skipped_sources: list[dict[str, Any]],
    *,
    workspace: Path,
    script_dir: Path,
) -> str:
    available: list[str] = []
    unavailable: list[str] = []
    skipped: list[str] = []

    for source in runnable_sources:
        status, reason = source_availability(source, workspace, script_dir=script_dir)
        if status == "available":
            available.append(source["name"])
        else:
            unavailable.append(f"{source['name']}({reason})")

    for source in skipped_sources:
        skipped.append(f"{source['source']}({source.get('reason', 'skipped')})")

    parts = [
        f"workspace={workspace}",
        "available=" + (", ".join(sorted(available)) if available else "none"),
    ]
    if unavailable:
        parts.append("unavailable=" + ", ".join(sorted(unavailable)))
    if skipped:
        parts.append("skipped=" + ", ".join(sorted(skipped)))
    return "Source preflight: " + " | ".join(parts)


def collect_source_results(
    runnable_sources: list[dict[str, Any]],
    skipped_sources: list[dict[str, Any]],
    *,
    workspace: Path,
    since: str | None,
    until: str | None,
    all_sessions: bool,
    max_workers: int,
    script_dir: Path,
) -> list[dict[str, Any]]:
    source_results = list(skipped_sources)
    if not runnable_sources:
        return source_results

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                run_source,
                source,
                workspace=workspace,
                since=since,
                until=until,
                all_sessions=all_sessions,
                script_dir=script_dir,
            ): source["name"]
            for source in runnable_sources
        }
        for future in as_completed(futures):
            source_results.append(future.result())
    return source_results


def collect_timeline(source_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    for result in source_results:
        if result["status"] == "success":
            timeline.extend(result["events"])
    timeline.sort(key=lambda event: ensure_datetime(event["timestamp"]))
    return timeline


def _confidence_rank(value: str | None) -> int | None:
    if value is None:
        return None
    lowered = str(value).strip().lower()
    if lowered not in EVENT_CONFIDENCE_RANK:
        return None
    return EVENT_CONFIDENCE_RANK[lowered]


def _confidence_label(rank: int) -> str:
    normalized = max(0, min(rank, 2))
    return {0: "low", 1: "medium", 2: "high"}[normalized]


def group_confidence(categories: set[str], event_confidence_breakdown: dict[str, int] | None = None) -> str:
    has_git = "git" in categories
    has_ai = "ai_history" in categories
    if has_git and has_ai:
        base_confidence = "high"
    elif has_git or has_ai:
        base_confidence = "medium"
    else:
        base_confidence = "low"
    if not event_confidence_breakdown:
        return base_confidence
    available_ranks = [
        rank
        for confidence, count in event_confidence_breakdown.items()
        for rank in [_confidence_rank(confidence)]
        if rank is not None and int(count or 0) > 0
    ]
    if not available_ranks:
        return base_confidence
    return _confidence_label(min(_confidence_rank(base_confidence) or 0, max(available_ranks)))


def _event_categories(event: dict[str, Any], confidence_categories_by_source: dict[str, list[str]]) -> set[str]:
    return set(confidence_categories_by_source.get(str(event.get("source") or ""), []))


def _context_tokens_from_text(text: str) -> set[str]:
    return {
        token.lower()
        for token in CONTEXT_TOKEN_PATTERN.findall(text or "")
        if len(token) > 2
    }


def _event_context_tokens(event: dict[str, Any]) -> set[str]:
    tokens = _context_tokens_from_text(str(event.get("summary") or ""))
    details = event.get("details", {})
    if not isinstance(details, dict):
        return tokens
    ai_observation = details.get("ai_observation") or details.get("skill_miner_packet")
    if isinstance(ai_observation, dict):
        tokens |= _context_tokens_from_text(str(ai_observation.get("primary_intent") or ""))
        for item in ai_observation.get("referenced_files", [])[:5]:
            tokens |= _context_tokens_from_text(str(item))
    changed_files = details.get("changed_files")
    if isinstance(changed_files, list):
        for item in changed_files[:5]:
            if isinstance(item, dict):
                tokens |= _context_tokens_from_text(str(item.get("path") or ""))
    for key in ("path", "title", "url", "body_summary"):
        value = details.get(key)
        if isinstance(value, str):
            tokens |= _context_tokens_from_text(value)
    return tokens


def _event_has_rich_context(event: dict[str, Any]) -> bool:
    details = event.get("details", {})
    if not isinstance(details, dict):
        return False
    if isinstance(details.get("ai_observation") or details.get("skill_miner_packet"), dict):
        return True
    if isinstance(details.get("changed_files"), list) and details.get("changed_files"):
        return True
    for key in ("path", "title", "url"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _group_context_tokens(events: list[dict[str, Any]]) -> set[str]:
    tokens: set[str] = set()
    for event in events:
        tokens |= _event_context_tokens(event)
    return tokens


def _should_split_by_context(
    current_events: list[dict[str, Any]],
    event: dict[str, Any],
    *,
    confidence_categories_by_source: dict[str, list[str]],
) -> bool:
    current_tokens = _group_context_tokens(current_events)
    incoming_tokens = _event_context_tokens(event)
    if not current_tokens or not incoming_tokens or current_tokens & incoming_tokens:
        return False
    if not _event_has_rich_context(event) or not any(_event_has_rich_context(current_event) for current_event in current_events):
        return False
    current_categories = {
        category
        for current_event in current_events
        for category in _event_categories(current_event, confidence_categories_by_source)
    }
    incoming_categories = _event_categories(event, confidence_categories_by_source)
    current_has_strong = bool(current_categories & STRONG_SIGNAL_CATEGORIES)
    incoming_has_strong = bool(incoming_categories & STRONG_SIGNAL_CATEGORIES)
    current_low_only = bool(current_categories) and current_categories <= LOW_SIGNAL_CATEGORIES
    incoming_low_only = bool(incoming_categories) and incoming_categories <= LOW_SIGNAL_CATEGORIES
    return (current_has_strong and incoming_low_only) or (incoming_has_strong and current_low_only)


def _browser_group_context(events: list[dict[str, Any]]) -> dict[str, Any]:
    host_counts: Counter[str] = Counter()
    flow_counts: Counter[str] = Counter()
    total_page_count = 0
    compressed_event_count = 0

    for event in events:
        details = event.get("details")
        if not isinstance(details, dict):
            continue
        host = str(details.get("host") or "").strip().lower()
        if not host:
            raw_url = str(details.get("url") or "").strip()
            if raw_url:
                host = re.sub(r"^www\.", "", urlsplit(raw_url).netloc.split(":", 1)[0].lower())
        flow = str(details.get("flow_key") or "").strip().lower()
        if host:
            host_counts[host] += 1
        if flow:
            flow_counts[flow] += 1
        try:
            total_page_count += int(details.get("page_count", 1) or 1)
        except (TypeError, ValueError):
            total_page_count += 1
        if bool(details.get("compressed", False)):
            compressed_event_count += 1

    dominant_host = ""
    dominant_host_share = 0.0
    if host_counts:
        dominant_host, dominant_host_count = host_counts.most_common(1)[0]
        dominant_host_share = round(dominant_host_count / max(sum(host_counts.values()), 1), 3)

    sensitive_hosts = sorted(
        host
        for host in host_counts
        if host in SHARE_SENSITIVE_BROWSER_HOSTS or host.endswith(SHARE_SENSITIVE_BROWSER_HOST_SUFFIXES)
    )

    return {
        "host_count": len(host_counts),
        "hosts": sorted(host_counts.keys())[:8],
        "dominant_host": dominant_host or None,
        "dominant_host_share": dominant_host_share,
        "flow_count": len(flow_counts),
        "flows": sorted(flow_counts.keys())[:8],
        "total_page_count": total_page_count,
        "compressed_event_count": compressed_event_count,
        "sensitive_hosts": sensitive_hosts,
        "sensitive_host_count": len(sensitive_hosts),
    }


def _group_share_policy(
    events: list[dict[str, Any]],
    *,
    categories: set[str],
    confidence: str,
    mixed_scope: bool,
) -> dict[str, Any]:
    reasons: list[str] = []
    auto_exclude_from_share = False
    requires_confirmation = False
    browser_context = _browser_group_context(events) if "browser" in categories else None

    if mixed_scope:
        reasons.append("mixed_scope")
        requires_confirmation = True

    if categories == {"browser"} and confidence == "low":
        reasons.append("browser_only_low_confidence")
        requires_confirmation = True
        browser_event_count = len(events)
        total_page_count = int((browser_context or {}).get("total_page_count", 0) or 0)
        flow_count = int((browser_context or {}).get("flow_count", 0) or 0)
        compressed_event_count = int((browser_context or {}).get("compressed_event_count", 0) or 0)
        if browser_event_count >= SHARE_EXCLUDE_BROWSER_EVENT_THRESHOLD:
            reasons.append("oversized_browser_cluster")
            auto_exclude_from_share = True
        if total_page_count >= SHARE_EXCLUDE_BROWSER_PAGE_THRESHOLD:
            reasons.append("high_browser_page_volume")
            auto_exclude_from_share = True
        if flow_count >= SHARE_EXCLUDE_BROWSER_FLOW_THRESHOLD:
            reasons.append("high_browser_flow_diversity")
            auto_exclude_from_share = True
        if compressed_event_count >= SHARE_EXCLUDE_BROWSER_COMPRESSED_EVENT_THRESHOLD:
            reasons.append("dense_browser_flow_cluster")
            auto_exclude_from_share = True
        if int((browser_context or {}).get("host_count", 0) or 0) >= 3:
            reasons.append("multi_domain_browser_cluster")
            auto_exclude_from_share = True
        if int((browser_context or {}).get("sensitive_host_count", 0) or 0) > 0:
            reasons.append("share_sensitive_browser_hosts")
            auto_exclude_from_share = True

    recommended_visibility = "share_safe"
    if auto_exclude_from_share:
        recommended_visibility = "private_only"
    elif requires_confirmation:
        recommended_visibility = "share_with_caution"

    return {
        "recommended_visibility": recommended_visibility,
        "auto_exclude_from_share": auto_exclude_from_share,
        "requires_confirmation": requires_confirmation,
        "reasons": reasons,
    }


def build_groups(
    timeline: list[dict[str, Any]],
    *,
    group_window_minutes: int,
    confidence_categories_by_source: dict[str, list[str]],
    evidence_limit: int = EVIDENCE_LIMIT,
    max_span_minutes: int = DEFAULT_MAX_SPAN_MINUTES,
    scope_mode_by_source: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    grouped_ranges = []
    current: dict[str, Any] | None = None
    window = timedelta(minutes=group_window_minutes)
    max_span = timedelta(minutes=max_span_minutes) if max_span_minutes > 0 else None

    for event in timeline:
        event_time = ensure_datetime(event["timestamp"])
        if current is None:
            current = {"events": [event], "start": event_time, "end": event_time}
            continue

        gap_ok = event_time - current["end"] <= window
        span_ok = max_span is None or event_time - current["start"] <= max_span
        context_ok = not _should_split_by_context(
            current["events"],
            event,
            confidence_categories_by_source=confidence_categories_by_source,
        )
        if gap_ok and span_ok and context_ok:
            current["events"].append(event)
            current["end"] = event_time
            continue

        grouped_ranges.append(current)
        current = {"events": [event], "start": event_time, "end": event_time}

    if current is not None:
        grouped_ranges.append(current)

    _scope_lookup = scope_mode_by_source or {}

    normalized_groups = []
    for index, group in enumerate(grouped_ranges, start=1):
        group_id = f"group-{index:03d}"
        events = group["events"]
        source_names = {event["source"] for event in events}
        categories = {
            category
            for source_name in source_names
            for category in confidence_categories_by_source.get(source_name, [])
        }
        event_confidence_breakdown: dict[str, int] = {}
        for event in events:
            confidence_label = str(event.get("confidence") or "").lower()
            if confidence_label:
                event_confidence_breakdown[confidence_label] = event_confidence_breakdown.get(confidence_label, 0) + 1
        confidence = group_confidence(categories, event_confidence_breakdown)
        confidence_basis = {
            "source_category_confidence": group_confidence(categories),
            "max_event_confidence": _confidence_label(
                max((_confidence_rank(label) or 0) for label in event_confidence_breakdown) if event_confidence_breakdown else 0
            ),
        }

        # confidence_breakdown: event count per confidence_category
        confidence_breakdown: dict[str, int] = {}
        for event in events:
            for category in confidence_categories_by_source.get(event["source"], []):
                confidence_breakdown[category] = confidence_breakdown.get(category, 0) + 1

        # scope_breakdown: set of scope_modes present in this group
        scope_modes: set[str] = set()
        for source_name in source_names:
            mode = _scope_lookup.get(source_name)
            if mode:
                scope_modes.add(mode)
        scope_breakdown = sorted(scope_modes)
        mixed_scope = len(scope_modes) > 1
        browser_context = _browser_group_context(events) if "browser" in categories else None
        share_policy = _group_share_policy(
            events,
            categories=categories,
            confidence=confidence,
            mixed_scope=mixed_scope,
        )

        # Salience-based evidence: prioritise git > ai_history > browser > file_activity
        def _evidence_sort_key(ev: dict[str, Any]) -> tuple[int, str]:
            source_cats = confidence_categories_by_source.get(ev["source"], [])
            best = min((EVIDENCE_CATEGORY_PRIORITY.get(c, 99) for c in source_cats), default=99)
            return (best, ev["timestamp"])

        sorted_for_evidence = sorted(events, key=_evidence_sort_key)
        evidence = [
            {
                "timestamp": event["timestamp"],
                "source": event["source"],
                "type": event["type"],
                "summary": event["summary"],
            }
            for event in sorted_for_evidence[:evidence_limit]
        ]

        # Keep `timeline` and `groups[].events` pointing at the same event objects so
        # the AR1a contract can expose `timeline[].group_id` without rebuilding copies.
        for event in events:
            event["group_id"] = group_id

        if len(events) == 1:
            summary = events[0]["summary"]
        else:
            lead = sorted_for_evidence[0]["summary"] if sorted_for_evidence else events[0]["summary"]
            summary = f"{lead} + {len(events) - 1} related activities"
        normalized_groups.append(
            {
                "id": group_id,
                "start_timestamp": group["start"].isoformat(),
                "end_timestamp": group["end"].isoformat(),
                "summary": summary,
                "confidence": confidence,
                "confidence_breakdown": confidence_breakdown,
                "event_confidence_breakdown": event_confidence_breakdown,
                "confidence_basis": confidence_basis,
                "sources": sorted(source_names),
                "confidence_categories": sorted(categories),
                "scope_breakdown": scope_breakdown,
                "mixed_scope": mixed_scope,
                "source_count": len(source_names),
                "event_count": len(events),
                **({"browser_context": browser_context} if browser_context else {}),
                "share_policy": share_policy,
                "evidence": evidence,
                "evidence_overflow_count": max(0, len(events) - evidence_limit),
                "events": events,
            }
        )

    return normalized_groups


def build_summary(source_results: list[dict[str, Any]], timeline: list[dict[str, Any]], groups: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {"success": 0, "skipped": 0, "error": 0}
    for result in source_results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1

    return {
        "source_status_counts": counts,
        "total_events": len(timeline),
        "total_groups": len(groups),
        "no_sources_available": counts["success"] == 0 and not timeline,
    }
