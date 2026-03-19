#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys

from aggregate_core import DEFAULT_GROUP_WINDOW_MINUTES, DEFAULT_MAX_SPAN_MINUTES
from common import emit
from projection_adapters import build_projection_payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build aggregate-compatible post-draft input from the DayTrace store.")
    parser.add_argument("--workspace", default=".", help="Workspace path.")
    parser.add_argument("--date", help="Date shorthand. Accepts today, yesterday, or YYYY-MM-DD.")
    parser.add_argument("--since", help="Start datetime or date (inclusive).")
    parser.add_argument("--until", help="End datetime or date (inclusive).")
    parser.add_argument("--all-sessions", action="store_true", help="Reuse all-session observations when available.")
    parser.add_argument("--sources-file", help="Optional sources.json path used only when hydrating missing store slices.")
    parser.add_argument("--store-path", help="Path to the DayTrace SQLite store.")
    parser.add_argument("--group-window", type=int, default=DEFAULT_GROUP_WINDOW_MINUTES, help="Minutes for activity grouping.")
    parser.add_argument("--max-span", type=int, default=DEFAULT_MAX_SPAN_MINUTES, help="Maximum span in minutes for a single group.")
    parser.add_argument("--pattern-days", type=int, default=7, help="Pattern window to attach when cached skill-miner patterns exist.")
    parser.add_argument("--no-hydrate", action="store_true", help="Do not run aggregate.py when the matching store slice is missing.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        emit(
            build_projection_payload(
                workspace=args.workspace,
                date=args.date,
                since=args.since,
                until=args.until,
                all_sessions=args.all_sessions,
                sources_file=args.sources_file,
                store_path=args.store_path,
                group_window_minutes=args.group_window,
                max_span_minutes=args.max_span,
                hydrate_missing=not args.no_hydrate,
                include_patterns=True,
                pattern_days=args.pattern_days,
            )
        )
    except Exception as exc:
        emit({"status": "error", "message": str(exc)})
        sys.exit(1)


if __name__ == "__main__":
    main()
