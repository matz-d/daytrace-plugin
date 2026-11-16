#!/usr/bin/env python3
"""Collect today's logs into the DayTrace store. Meant to run unattended (launchd / cron).

Runs `aggregate.py --date today`. When yesterday has no stored file-activity run
(the Mac was asleep or off at the scheduled time), it collects yesterday first.
Yesterday is not re-collected once stored: untracked-file activity is a snapshot,
and a later run would lose files that were committed in between.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from aggregate_core import report_day_for_local_time
from common import emit, resolve_workspace
from derived_store import get_source_runs
from store import resolve_store_path

SCRIPT_DIR = Path(__file__).resolve().parent
SNAPSHOT_SOURCE = "workspace-file-activity"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect today's DayTrace logs into the store.")
    parser.add_argument("--workspace", default=".", help="Repository whose git and file activity is collected.")
    parser.add_argument("--store-path", help="Path to the DayTrace SQLite store.")
    parser.add_argument("--sources-file", help="Path to sources.json. Defaults to the bundled registry.")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds before one collection is stopped.")
    return parser


def has_stored_snapshot(store_path: Path, workspace: Path, day: str) -> bool:
    runs = get_source_runs(
        store_path,
        workspace=workspace,
        requested_date=day,
        all_sessions=True,
        source_names=[SNAPSHOT_SOURCE],
    )
    return any(run.get("status") == "success" for run in runs)


def collect(day: str, *, workspace: Path, store_path: Path, timeout: int, sources_file: str | None = None) -> dict[str, object]:
    command = [
        sys.executable,
        str(SCRIPT_DIR / "aggregate.py"),
        "--date",
        day,
        "--all-sessions",
        "--workspace",
        str(workspace),
        "--store-path",
        str(store_path),
    ]
    if sources_file:
        command.extend(["--sources-file", sources_file])
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=str(workspace))
    if completed.returncode != 0:
        return {"date": day, "status": "error", "message": completed.stderr.strip()[-2000:]}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"date": day, "status": "error", "message": "aggregate.py did not return JSON"}
    if payload.get("status") != "success":
        return {"date": day, "status": "error", "message": str(payload.get("message") or "aggregate.py failed")}
    sources = [
        {
            "name": source.get("name"),
            "status": source.get("status"),
            "events_count": source.get("events_count"),
            **({"reason": source["reason"]} if source.get("reason") else {}),
        }
        for source in payload.get("sources", [])
    ]
    store_errors = payload.get("config", {}).get("store_errors") or []
    return {
        "date": day,
        "status": "error" if store_errors else "success",
        "sources": sources,
        **({"store_errors": store_errors} if store_errors else {}),
    }


def main() -> None:
    args = build_parser().parse_args()
    workspace = resolve_workspace(args.workspace)
    store_path = resolve_store_path(args.store_path)
    today = report_day_for_local_time(datetime.now().astimezone())
    yesterday = (today - timedelta(days=1)).isoformat()

    days = [today.isoformat()]
    if not has_stored_snapshot(store_path, workspace, yesterday):
        days.insert(0, yesterday)

    results = [
        collect(day, workspace=workspace, store_path=store_path, timeout=args.timeout, sources_file=args.sources_file)
        for day in days
    ]
    failed = [result for result in results if result["status"] != "success"]
    emit(
        {
            "status": "error" if failed else "success",
            "collected_at": datetime.now().astimezone().isoformat(),
            "workspace": str(workspace),
            "store_path": str(store_path),
            "runs": results,
        }
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
