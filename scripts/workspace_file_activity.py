#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from common import (
    apply_limit,
    emit,
    error_response,
    isoformat,
    is_within_path,
    parse_datetime,
    resolve_workspace,
    run_command,
    skipped_response,
    success_response,
    within_range,
)


SOURCE_NAME = "workspace-file-activity"
GIT_TIMEOUT_SEC = 10


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Emit untracked workspace file activity as DayTrace events.")
    parser.add_argument("--workspace", default=".", help="Workspace path to inspect. Defaults to cwd.")
    parser.add_argument("--since", help="Start datetime or date (inclusive).")
    parser.add_argument("--until", help="End datetime or date (inclusive).")
    parser.add_argument("--limit", type=int, help="Maximum number of events to return.")
    return parser


def repo_context(workspace: Path) -> tuple[Path, str] | None:
    repo_check = run_command(
        ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
        timeout=GIT_TIMEOUT_SEC,
    )
    if repo_check.returncode != 0:
        return None

    repo_root = Path(repo_check.stdout.strip()).resolve()
    relative = "."
    if workspace != repo_root:
        relative = str(workspace.relative_to(repo_root))
    return repo_root, relative


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    workspace: Path | None = None

    try:
        workspace = resolve_workspace(args.workspace)
        start = parse_datetime(args.since, bound="start")
        end = parse_datetime(args.until, bound="end")
        context = repo_context(workspace)
        if context is None:
            emit(skipped_response(SOURCE_NAME, "not_git_repo", workspace=str(workspace)))
            return

        repo_root, pathspec = context
        result = run_command(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-files",
                "--others",
                "--exclude-standard",
                "--",
                pathspec,
            ],
            timeout=GIT_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            emit(error_response(SOURCE_NAME, result.stderr.strip() or "git ls-files failed", workspace=str(workspace)))
            return

        rel_paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        events = []
        skipped_paths = []
        for rel_path in rel_paths:
            full_path = (repo_root / rel_path).resolve()
            try:
                if not is_within_path(full_path, workspace):
                    continue
                if not full_path.exists() or not full_path.is_file():
                    continue

                stats = full_path.stat()
            except PermissionError as exc:
                skipped_paths.append(
                    {
                        "path": rel_path,
                        "reason": "permission_denied",
                        "message": str(exc),
                    }
                )
                continue
            timestamp = isoformat(stats.st_mtime)
            if not within_range(timestamp, start, end):
                continue
            events.append(
                {
                    "source": SOURCE_NAME,
                    "timestamp": timestamp,
                    "type": "untracked_file",
                    "summary": f"Untracked file activity: {rel_path}",
                    "details": {
                        "workspace": str(workspace),
                        "repo_root": str(repo_root),
                        "path": rel_path,
                        "size_bytes": stats.st_size,
                        "mtime_epoch": stats.st_mtime,
                    },
                    "confidence": "low",
                }
            )

        events.sort(key=lambda event: event["timestamp"], reverse=True)
        emit(
            success_response(
                SOURCE_NAME,
                apply_limit(events, args.limit),
                workspace=str(workspace),
                repo_root=str(repo_root),
                since=args.since,
                until=args.until,
                **({"skipped_paths": skipped_paths} if skipped_paths else {}),
            )
        )
    except PermissionError as exc:
        payload = {"message": str(exc)}
        if workspace is not None:
            payload["workspace"] = str(workspace)
        emit(skipped_response(SOURCE_NAME, "permission_denied", **payload))
    except subprocess.TimeoutExpired as exc:
        payload = {"message": f"git command timed out after {exc.timeout}s"}
        if workspace is not None:
            payload["workspace"] = str(workspace)
        emit(error_response(SOURCE_NAME, payload.pop("message"), **payload))
    except Exception as exc:
        emit(error_response(SOURCE_NAME, str(exc)))


if __name__ == "__main__":
    main()
