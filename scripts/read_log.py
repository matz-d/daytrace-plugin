#!/usr/bin/env python3
"""Read collected logs back from the DayTrace store as one time-ordered timeline."""

from __future__ import annotations

import argparse
from collections import Counter

from aggregate_core import resolve_date_filters
from common import emit, ensure_datetime, resolve_workspace
from derived_store import get_observations
from store import resolve_store_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Print stored DayTrace events in time order.")
    parser.add_argument("--workspace", default=".", help="Workspace the logs were collected for.")
    parser.add_argument("--date", help="today, yesterday, or YYYY-MM-DD.")
    parser.add_argument("--since", help="Start datetime or date (inclusive).")
    parser.add_argument("--until", help="End datetime or date (inclusive).")
    parser.add_argument("--source", action="append", dest="source_names", help="Only these source names.")
    parser.add_argument("--store-path", help="Path to the DayTrace SQLite store.")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser


def read_timeline(
    *,
    workspace,
    since: str | None,
    until: str | None,
    source_names: list[str] | None,
    store_path,
) -> list[dict]:
    observations = get_observations(
        store_path,
        workspace=workspace,
        since=since,
        until=until,
        all_sessions=True,
        source_names=source_names,
        daily_runs_only=True,
    )
    return [dict(observation["event"]) for observation in observations]


def text_lines(timeline: list[dict]) -> list[str]:
    lines = []
    for event in timeline:
        moment = ensure_datetime(event.get("timestamp"))
        clock = moment.astimezone().strftime("%m-%d %H:%M") if moment else "--"
        summary = str(event.get("summary") or "").replace("\n", " ")
        lines.append(f"{clock}  {event.get('source')}  {event.get('type')}  {summary}")
    return lines


def main() -> None:
    args = build_parser().parse_args()
    since, until = resolve_date_filters(args.date, args.since, args.until)
    timeline = read_timeline(
        workspace=resolve_workspace(args.workspace),
        since=since,
        until=until,
        source_names=args.source_names,
        store_path=resolve_store_path(args.store_path),
    )
    if args.format == "text":
        print("\n".join(text_lines(timeline)))
        return
    emit(
        {
            "status": "success",
            "since": since,
            "until": until,
            "counts": dict(Counter(str(event.get("source")) for event in timeline)),
            "timeline": timeline,
        }
    )


if __name__ == "__main__":
    main()
