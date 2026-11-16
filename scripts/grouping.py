"""Group a merged timeline into nearby-activity groups with confidence, scope, share policy, and evidence."""

from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from common import ensure_datetime

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
    # Claude / Codex events: the first request excerpt and the files the session touched.
    user_highlights = details.get("user_highlights")
    if isinstance(user_highlights, list) and user_highlights:
        tokens |= _context_tokens_from_text(str(user_highlights[0]))
    referenced_files = details.get("referenced_files")
    if isinstance(referenced_files, list):
        for item in referenced_files[:5]:
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
    if details.get("referenced_files") or details.get("user_highlights"):
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
    browser_context: dict[str, Any] | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    auto_exclude_from_share = False
    requires_confirmation = False

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


def _split_into_ranges(
    timeline: list[dict[str, Any]],
    *,
    group_window_minutes: int,
    max_span_minutes: int,
    confidence_categories_by_source: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Cut the time-ordered timeline into ranges: a new range starts on a time gap,
    when the range grows past the max span, or when the work context changes."""
    ranges: list[dict[str, Any]] = []
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

        ranges.append(current)
        current = {"events": [event], "start": event_time, "end": event_time}

    if current is not None:
        ranges.append(current)
    return ranges


def _build_group(
    group_id: str,
    group_range: dict[str, Any],
    *,
    confidence_categories_by_source: dict[str, list[str]],
    scope_mode_by_source: dict[str, str],
    evidence_limit: int,
) -> dict[str, Any]:
    events = group_range["events"]
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
    scope_modes = {scope_mode_by_source[name] for name in source_names if scope_mode_by_source.get(name)}
    scope_breakdown = sorted(scope_modes)
    mixed_scope = len(scope_modes) > 1
    browser_context = _browser_group_context(events) if "browser" in categories else None
    share_policy = _group_share_policy(
        events,
        categories=categories,
        confidence=confidence,
        mixed_scope=mixed_scope,
        browser_context=browser_context,
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
    return {
        "id": group_id,
        "start_timestamp": group_range["start"].isoformat(),
        "end_timestamp": group_range["end"].isoformat(),
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


def build_groups(
    timeline: list[dict[str, Any]],
    *,
    group_window_minutes: int,
    confidence_categories_by_source: dict[str, list[str]],
    evidence_limit: int = EVIDENCE_LIMIT,
    max_span_minutes: int = DEFAULT_MAX_SPAN_MINUTES,
    scope_mode_by_source: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Group the time-ordered timeline. Also sets `group_id` on each timeline event."""
    ranges = _split_into_ranges(
        timeline,
        group_window_minutes=group_window_minutes,
        max_span_minutes=max_span_minutes,
        confidence_categories_by_source=confidence_categories_by_source,
    )
    return [
        _build_group(
            f"group-{index:03d}",
            group_range,
            confidence_categories_by_source=confidence_categories_by_source,
            scope_mode_by_source=scope_mode_by_source or {},
            evidence_limit=evidence_limit,
        )
        for index, group_range in enumerate(ranges, start=1)
    ]
