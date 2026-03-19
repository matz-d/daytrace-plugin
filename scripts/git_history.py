#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
from collections import Counter
from datetime import datetime, time
from pathlib import Path

from common import (
    LOCAL_TZ,
    apply_limit,
    emit,
    error_response,
    isoformat,
    parse_datetime,
    resolve_workspace,
    run_command,
    skipped_response,
    success_response,
    summarize_text,
)


SOURCE_NAME = "git-history"
GIT_TIMEOUT_SEC = 10
LANGUAGE_SUFFIXES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".md": "markdown",
    ".mdx": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".css": "css",
    ".html": "html",
    ".sql": "sql",
}
TEST_PATH_MARKERS = {"test", "tests", "__tests__", "spec", "specs"}
DOC_PATH_MARKERS = {"docs", "doc", "readme", "guide", "guides"}
DESIGN_PATH_MARKERS = {"plan", "plans", "architecture", "schema", "contract", "contracts", "design", "manifest", "proposal", "spec"}
CONFIG_PATH_MARKERS = {"config", "configs", ".github"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Emit git commit history as DayTrace events.")
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


def parse_numstat(record: str, repo_root: Path, workspace: Path) -> dict[str, object] | None:
    lines = [line for line in record.strip().splitlines()]
    if not lines:
        return None

    header = lines[0].split("\x1f")
    if len(header) < 4:
        return None

    commit_hash, authored_at, subject, body = header[:4]
    changed_files = []
    insertions = 0
    deletions = 0

    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        additions_raw, deletions_raw = parts[0], parts[1]
        file_path = "\t".join(parts[2:])

        additions = int(additions_raw) if additions_raw.isdigit() else None
        deletions_count = int(deletions_raw) if deletions_raw.isdigit() else None
        if additions is not None:
            insertions += additions
        if deletions_count is not None:
            deletions += deletions_count

        changed_files.append(
            {
                "path": file_path,
                "additions": additions,
                "deletions": deletions_count,
            }
        )

    return {
        "source": SOURCE_NAME,
        "timestamp": authored_at,
        "type": "commit",
        "summary": summarize_text(subject, 140),
        "details": {
            "commit_hash": commit_hash,
            "workspace": str(workspace),
            "repo_root": str(repo_root),
            "changed_files": changed_files,
            "stats": {
                "files_changed": len(changed_files),
                "insertions": insertions,
                "deletions": deletions,
            },
            "body_summary": summarize_text(body, 240),
        },
        "confidence": "high",
    }


def includes_today_window(start: datetime | None, end: datetime | None, *, now: datetime | None = None) -> bool:
    current = (now or datetime.now().astimezone()).astimezone()
    today_start = datetime.combine(current.date(), time.min, tzinfo=current.tzinfo or LOCAL_TZ)
    today_end = datetime.combine(current.date(), time.max, tzinfo=current.tzinfo or LOCAL_TZ)
    if start and start > today_end:
        return False
    if end and end < today_start:
        return False
    return True


def current_branch(repo_root: Path) -> str | None:
    branch_result = run_command(
        ["git", "-C", str(repo_root), "symbolic-ref", "--quiet", "--short", "HEAD"],
        timeout=GIT_TIMEOUT_SEC,
    )
    if branch_result.returncode == 0:
        value = branch_result.stdout.strip()
        return value or None
    detached_result = run_command(
        ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
        timeout=GIT_TIMEOUT_SEC,
    )
    if detached_result.returncode == 0:
        value = detached_result.stdout.strip()
        return value or None
    return None


def tracked_diff_paths(repo_root: Path, *, pathspec: str, staged: bool) -> list[str] | None:
    command = ["git", "-C", str(repo_root), "diff", "--name-only"]
    if staged:
        command.append("--cached")
    command.extend(["--", pathspec])
    result = run_command(command, timeout=GIT_TIMEOUT_SEC)
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def detect_path_kind(path: str) -> str:
    normalized = Path(path)
    lowered_parts = [part.lower() for part in normalized.parts]
    basename = normalized.name.lower()
    suffix = normalized.suffix.lower()

    if basename.startswith("test_") or basename.endswith("_test.py") or any(marker in lowered_parts for marker in TEST_PATH_MARKERS):
        return "tests"
    if basename == "skill.md" or suffix in {".md", ".mdx", ".rst"} or any(marker in lowered_parts for marker in DOC_PATH_MARKERS):
        return "docs"
    if any(marker in lowered_parts for marker in DESIGN_PATH_MARKERS) or any(marker in basename for marker in DESIGN_PATH_MARKERS):
        return "design"
    if suffix in {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg"} or any(marker in lowered_parts for marker in CONFIG_PATH_MARKERS):
        return "config"
    return "implementation"


def detect_language(path: str) -> str | None:
    suffix = Path(path).suffix.lower()
    return LANGUAGE_SUFFIXES.get(suffix)


def top_dir_key(path: str) -> str:
    parts = Path(path).parts
    if len(parts) >= 2:
        return str(Path(parts[0]) / parts[1])
    if parts:
        return parts[0]
    return "."


def summarize_worktree_paths(paths: list[str]) -> dict[str, object]:
    unique_paths = sorted(dict.fromkeys(path for path in paths if path.strip()))
    kind_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    top_dir_counts: Counter[str] = Counter()
    for path in unique_paths:
        kind_counts[detect_path_kind(path)] += 1
        language = detect_language(path)
        if language:
            language_counts[language] += 1
        top_dir_counts[top_dir_key(path)] += 1
    dominant_kind = None
    if kind_counts:
        dominant_kind = sorted(kind_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return {
        "path_kinds": dict(sorted(kind_counts.items())),
        "dominant_kind": dominant_kind,
        "languages": dict(sorted(language_counts.items())),
        "top_dirs": [
            {"path": path, "count": count}
            for path, count in sorted(top_dir_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        ],
    }


def build_worktree_status_event(repo_root: Path, workspace: Path, pathspec: str) -> dict[str, object] | None:
    staged_files = tracked_diff_paths(repo_root, pathspec=pathspec, staged=True)
    unstaged_files = tracked_diff_paths(repo_root, pathspec=pathspec, staged=False)
    if staged_files is None or unstaged_files is None:
        return None

    branch = current_branch(repo_root)
    dirty = bool(staged_files or unstaged_files)
    if not dirty:
        return None

    all_paths = staged_files + unstaged_files
    path_summary = summarize_worktree_paths(all_paths)
    dominant_kind = str(path_summary.get("dominant_kind") or "changes")
    staged_count = len(staged_files)
    unstaged_count = len(unstaged_files)
    return {
        "source": SOURCE_NAME,
        "timestamp": isoformat(datetime.now().astimezone()),
        "type": "worktree_status",
        "summary": f"Git worktree on {branch or 'detached HEAD'}: {staged_count} staged, {unstaged_count} unstaged ({dominant_kind})",
        "details": {
            "workspace": str(workspace),
            "repo_root": str(repo_root),
            "branch": branch,
            "staged_files": staged_files,
            "unstaged_files": unstaged_files,
            "staged_count": staged_count,
            "unstaged_count": unstaged_count,
            "dirty": dirty,
            **path_summary,
        },
        "confidence": "high",
    }


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
        command = [
            "git",
            "-C",
            str(repo_root),
            "log",
            "--numstat",
            "--date=iso-strict",
            "--format=%x1e%H%x1f%aI%x1f%s%x1f%b",
        ]
        if start:
            command.append(f"--after={start.isoformat()}")
        if end:
            command.append(f"--before={end.isoformat()}")
        command.extend(["--", pathspec])

        result = run_command(command, timeout=GIT_TIMEOUT_SEC)
        if result.returncode != 0:
            emit(error_response(SOURCE_NAME, result.stderr.strip() or "git log failed", workspace=str(workspace)))
            return

        records = [chunk for chunk in result.stdout.split("\x1e") if chunk.strip()]
        events = []
        for record in records:
            event = parse_numstat(record, repo_root, workspace)
            if event:
                events.append(event)
        if includes_today_window(start, end):
            worktree_event = build_worktree_status_event(repo_root, workspace, pathspec)
            if worktree_event is not None:
                events.insert(0, worktree_event)

        emit(
            success_response(
                SOURCE_NAME,
                apply_limit(events, args.limit),
                workspace=str(workspace),
                since=args.since,
                until=args.until,
            )
        )
    except subprocess.TimeoutExpired as exc:
        payload = {"message": f"git command timed out after {exc.timeout}s"}
        if workspace is not None:
            payload["workspace"] = str(workspace)
        emit(error_response(SOURCE_NAME, payload.pop("message"), **payload))
    except Exception as exc:
        emit(error_response(SOURCE_NAME, str(exc)))


if __name__ == "__main__":
    main()
