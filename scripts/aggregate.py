#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from aggregate_core import (
    DEFAULT_GROUP_WINDOW_MINUTES,
    DEFAULT_MAX_SPAN_MINUTES,
    EVIDENCE_LIMIT,
    build_groups,
    build_preflight_summary,
    build_summary,
    collect_source_results,
    collect_timeline,
    resolve_date_filters,
    select_sources,
    summarize_source_result,
)
from common import current_platform, emit, isoformat, resolve_workspace
from source_registry import DEFAULT_USER_SOURCES_DIR, RegistryValidationError, load_registry, normalize_confidence_categories
from store import persist_source_result, resolve_store_path

SCRIPT_DIR = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate DayTrace source CLIs into a reusable timeline JSON.")
    parser.add_argument("--workspace", default=".", help="Workspace path. Used as cwd for source CLIs.")
    parser.add_argument("--date", help="Date shorthand. Accepts today, yesterday, or YYYY-MM-DD.")
    parser.add_argument("--since", help="Start datetime or date (inclusive).")
    parser.add_argument("--until", help="End datetime or date (inclusive).")
    parser.add_argument("--all-sessions", action="store_true", help="Pass --all-sessions to sources that support it.")
    parser.add_argument("--sources-file", default=str(SCRIPT_DIR / "sources.json"), help="Path to sources.json.")
    parser.add_argument(
        "--user-sources-dir",
        help="Optional user drop-in manifest directory. Defaults to ~/.config/daytrace/sources.d when using built-in sources.json.",
    )
    parser.add_argument("--source", action="append", dest="source_names", help="Specific source name(s) to run.")
    parser.add_argument("--group-window", type=int, default=DEFAULT_GROUP_WINDOW_MINUTES, help="Minutes for grouping nearby events.")
    parser.add_argument("--max-span", type=int, default=DEFAULT_MAX_SPAN_MINUTES, help="Maximum span in minutes for a single group. 0 to disable.")
    parser.add_argument("--max-workers", type=int, help="Maximum concurrent source processes.")
    parser.add_argument("--store-path", help="Path to the DayTrace SQLite store.")
    parser.add_argument("--no-store", action="store_true", help="Disable SQLite store ingestion for this run.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        workspace = resolve_workspace(args.workspace)
        if args.group_window < 0:
            raise ValueError("--group-window must be >= 0")
        if args.max_span < 0:
            raise ValueError("--max-span must be >= 0")
        default_sources_file = (SCRIPT_DIR / "sources.json").resolve()
        sources_file = Path(args.sources_file).expanduser().resolve()
        include_user_sources = sources_file == default_sources_file or bool(args.user_sources_dir)
        user_sources_dir = Path(args.user_sources_dir).expanduser().resolve() if args.user_sources_dir else DEFAULT_USER_SOURCES_DIR
        store_path = resolve_store_path(args.store_path)
        since_arg, until_arg = resolve_date_filters(args.date, args.since, args.until)
        sources = load_registry(
            sources_file,
            user_sources_dir=user_sources_dir,
            include_user_sources=include_user_sources,
        )
        confidence_categories_by_source = {
            source["name"]: normalize_confidence_categories(source) for source in sources
        }
        scope_mode_by_source = {source["name"]: source["scope_mode"] for source in sources}
        runnable_sources, skipped_sources = select_sources(
            sources,
            source_names=args.source_names,
            platform_name=current_platform(),
        )
        print(
            build_preflight_summary(
                runnable_sources,
                skipped_sources,
                workspace=workspace,
                script_dir=SCRIPT_DIR,
            ),
            file=sys.stderr,
        )

        max_workers = args.max_workers or max(1, len(runnable_sources))
        source_results = collect_source_results(
            runnable_sources,
            skipped_sources,
            workspace=workspace,
            since=since_arg,
            until=until_arg,
            all_sessions=args.all_sessions,
            max_workers=max_workers,
            script_dir=SCRIPT_DIR,
        )
        store_errors: list[str] = []
        if not args.no_store:
            selected_names = {source["name"] for source in runnable_sources} | {result["source"] for result in skipped_sources}
            source_lookup = {source["name"]: source for source in sources if source["name"] in selected_names}
            normalized_date = since_arg if args.date else None
            for result in source_results:
                source_name = result.get("manifest_source_name", result["source"])
                try:
                    source = source_lookup[source_name]
                    persist_source_result(
                        result,
                        source,
                        workspace=workspace,
                        requested_date=normalized_date,
                        since=since_arg,
                        until=until_arg,
                        all_sessions=args.all_sessions,
                        store_path=store_path,
                    )
                except Exception as store_exc:
                    error_message = f"{source_name}: {store_exc}"
                    store_errors.append(error_message)
                    print(f"[warn] store persistence failed for {source_name}: {store_exc}", file=sys.stderr)
        timeline = collect_timeline(source_results)
        groups = build_groups(
            timeline,
            group_window_minutes=args.group_window,
            confidence_categories_by_source=confidence_categories_by_source,
            evidence_limit=EVIDENCE_LIMIT,
            max_span_minutes=args.max_span,
            scope_mode_by_source=scope_mode_by_source,
        )

        emit(
            {
                "status": "success",
                "generated_at": isoformat(datetime.now().astimezone()),
                "workspace": str(workspace),
                "filters": {
                    "since": args.since,
                    "until": args.until,
                    "date": args.date,
                    "all_sessions": args.all_sessions,
                    "group_window": args.group_window,
                    "max_span": args.max_span,
                },
                "config": {
                    "sources_file": str(sources_file),
                    "user_sources_dir": str(user_sources_dir) if include_user_sources else None,
                    "store_path": None if args.no_store else str(store_path),
                    "group_window_minutes": args.group_window,
                    "max_span_minutes": args.max_span,
                    "evidence_limit": EVIDENCE_LIMIT,
                    **({"store_error": store_errors[0]} if store_errors else {}),
                    **({"store_errors": store_errors} if store_errors else {}),
                },
                "sources": [summarize_source_result(result) for result in sorted(source_results, key=lambda item: item["source"])],
                "timeline": timeline,
                "groups": groups,
                "summary": build_summary(source_results, timeline, groups),
            }
        )
    except RegistryValidationError as exc:
        emit({"status": "error", "message": str(exc), "registry_errors": exc.issues})
        sys.exit(1)
    except Exception as exc:
        emit({"status": "error", "message": str(exc)})
        sys.exit(1)


if __name__ == "__main__":
    main()
