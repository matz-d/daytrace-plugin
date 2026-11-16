"""Split Claude / Codex conversations into logical packets (work chunks) and summarize them for events."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from common import ensure_datetime, extract_text, summarize_text
from request_text import clean_request_text


RAW_SNIPPET_LIMIT = 100
PRIMARY_INTENT_LIMIT = 300
MAX_USER_HIGHLIGHTS = 8
USER_HIGHLIGHT_LIMIT = 400
MAX_ASSISTANT_HIGHLIGHTS = 3
ASSISTANT_HIGHLIGHT_LIMIT = 180
DEFAULT_GAP_HOURS = 8
HEAD_TAIL_RATIO = 0.7  # fraction of max_items reserved for head; remainder for tail
MAX_WORKFLOW_SIGNAL_ITEMS = 4
TOOL_ERROR_EXCERPT_LIMIT = 160


FAILURE_SIGNAL_PATTERNS = (
    "error",
    "failed",
    "failure",
    "failing",
    "timeout",
    "timed out",
    "exception",
    "permission denied",
    "not found",
    "invalid json",
    "stack trace",
    "bug",
    "broken",
    "修正",
    "失敗",
    "壊れ",
    "不具合",
    "エラー",
)

RETRY_SIGNAL_PATTERNS = (
    "retry",
    "re-run",
    "rerun",
    "run again",
    "try again",
    "again",
    "もう一度",
    "再試行",
    "やり直し",
    "再実行",
)

PIVOT_SIGNAL_PATTERNS = (
    "instead",
    "switch to",
    "rather than",
    "alternative",
    "different approach",
    "change approach",
    "cut over",
    "方針転換",
    "切り替え",
    "別案",
    "代わりに",
    "やっぱり",
)

CODEX_TOOL_RESULT_TYPES = {"function_call_output", "function_result", "tool_result"}

def head_tail_excerpts(
    messages: list[str],
    *,
    limit: int,
    max_items: int,
    head_ratio: float = HEAD_TAIL_RATIO,
) -> list[str]:
    """Select excerpts from head and tail of *messages*, deduplicating.

    This avoids the head-only bias of ``append_excerpt`` loops by reserving
    ``ceil(max_items * head_ratio)`` slots for the first messages and
    the remaining slots for the last messages.  Duplicates (e.g. when
    the message list is short enough to overlap) are silently removed.
    """
    all_excerpts: list[str] = []
    seen: set[str] = set()
    for message in messages:
        excerpt = summarize_text(str(message), limit)
        if excerpt and excerpt not in seen:
            all_excerpts.append(excerpt)
            seen.add(excerpt)

    if len(all_excerpts) <= max_items:
        return all_excerpts

    head_count = max(1, int(max_items * head_ratio + 0.5))
    tail_count = max_items - head_count
    head = all_excerpts[:head_count]
    tail_candidates = all_excerpts[len(all_excerpts) - tail_count:]
    # Remove tail items already in head
    head_set = set(head)
    tail = [item for item in tail_candidates if item not in head_set]
    return head + tail


COMMON_COMMANDS = {
    "rg",
    "sed",
    "git",
    "pytest",
    "uv",
    "npm",
    "pnpm",
    "yarn",
    "cargo",
    "python",
    "python3",
    "bash",
    "zsh",
    "ls",
    "cat",
    "find",
    "grep",
    "make",
}


MATCH_TEXT_NORMALIZATIONS: tuple[tuple[str, str], ...] = (
    ("pull request", "pr"),
    ("findings first", "findings-first"),
    ("findings-first", "findings-first"),
    ("same format", "same-format"),
    ("same findings format", "findings-first"),
    ("file/line refs", "file-line-refs"),
    ("file/line references", "file-line-refs"),
    ("line refs", "file-line-refs"),
    ("line references", "file-line-refs"),
    ("file and line", "file-line-refs"),
    ("root cause", "debug"),
    ("failing", "failure"),
    ("write-up", "write up"),
    ("レポート", "report"),
    ("報告", "report"),
    ("日報", "daily report"),
    ("設定", "config"),
    ("実装", "implement"),
    ("修正", "fix"),
    ("不具合", "bug"),
)


URL_PATTERN = re.compile(r"https?://[^\s<>\"]+")
PATH_PATTERN = re.compile(r"(/[^ \n\t`\"']+)")
WORD_PATTERN = re.compile(r"[A-Za-z0-9_./+-]+|[一-龥ぁ-んァ-ン]+")
COMMAND_ARGS_PATTERN = re.compile(r"<command-args>(.*?)</command-args>", re.IGNORECASE | re.DOTALL)
TAG_PATTERN = re.compile(r"</?([a-z0-9_-]+)(?:\s[^>]*)?>", re.IGNORECASE)


def normalize_match_text(text: str) -> str:
    normalized = text.lower()
    for source, target in MATCH_TEXT_NORMALIZATIONS:
        normalized = normalized.replace(source, target)
    return normalized


def pattern_in_text(text: str, pattern: str) -> bool:
    lowered = text.lower()
    needle = pattern.lower()
    if not needle:
        return False
    if re.search(r"[一-龥ぁ-んァ-ン./-]", needle):
        return needle in lowered
    escaped = re.escape(needle).replace(r"\ ", r"\s+")
    return re.search(rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])", lowered) is not None


def sanitize_url_domain(raw_url: str) -> str:
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return raw_url
    if not parsed.scheme or not parsed.netloc:
        return raw_url
    return f"{parsed.scheme}://{parsed.netloc}"


def mask_paths(text: str, workspace: str | None) -> str:
    masked = text
    if workspace:
        aliases = _workspace_aliases(str(Path(workspace).expanduser()))
        for alias in sorted(aliases, key=len, reverse=True):
            masked = masked.replace(alias, "[WORKSPACE]")
    masked = PATH_PATTERN.sub(_replace_path_token, masked)
    return masked


def _workspace_aliases(workspace: str) -> set[str]:
    aliases = {workspace}
    if workspace.startswith("/private/var/"):
        aliases.add(workspace[len("/private") :])
    elif workspace.startswith("/var/"):
        aliases.add(f"/private{workspace}")
    return aliases


def _replace_path_token(match: re.Match[str]) -> str:
    token = match.group(1)
    if token.startswith("[WORKSPACE]"):
        return token
    if token.startswith(("http://", "https://")):
        return token
    if token.startswith("/Users/") or token.startswith("/home/") or token.startswith("/tmp/") or token.startswith("/var/"):
        suffix = ""
        parts = token.split("/")
        if len(parts) > 3:
            suffix = "/" + "/".join(parts[-2:])
        return f"[PATH]{suffix}"
    return token


def compact_snippet(text: str, workspace: str | None, limit: int = RAW_SNIPPET_LIMIT) -> str:
    sanitized = URL_PATTERN.sub(lambda match: sanitize_url_domain(match.group(0)), text or "")
    sanitized = mask_paths(sanitized, workspace)
    return summarize_text(sanitized, limit)


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _dedupe_texts(values: list[str], *, limit: int | None = None) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _collapse_whitespace(str(value))
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(text)
        if limit is not None and len(deduped) >= limit:
            break
    return deduped


def _line_is_user_wrapper(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    if stripped == "Tool loaded.":
        return True
    if stripped.isdigit():
        return True
    if lowered.startswith("## my request for codex"):
        return True
    if lowered == "# files mentioned by the user:":
        return True
    if re.match(r"^##\s+[^:]+:\s+/.+", stripped):
        return True
    if lowered.startswith("<task-notification>"):
        return True
    if lowered.startswith("<command-name>") or lowered.startswith("<command-message>"):
        return True
    return False


def clean_user_message_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""

    command_args = COMMAND_ARGS_PATTERN.findall(raw)
    command_args_text = " ".join(match.strip() for match in command_args if str(match).strip())
    if command_args:
        raw = COMMAND_ARGS_PATTERN.sub(" ", raw)

    if "## My request for Codex:" in raw:
        raw = raw.split("## My request for Codex:", 1)[1]
    if command_args_text and command_args_text not in raw:
        raw = f"{command_args_text}\n{raw}"

    if "# Files mentioned by the user:" in raw and "## My request for Codex:" not in raw:
        lines: list[str] = []
        for line in raw.splitlines():
            if _line_is_user_wrapper(line):
                continue
            lines.append(line)
        raw = "\n".join(lines)

    raw = re.sub(r"<command-message>.*?</command-message>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    raw = re.sub(r"<command-name>.*?</command-name>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    raw = re.sub(r"<task-notification>.*?</task-notification>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    raw = TAG_PATTERN.sub(" ", raw)

    cleaned_lines = [line for line in raw.splitlines() if not _line_is_user_wrapper(line)]
    cleaned = _collapse_whitespace(" ".join(cleaned_lines))
    return cleaned


_FILE_PATH_PATTERN = re.compile(
    r"(?:^|[\s\"'=:,])(/[\w./-]+(?:\.\w+)?)"
    r"|(?:^|[\s\"'=:,])([\w./-]+(?:\.\w{1,10}))"
)

_COMMON_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb",
    ".java", ".c", ".cpp", ".h", ".hpp", ".sh", ".md", ".json",
    ".yaml", ".yml", ".toml", ".sql", ".html", ".css", ".txt",
    ".cfg", ".conf", ".env", ".lock",
})


def extract_referenced_files(tool_inputs: list[dict[str, Any]], workspace: str | None) -> list[str]:
    """Extract file paths referenced in tool_use.input dicts."""
    files: list[str] = []
    seen: set[str] = set()
    for input_dict in tool_inputs:
        for value in _iter_string_values(input_dict):
            for match in _FILE_PATH_PATTERN.finditer(value):
                path = match.group(1) or match.group(2)
                if not path or path in seen:
                    continue
                # Keep only plausible file references
                suffix = Path(path).suffix.lower()
                if suffix in _COMMON_EXTENSIONS or (path.startswith("/") and len(path) > 2):
                    normalized = path
                    if workspace:
                        workspace_base = workspace.rstrip("/")
                        workspace_prefix = f"{workspace_base}/"
                        if normalized.startswith(workspace_prefix):
                            normalized = normalized[len(workspace_prefix):]
                        elif normalized == workspace_base:
                            normalized = ""
                    if normalized not in seen:
                        seen.add(normalized)
                        files.append(normalized)
    return files[:20]  # cap to prevent bloat


def _iter_string_values(obj: Any) -> list[str]:
    """Recursively extract string values from nested dicts/lists."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        result: list[str] = []
        for v in obj.values():
            result.extend(_iter_string_values(v))
        return result
    if isinstance(obj, list):
        result = []
        for item in obj:
            result.extend(_iter_string_values(item))
        return result
    return []


def extract_known_commands(text: str) -> list[str]:
    commands: list[str] = []
    for raw in re.findall(r"`([^`]+)`", text):
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()
        if tokens:
            commands.append(tokens[0])
    for token in WORD_PATTERN.findall(text):
        lowered = token.lower()
        if lowered in COMMON_COMMANDS:
            commands.append(lowered)
    return commands


def _classify_argument_token(token: str, workspace: str | None) -> str:
    normalized = mask_paths(str(token or "").strip(), workspace)
    lowered = normalized.lower()
    if not lowered:
        return "value"
    if lowered.startswith("-"):
        return f"flag:{lowered}"
    if lowered.startswith(("http://", "https://")):
        return "url"
    if re.fullmatch(r"\d+", lowered):
        return "number"
    if "/" in normalized or Path(normalized).suffix:
        return "path"
    return "value"


def _command_argument_pattern(command: str, workspace: str | None) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return ""
    command_name = str(tokens[0]).lower()
    kinds = [_classify_argument_token(token, workspace) for token in tokens[1:5] if str(token or "").strip()]
    if not kinds:
        return command_name
    return f"{command_name}({','.join(kinds)})"


def _tool_argument_pattern(name: str, raw_input: Any, workspace: str | None) -> str:
    if isinstance(raw_input, dict):
        command = raw_input.get("cmd")
        if isinstance(command, str) and command.strip():
            return _command_argument_pattern(command, workspace)
        keys = [str(key) for key in raw_input.keys() if str(key).strip()]
        if keys:
            return f"{name}<{','.join(sorted(keys)[:4])}>"
        return name
    if isinstance(raw_input, str) and raw_input.strip():
        return _command_argument_pattern(raw_input, workspace)
    return name


def build_tool_call_detail(
    name: str,
    raw_input: Any,
    *,
    timestamp: str | None = None,
    workspace: str | None = None,
    invocation_kind: str | None = None,
    result_status: str | None = None,
    exit_code: Any = None,
    error_excerpt: str | None = None,
) -> dict[str, Any]:
    normalized_name = str(name or "").strip().lower() or "unknown"
    detail: dict[str, Any] = {"name": normalized_name}
    if timestamp:
        detail["timestamp"] = timestamp
    if invocation_kind:
        detail["invocation_kind"] = str(invocation_kind)
    if isinstance(raw_input, dict):
        argument_keys = [str(key) for key in raw_input.keys() if str(key).strip()]
        if argument_keys:
            detail["argument_keys"] = sorted(argument_keys)[:6]
        referenced_files = extract_referenced_files([raw_input], workspace)
        if referenced_files:
            detail["referenced_files"] = referenced_files[:5]
    argument_pattern = _tool_argument_pattern(normalized_name, raw_input, workspace)
    if argument_pattern:
        detail["argument_pattern"] = argument_pattern
    normalized_result_status = str(result_status or "").strip().lower()
    if normalized_result_status in {"success", "error", "unknown"}:
        detail["result_status"] = normalized_result_status
    try:
        normalized_exit_code = int(exit_code) if exit_code not in (None, "") else None
    except (TypeError, ValueError):
        normalized_exit_code = None
    if normalized_exit_code is not None:
        detail["exit_code"] = normalized_exit_code
    normalized_error_excerpt = compact_snippet(str(error_excerpt or "").strip(), workspace, limit=TOOL_ERROR_EXCERPT_LIMIT)
    if normalized_error_excerpt:
        detail["error_excerpt"] = normalized_error_excerpt
    return detail


def _result_status_from_payload(payload: dict[str, Any]) -> str | None:
    explicit_status = str(payload.get("result_status") or "").strip().lower()
    if explicit_status in {"success", "error", "unknown"}:
        return explicit_status

    if payload.get("is_error") is True:
        return "error"
    if payload.get("ok") is True or payload.get("success") is True:
        return "success"

    for key in ("exit_code", "returncode", "return_code"):
        value = payload.get(key)
        try:
            exit_code = int(value)
        except (TypeError, ValueError):
            continue
        return "success" if exit_code == 0 else "error"

    status = str(payload.get("status") or "").strip().lower()
    if status in {"ok", "success", "completed"}:
        return "success"
    if status in {"error", "failed", "failure"}:
        return "error"
    return None


def _exit_code_from_payload(payload: dict[str, Any]) -> int | None:
    for key in ("exit_code", "returncode", "return_code"):
        value = payload.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _error_excerpt_from_payload(payload: dict[str, Any], workspace: str | None) -> str | None:
    for key in ("stderr", "error"):
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("message") or value.get("stderr") or value.get("error")
        if str(value or "").strip():
            return compact_snippet(str(value), workspace, limit=TOOL_ERROR_EXCERPT_LIMIT)
    if _result_status_from_payload(payload) == "error":
        value = payload.get("message")
        if isinstance(value, dict):
            value = value.get("message") or value.get("stderr") or value.get("error")
        if str(value or "").strip():
            return compact_snippet(str(value), workspace, limit=TOOL_ERROR_EXCERPT_LIMIT)
    return None


def codex_tool_result_metadata(payload: dict[str, Any], workspace: str | None) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for candidate in (
        payload,
        payload.get("payload"),
        payload.get("output"),
        payload.get("result"),
        payload.get("response"),
    ):
        if isinstance(candidate, dict):
            candidates.append(candidate)

    metadata: dict[str, Any] = {}
    for candidate in candidates:
        result_status = _result_status_from_payload(candidate)
        if result_status and "result_status" not in metadata:
            metadata["result_status"] = result_status
        exit_code = _exit_code_from_payload(candidate)
        if exit_code is not None and "exit_code" not in metadata:
            metadata["exit_code"] = exit_code
        error_excerpt = _error_excerpt_from_payload(candidate, workspace)
        if error_excerpt and "error_excerpt" not in metadata:
            metadata["error_excerpt"] = error_excerpt

    return metadata


def apply_tool_result_metadata(detail: dict[str, Any], metadata: dict[str, Any], workspace: str | None) -> dict[str, Any]:
    if not isinstance(detail, dict):
        return {}
    updated = dict(detail)
    result_status = str(metadata.get("result_status") or "").strip().lower()
    if result_status in {"success", "error", "unknown"}:
        updated["result_status"] = result_status
    try:
        exit_code = int(metadata.get("exit_code"))
    except (TypeError, ValueError):
        exit_code = None
    if exit_code is not None:
        updated["exit_code"] = exit_code
    error_excerpt = compact_snippet(str(metadata.get("error_excerpt") or "").strip(), workspace, limit=TOOL_ERROR_EXCERPT_LIMIT)
    if error_excerpt:
        updated["error_excerpt"] = error_excerpt
    return updated


def codex_tool_result_call_id(payload: dict[str, Any]) -> str | None:
    for key in ("call_id", "tool_call_id", "function_call_id", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    nested_payload = payload.get("payload")
    if isinstance(nested_payload, dict):
        for key in ("call_id", "tool_call_id", "function_call_id", "id"):
            value = str(nested_payload.get(key) or "").strip()
            if value:
                return value
    return None


def _message_signal_evidence(
    messages: list[str],
    workspace: str | None,
    *,
    patterns: tuple[str, ...],
) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in messages:
        cleaned = clean_user_message_text(raw) or _collapse_whitespace(str(raw))
        if not cleaned:
            continue
        lowered = normalize_match_text(cleaned)
        if not any(pattern_in_text(lowered, pattern) for pattern in patterns):
            continue
        snippet = compact_snippet(cleaned, workspace, limit=PRIMARY_INTENT_LIMIT)
        if not snippet or snippet.lower() in seen:
            continue
        seen.add(snippet.lower())
        evidence.append({"snippet": snippet})
        if len(evidence) >= MAX_WORKFLOW_SIGNAL_ITEMS:
            break
    return evidence


def _explicit_failure_hints(tool_call_details: list[dict[str, Any]], workspace: str | None) -> list[dict[str, str]]:
    hints: list[dict[str, str]] = []
    seen: set[str] = set()
    for detail in tool_call_details:
        if not isinstance(detail, dict):
            continue
        if str(detail.get("result_status") or "").strip().lower() != "error":
            continue
        name = str(detail.get("name") or "tool").strip().lower() or "tool"
        exit_code = detail.get("exit_code")
        error_excerpt = str(detail.get("error_excerpt") or "").strip()
        parts = [f"Explicit tool failure: {name}"]
        if exit_code not in (None, ""):
            parts.append(f"exit={exit_code}")
        if error_excerpt:
            parts.append(error_excerpt)
        snippet = compact_snippet(" / ".join(parts), workspace, limit=PRIMARY_INTENT_LIMIT)
        if not snippet or snippet.lower() in seen:
            continue
        seen.add(snippet.lower())
        hints.append({"snippet": snippet})
        if len(hints) >= MAX_WORKFLOW_SIGNAL_ITEMS:
            break
    return hints


def _explicit_retry_hints(tool_call_details: list[dict[str, Any]], workspace: str | None) -> list[dict[str, str]]:
    hints: list[dict[str, str]] = []
    seen: set[str] = set()
    failed_tools: set[str] = set()
    failed_patterns: set[str] = set()
    for detail in tool_call_details:
        if not isinstance(detail, dict):
            continue
        name = str(detail.get("name") or "").strip().lower()
        argument_pattern = str(detail.get("argument_pattern") or "").strip().lower()
        result_status = str(detail.get("result_status") or "").strip().lower()
        if result_status == "error":
            if name:
                failed_tools.add(name)
            if argument_pattern:
                failed_patterns.add(argument_pattern)
            continue
        retry_label = ""
        if argument_pattern and argument_pattern in failed_patterns:
            retry_label = argument_pattern
        elif name and name in failed_tools:
            retry_label = name
        if not retry_label:
            continue
        snippet = compact_snippet(f"Retry after explicit failure: {retry_label}", workspace, limit=PRIMARY_INTENT_LIMIT)
        if not snippet or snippet.lower() in seen:
            continue
        seen.add(snippet.lower())
        hints.append({"snippet": snippet})
        if len(hints) >= MAX_WORKFLOW_SIGNAL_ITEMS:
            break
    return hints


def _has_meaningful_explicit_results(tool_call_details: list[dict[str, Any]]) -> bool:
    for detail in tool_call_details:
        if not isinstance(detail, dict):
            continue
        result_status = str(detail.get("result_status") or "").strip().lower()
        if result_status in {"success", "error"}:
            return True
        if detail.get("exit_code") not in (None, ""):
            return True
        if str(detail.get("error_excerpt") or "").strip():
            return True
    return False


def infer_workflow_signals(
    user_messages: list[str],
    assistant_messages: list[str],
    tool_call_details: list[dict[str, Any]],
    workspace: str | None,
) -> dict[str, Any]:
    combined_messages = list(user_messages) + list(assistant_messages)
    pivot_hints = _message_signal_evidence(user_messages, workspace, patterns=PIVOT_SIGNAL_PATTERNS)
    has_explicit_results = _has_meaningful_explicit_results(tool_call_details)
    if has_explicit_results:
        failure_hints = _explicit_failure_hints(tool_call_details, workspace)
        retry_hints = _explicit_retry_hints(tool_call_details, workspace)
    else:
        failure_hints = _message_signal_evidence(combined_messages, workspace, patterns=FAILURE_SIGNAL_PATTERNS)
        retry_hints = _message_signal_evidence(combined_messages, workspace, patterns=RETRY_SIGNAL_PATTERNS)

    trace = [str(detail.get("name") or "").strip().lower() for detail in tool_call_details if str(detail.get("name") or "").strip()]
    if trace and not has_explicit_results:
        repeated_tools: list[str] = []
        last_name = ""
        streak = 0
        for name in trace:
            if name == last_name:
                streak += 1
            else:
                if last_name and streak >= 2:
                    repeated_tools.append(last_name)
                last_name = name
                streak = 1
        if last_name and streak >= 2:
            repeated_tools.append(last_name)
        for tool_name in _dedupe_texts(repeated_tools, limit=MAX_WORKFLOW_SIGNAL_ITEMS):
            retry_hints.append({"snippet": f"Repeated tool sequence: {tool_name}"})

    retry_hints = retry_hints[:MAX_WORKFLOW_SIGNAL_ITEMS]
    flags: list[str] = []
    if failure_hints:
        flags.append("failure")
    if retry_hints:
        flags.append("retry")
    if pivot_hints:
        flags.append("pivot")
    return {
        "flags": flags,
        "counts": {
            "failure": len(failure_hints),
            "retry": len(retry_hints),
            "pivot": len(pivot_hints),
        },
        "failure_hints": failure_hints,
        "retry_hints": retry_hints,
        "pivot_hints": pivot_hints,
    }


def build_claude_logical_packets(records: list[dict[str, Any]], gap_hours: int) -> list[dict[str, Any]]:
    logical_packets: list[dict[str, Any]] = []
    packet_records: list[dict[str, Any]] = []
    last_timestamp = None
    last_sidechain = None
    last_cwd = None

    def flush_packet() -> None:
        nonlocal packet_records
        if not packet_records:
            return
        user_messages: list[str] = []
        assistant_messages: list[str] = []
        tools: list[str] = []
        tool_inputs: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        timestamps: list[str] = []
        cwd = None
        session_id = None
        is_sidechain = False
        for record in packet_records:
            timestamps.append(str(record.get("timestamp")))
            cwd = record.get("cwd") or cwd
            session_id = record.get("sessionId") or session_id
            is_sidechain = bool(record.get("isSidechain"))
            message = record.get("message")
            synthetic_user_tool_result = record.get("type") == "user" and claude_message_is_tool_result_only(message)
            text = claude_message_text(record.get("message"))
            if record.get("type") == "user" and not synthetic_user_tool_result:
                text = clean_request_text(text)
                if text:
                    user_messages.append(text)
            elif record.get("type") != "user":
                assistant_messages.append(text)
            if isinstance(message, dict) and isinstance(message.get("content"), list):
                for item in message["content"]:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        name = str(item.get("name") or "").lower()
                        if name:
                            tools.append(name)
                        tool_input = item.get("input")
                        if isinstance(tool_input, dict):
                            tool_inputs.append(tool_input)
                            tool_calls.append(
                                build_tool_call_detail(
                                    name or "tool",
                                    tool_input,
                                    timestamp=str(record.get("timestamp") or ""),
                                    workspace=str(cwd) if cwd else None,
                                    invocation_kind="tool_use",
                                )
                            )
            if not synthetic_user_tool_result:
                tools.extend(extract_known_commands(text))

        packet_cwd = str(cwd) if cwd else None
        referenced_files = extract_referenced_files(tool_inputs, packet_cwd)
        logical_packets.append(
            {
                "started_at": earliest_iso_timestamp(timestamps),
                "ended_at": max(timestamps, key=compare_iso_timestamps, default=None),
                "timestamps": timestamps,
                "cwd": packet_cwd,
                "session_id": str(session_id) if session_id else None,
                "is_sidechain": is_sidechain,
                "user_messages": user_messages,
                "assistant_messages": assistant_messages,
                "tools": tools,
                "tool_calls": tool_calls,
                "referenced_files": referenced_files,
                "message_count": len(user_messages) + len(assistant_messages),
                "user_message_count": len(user_messages),
                "assistant_message_count": len(assistant_messages),
            }
        )
        packet_records = []

    for record in records:
        record_type = record.get("type")
        if record_type not in {"user", "assistant"}:
            continue
        if record.get("isMeta"):
            continue
        timestamp_value = record.get("timestamp")
        current_timestamp = ensure_datetime(timestamp_value)
        if current_timestamp is None:
            continue
        current_sidechain = bool(record.get("isSidechain"))
        current_cwd = record.get("cwd")
        should_split = False
        if packet_records and last_timestamp is not None:
            gap_seconds = current_timestamp.timestamp() - last_timestamp.timestamp()
            if gap_seconds >= gap_hours * 60 * 60:
                should_split = True
        if packet_records and last_sidechain is not None and current_sidechain != last_sidechain:
            should_split = True
        if packet_records and last_cwd is not None and current_cwd != last_cwd:
            should_split = True
        if packet_records and record_type == "user":
            current_text = claude_message_text(record.get("message"))
            if current_text:
                current_signals = infer_workflow_signals(
                    [current_text],
                    [],
                    [],
                    str(current_cwd) if current_cwd else None,
                )
                if "pivot" in current_signals.get("flags", []):
                    should_split = True
        if should_split:
            flush_packet()
        packet_records.append(record)
        last_timestamp = current_timestamp
        last_sidechain = current_sidechain
        last_cwd = current_cwd

    flush_packet()
    return logical_packets


def codex_user_message_text(record: dict[str, Any]) -> str | None:
    """Text the user typed in this Codex record, or None when the record is not a user turn.

    Older rollouts write `event_msg/user_message`. Since 2026-11 Codex writes
    `event_msg/item_completed` with `item.type == "UserMessage"` instead, and
    `~/.codex/history.jsonl` is no longer updated.
    """
    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("type") == "user_message":
        return clean_request_text(str(payload.get("message") or ""))
    if payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "UserMessage":
        return None
    content = item.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    elif isinstance(content, str):
        parts.append(content)
    return clean_request_text("\n".join(parts))


def _empty_codex_packet() -> dict[str, Any]:
    return {
        "timestamps": [],
        "user_messages": [],
        "assistant_messages": [],
        "tools": [],
        "tool_calls": [],
        "pending_tool_calls": [],
        "has_non_user_activity": False,
    }


def build_codex_logical_packets(
    records: list[dict[str, Any]],
    *,
    session_id: str,
    workspace: str | None,
    history_user_messages: list[str] | None = None,
    history_timestamps: list[Any] | None = None,
    session_started_at: Any = None,
    gap_hours: int = DEFAULT_GAP_HOURS,
) -> list[dict[str, Any]]:
    logical_packets: list[dict[str, Any]] = []
    packet: dict[str, Any] = _empty_codex_packet()
    history_messages = [str(message) for message in (history_user_messages or []) if str(message or "").strip()]
    history_timestamps_list = [timestamp for timestamp in (history_timestamps or []) if timestamp not in (None, "")]
    history_used = False
    last_timestamp = None

    def flush_packet() -> None:
        nonlocal packet, history_used
        user_messages = list(packet["user_messages"])
        if not history_used and history_messages:
            merged_user_messages: list[str] = []
            for message in history_messages + user_messages:
                if message and message not in merged_user_messages:
                    merged_user_messages.append(message)
            user_messages = merged_user_messages
            history_used = True
        if not packet["timestamps"] and not user_messages and not packet["assistant_messages"] and not packet["tool_calls"]:
            packet = _empty_codex_packet()
            return
        referenced_files = _dedupe_texts(
            [
                referenced_file
                for detail in packet["tool_calls"]
                if isinstance(detail, dict)
                for referenced_file in detail.get("referenced_files", [])
                if str(referenced_file or "").strip()
            ],
            limit=20,
        )
        started_at_values = list(packet["timestamps"])
        if logical_packets == []:
            if session_started_at not in (None, ""):
                started_at_values.append(session_started_at)
            started_at_values.extend(history_timestamps_list)
        logical_packets.append(
            {
                "started_at": earliest_iso_timestamp(started_at_values),
                "ended_at": max(packet["timestamps"], key=compare_iso_timestamps, default=None),
                "timestamps": list(packet["timestamps"]),
                "cwd": workspace,
                "session_id": session_id,
                "user_messages": user_messages,
                "assistant_messages": list(packet["assistant_messages"]),
                "tools": list(packet["tools"]),
                "tool_calls": [dict(detail) for detail in packet["tool_calls"] if isinstance(detail, dict)],
                "referenced_files": referenced_files,
                "message_count": len(user_messages) + len(packet["assistant_messages"]),
                "user_message_count": len(user_messages),
                "assistant_message_count": len(packet["assistant_messages"]),
            }
        )
        packet = _empty_codex_packet()

    def ensure_packet_boundary(current_timestamp: Any) -> None:
        nonlocal last_timestamp
        if packet["timestamps"] and last_timestamp is not None and current_timestamp is not None:
            gap_seconds = current_timestamp.timestamp() - last_timestamp.timestamp()
            if gap_seconds >= gap_hours * 60 * 60:
                flush_packet()
        if current_timestamp is not None:
            last_timestamp = current_timestamp

    def attach_result_metadata(payload: dict[str, Any]) -> None:
        metadata = codex_tool_result_metadata(payload, workspace)
        call_id = codex_tool_result_call_id(payload)
        if call_id:
            for pending_call_id, detail in reversed(packet["pending_tool_calls"]):
                if pending_call_id == call_id:
                    updated = apply_tool_result_metadata(detail, metadata, workspace)
                    detail.clear()
                    detail.update(updated)
                    return
        for _pending_call_id, detail in reversed(packet["pending_tool_calls"]):
            if str(detail.get("result_status") or "").strip():
                continue
            updated = apply_tool_result_metadata(detail, metadata, workspace)
            detail.clear()
            detail.update(updated)
            return

    for record in records:
        record_type = record.get("type")
        timestamp_value = record.get("timestamp")
        current_timestamp = ensure_datetime(timestamp_value)
        payload = record.get("payload", {})
        payload_type = payload.get("type") if isinstance(payload, dict) else None

        message = codex_user_message_text(record)
        if message is not None:
            ensure_packet_boundary(current_timestamp)
            if not message:
                continue
            if packet["timestamps"] and packet["has_non_user_activity"]:
                flush_packet()
            else:
                pivot_signals = infer_workflow_signals([message], [], [], workspace)
                if packet["timestamps"] and "pivot" in pivot_signals.get("flags", []):
                    flush_packet()
            packet["timestamps"].append(str(timestamp_value or ""))
            packet["user_messages"].append(message)
            continue

        if record_type != "response_item" or not isinstance(payload, dict):
            continue

        if payload_type in CODEX_TOOL_RESULT_TYPES:
            ensure_packet_boundary(current_timestamp)
            if timestamp_value:
                packet["timestamps"].append(str(timestamp_value))
            packet["has_non_user_activity"] = True
            attach_result_metadata(payload)
            continue

        if payload_type == "message" and payload.get("role") == "assistant":
            ensure_packet_boundary(current_timestamp)
            assistant_text = codex_message_text(payload)
            if assistant_text:
                packet["timestamps"].append(str(timestamp_value or ""))
                packet["assistant_messages"].append(assistant_text)
                packet["has_non_user_activity"] = True
            continue

        if payload_type == "function_call":
            ensure_packet_boundary(current_timestamp)
            raw_arguments = payload.get("arguments")
            try:
                parsed_arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError:
                parsed_arguments = {"raw_arguments": raw_arguments}
            if not isinstance(parsed_arguments, dict):
                parsed_arguments = {"raw_arguments": parsed_arguments}
            tool_names = codex_command_names(payload)
            timestamp_str = str(timestamp_value or "")
            for tool_name in tool_names or [str(payload.get("name") or "unknown")]:
                detail = build_tool_call_detail(
                    tool_name,
                    parsed_arguments,
                    timestamp=timestamp_str,
                    workspace=workspace,
                    invocation_kind=str(payload.get("name") or "function_call"),
                )
                packet["tool_calls"].append(detail)
                packet["pending_tool_calls"].append((codex_tool_result_call_id(payload), detail))
            packet["tools"].extend(tool_names or [str(payload.get("name") or "unknown")])
            if timestamp_str:
                packet["timestamps"].append(timestamp_str)
            packet["has_non_user_activity"] = True

    flush_packet()
    return logical_packets


def timestamp_to_epoch(value: Any) -> int:
    try:
        current = ensure_datetime(value)
    except (TypeError, ValueError):
        return 0
    if current is None:
        return 0
    return int(current.timestamp())


def compare_iso_timestamps(value: str | None) -> int:
    return timestamp_to_epoch(value)


def earliest_iso_timestamp(values: list[Any]) -> str | None:
    best: tuple[float, str] | None = None
    for value in values:
        try:
            current = ensure_datetime(value)
        except (TypeError, ValueError):
            continue
        if current is None:
            continue
        candidate = (current.timestamp(), current.isoformat())
        if best is None or candidate[0] < best[0]:
            best = candidate
    return best[1] if best else None


def claude_message_text(message: object) -> str:
    if not isinstance(message, dict):
        return extract_text(message)

    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
            elif item.get("type") == "thinking" and isinstance(item.get("thinking"), str):
                text_parts.append(item["thinking"])
            elif item.get("type") == "tool_use":
                name = item.get("name", "tool")
                text_parts.append(f"{name} tool call")
        return " ".join(part for part in text_parts if part)
    return extract_text(message)


def claude_message_is_tool_result_only(message: object) -> bool:
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    saw_tool_result = False
    for item in content:
        if not isinstance(item, dict):
            return False
        if item.get("type") == "tool_result":
            saw_tool_result = True
            continue
        return False
    return saw_tool_result


def codex_message_text(payload: dict[str, Any]) -> str:
    return extract_text(payload.get("content"))


def codex_command_names(payload: dict[str, Any]) -> list[str]:
    names: list[str] = []
    function_name = str(payload.get("name") or "").strip()
    arguments = payload.get("arguments")
    if function_name and function_name != "exec_command":
        names.append(function_name)
    if function_name == "exec_command":
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else {}
        except json.JSONDecodeError:
            parsed = {}
        cmd = parsed.get("cmd")
        if isinstance(cmd, str):
            try:
                tokens = shlex.split(cmd)
            except ValueError:
                tokens = cmd.split()
            if tokens:
                names.append(tokens[0].lower())
    return names


