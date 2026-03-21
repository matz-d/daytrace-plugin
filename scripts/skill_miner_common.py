from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections import Counter, defaultdict
from difflib import unified_diff
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from common import ensure_datetime, extract_text, is_within_path, summarize_text


CLAUDE_SOURCE = "claude-history"
CODEX_SOURCE = "codex-history"
PREPARE_SOURCE = "skill-miner-prepare"
DETAIL_SOURCE = "skill-miner-detail"
RESEARCH_JUDGE_SOURCE = "skill-miner-research-judge"
PROPOSAL_SOURCE = "skill-miner-proposal"

DEFAULT_DAYTRACE_DIR = Path("~/.daytrace").expanduser()
DEFAULT_DECISION_LOG_PATH = DEFAULT_DAYTRACE_DIR / "skill-miner-decisions.jsonl"
DEFAULT_SKILL_CREATOR_HANDOFF_DIR = DEFAULT_DAYTRACE_DIR / "skill-creator-handoffs"

MAX_SNIPPETS = 2
RAW_SNIPPET_LIMIT = 100
PRIMARY_INTENT_LIMIT = 300
FULL_USER_INTENT_LIMIT = 1200
MAX_INTENT_TRACE_ITEMS = 4
MAX_CONSTRAINT_ITEMS = 4
MAX_ACCEPTANCE_CRITERIA_ITEMS = 4
MAX_BLOCK_KEYS = 12
PRIMARY_INTENT_DIRECTIVE_EXTRAS = 2
MAX_USER_HIGHLIGHTS = 8
USER_HIGHLIGHT_LIMIT = 400
MAX_ASSISTANT_HIGHLIGHTS = 3
ASSISTANT_HIGHLIGHT_LIMIT = 180
DEFAULT_TOP_N = 10
DEFAULT_MAX_UNCLUSTERED = 10
DEFAULT_GAP_HOURS = 8
DEFAULT_RESEARCH_REF_LIMIT = 5
HEAD_TAIL_RATIO = 0.7  # fraction of max_items reserved for head; remainder for tail
MAX_TOOL_TRACE_ITEMS = 20
MAX_TOOL_CALL_EXAMPLES = 8
MAX_TOOL_ARGUMENT_PATTERNS = 8
MAX_WORKFLOW_SIGNAL_ITEMS = 4
TOOL_ERROR_EXCERPT_LIMIT = 160

OVERSIZED_CLUSTER_MIN_PACKETS = 8
OVERSIZED_CLUSTER_MIN_SHARE = 0.5
NEAR_MATCH_DENSE_MIN_COUNT = 2
SKILL_MINER_PACKET_VERSION = 2

PRIMARY_INTENT_SOURCE_RAW = "raw_user_message"
PRIMARY_INTENT_SOURCE_HIGHLIGHT = "user_highlight"
PRIMARY_INTENT_SOURCE_SUMMARY = "summary_fallback"
PRIMARY_INTENT_SOURCES = {
    PRIMARY_INTENT_SOURCE_RAW,
    PRIMARY_INTENT_SOURCE_HIGHLIGHT,
    PRIMARY_INTENT_SOURCE_SUMMARY,
}

TASK_SHAPE_TOOL_HINTS: dict[str, set[str]] = {
    "prepare_report": {"python", "python3", "bash", "read", "rg"},
    "write_markdown": {"bash", "cat", "read", "sed"},
    "debug_failure": {"bash", "pytest", "python", "python3", "rg"},
    "implement_feature": {"bash", "git", "python", "python3", "npm", "pnpm", "cargo", "go"},
    "edit_config": {"bash", "cat", "sed"},
    "run_tests": {"pytest", "python", "python3", "npm", "pnpm", "yarn", "cargo", "go", "make"},
    "review_changes": {"git", "read", "rg", "sed"},
    "summarize_findings": {"read", "sed", "cat"},
    "search_code": {"rg", "grep"},
    "inspect_files": {"cat", "read", "sed", "nl"},
}

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


GENERIC_TASK_SHAPES = {
    "review_changes",
    "search_code",
    "summarize_findings",
    "inspect_files",
}

GENERIC_TOOL_SIGNATURES = {
    "bash",
    "cat",
    "ls",
    "nl",
    "read",
    "rg",
    "sed",
}

VALID_SUGGESTED_KINDS = {"CLAUDE.md", "skill", "hook", "agent"}
# Substrings for intent/label (normalized lower). Two+ distinct lines must match for agent role consistency.
AGENT_ROLE_SUBSTRINGS = (
    "reviewer",
    "persistent role",
    "standing",
    "triage",
    "observer",
    "watchdog",
    "router",
    "coordinator",
    "across tasks",
    "across sessions",
    "always act",
    "レビュー",
    "役割",
    "振る舞い",
    "横断",
)
CONSTRAINT_RULE_NAMES = {"never-do", "confirm-before"}
ACCEPTANCE_RULE_NAMES = {"findings-first", "file-line-refs", "tests-before-close", "format-rule"}
CLAUDE_MD_RULE_NAMES = {
    "findings-first",
    "file-line-refs",
    "tests-before-close",
    "always-do",
    "never-do",
    "format-rule",
    "confirm-before",
}
HOOK_SHAPES = {"run_tests"}
SKILL_SHAPES = {"prepare_report", "write_markdown", "debug_failure", "implement_feature", "edit_config", "review_changes"}
AGENT_SHAPES = {"summarize_findings", "search_code", "inspect_files"}
CONSTRAINT_KEYWORDS = (
    "never",
    "avoid",
    "do not",
    "don't",
    "without",
    "must not",
    "禁止",
    "しないで",
    "やめて",
    "避け",
    "確認してから",
    "先に確認",
)
ACCEPTANCE_KEYWORDS = (
    "include",
    "return",
    "report",
    "list",
    "show",
    "verify",
    "test",
    "format",
    "severity",
    "line",
    "refs",
    "出力",
    "根拠",
    "行番号",
    "重要度",
    "テスト",
    "検証",
)
READY_BLOCKING_FLAGS = {"oversized_cluster", "split_recommended", "weak_semantic_cohesion", "near_match_dense"}
READY_BLOCKING_FLAG_LABELS = {
    "oversized_cluster": "大きなグループだが一貫したパターンとして説明可能",
    "split_recommended": "分割せず 1 つのパターンとして扱える",
    "weak_semantic_cohesion": "意味的なまとまりがあることを確認",
    "near_match_dense": "類似パターンとの重複なし",
}

BROAD_LABEL_TASK_SHAPES = {"prepare_report", "write_markdown", "search_code", "inspect_files", "summarize_findings"}

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

TASK_SHAPE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("prepare_report", ("daily report", "prepare report", "weekly report", "status report", "報告書", "レポート", "日報", "報告")),
    ("write_markdown", ("markdown", ".md", "readme", "draft", "記事", "ブログ", "write up")),
    ("debug_failure", ("root cause", "debug", "fix", "error", "bug", "failure", "failing", "修正", "不具合")),
    ("implement_feature", ("implement", "feature", "add", "build", "create", "ship", "実装", "追加")),
    ("edit_config", (".env", "config", "settings", "設定", "yaml", "json", "toml")),
    ("run_tests", ("pytest", "unit test", "integration test", "test", "tests", "spec", "検証")),
    ("review_changes", ("review", "findings", "pr", "diff", "指摘", "レビュー")),
    ("summarize_findings", ("findings", "severity", "summary", "要約", "まとめ")),
    ("search_code", ("rg", "grep", "search", "検索")),
    ("inspect_files", ("inspect", "read", "file", "確認", "読む")),
]

ARTIFACT_HINT_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("claude-md", ("claude.md", "repo rule", "suggested rule", "local rule")),
    ("review", ("review", "findings", "pr", "指摘", "レビュー")),
    ("markdown", ("markdown", ".md", "readme", "記事", "日報")),
    ("report", ("report", "daily", "weekly", "summary", "レポート", "報告")),
    ("config", ("config", ".env", "yaml", "json", "設定")),
    ("code", ("python", "ts", "tsx", "js", "実装", "コード")),
]

USER_REPEATED_RULE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
        (
            "findings-first",
            (
                "findings-first",
                "return findings first",
                "report findings first",
                "list findings by severity",
                "findings by severity",
                "findings in severity order",
                "summarize by severity",
                "with severity ordering",
                "severity ordering",
                "severity ordered findings",
                "指摘を先に",
                "指摘をseverity順",
                "重要度順で指摘",
            "同じ findings-first format",
        ),
    ),
    (
        "file-line-refs",
        (
            "file-line-refs",
            "file/line refs",
            "file/line references",
            "ファイル名と行番号",
            "行番号付き",
            "行番号とファイル名",
        ),
    ),
    (
        "concise-updates",
        (
            "1-2 sentence",
            "1-2 sentences",
            "one or two sentence",
            "keep updates concise",
            "brief updates",
            "short updates",
            "same concise format",
            "簡潔に更新",
            "簡潔な形式",
            "短い更新",
        ),
    ),
    (
        "tests-before-close",
        (
            "run tests before",
            "test before close",
            "verify before finish",
            "pytest before",
            "テストしてから",
            "検証してから",
            "先にテスト",
        ),
    ),
    (
        "always-do",
        (
            "always",
            "always do",
            "every time",
            "必ず",
            "毎回",
        ),
    ),
    (
        "never-do",
        (
            "never",
            "never do",
            "絶対に",
            "禁止",
            "やめて",
            "しないで",
        ),
    ),
    (
        "format-rule",
        (
            "same-format",
            "same template",
            "template通り",
            "テンプレート通り",
            "同じ形式",
            "同じフォーマット",
            "format rule",
        ),
    ),
    (
        "confirm-before",
        (
            "confirm before",
            "ask before",
            "check with me before",
            "確認してから",
            "先に確認",
            "聞いてから",
            "相談してから",
        ),
    ),
]

ASSISTANT_REPEATED_RULE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
        (
            "findings-first",
            (
                "findings-first",
                "findings by severity",
                "list findings by severity",
                "return findings first",
                "report findings first",
                "summarize by severity",
                "with severity ordering",
                "severity ordering",
                "severity ordered findings",
                "指摘を先に",
                "重要度順で指摘",
            ),
    ),
    (
        "file-line-refs",
        (
            "file-line-refs",
            "file/line refs",
            "file/line references",
            "ファイル名と行番号",
            "行番号付き",
        ),
    ),
    (
        "concise-updates",
        (
            "1-2 sentence",
            "1-2 sentences",
            "same concise format",
            "keep updates concise",
            "brief updates",
            "簡潔な形式",
        ),
    ),
    (
        "tests-before-close",
        (
            "run tests before",
            "test before close",
            "verify before finish",
            "pytest before",
            "テストしてから",
            "検証してから",
        ),
    ),
]

DIRECTIVE_RULE_NAMES = {label for label, _patterns in USER_REPEATED_RULE_PATTERNS}

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

TOKEN_SYNONYMS: dict[str, str] = {
    "summarise": "summarize",
    "summary": "report",
    "reporting": "report",
    "reports": "report",
    "reviewing": "review",
    "reviews": "review",
    "findings": "finding",
    "tests": "test",
    "testing": "test",
    "configs": "config",
    "settings": "config",
    "implemented": "implement",
    "implementing": "implement",
    "fixes": "fix",
    "fixed": "fix",
    "debugging": "debug",
}

DAYTRACE_RULES_SECTION = "## DayTrace Suggested Rules"
RULE_BULLET_PREFIX = "- "
CLAUDE_MD_FILENAME = "CLAUDE.md"

URL_PATTERN = re.compile(r"https?://[^\s<>\"]+")
PATH_PATTERN = re.compile(r"(/[^ \n\t`\"']+)")
WORD_PATTERN = re.compile(r"[A-Za-z0-9_./+-]+|[一-龥ぁ-んァ-ン]+")
COMMAND_ARGS_PATTERN = re.compile(r"<command-args>(.*?)</command-args>", re.IGNORECASE | re.DOTALL)
TAG_PATTERN = re.compile(r"</?([a-z0-9_-]+)(?:\s[^>]*)?>", re.IGNORECASE)
DIRECTIVE_ENGLISH_PATTERNS = (
    r"\bplease\b",
    r"\b(can|could|would) you\b",
    r"\bneed you to\b",
    r"\bi want you to\b",
    r"\bkeep\b",
    r"\breturn\b",
    r"\blist\b",
    r"\binclude\b",
    r"\bavoid\b",
    r"\buse\b",
    r"\bdo not\b",
    r"\bdon't\b",
    r"\bnever\b",
    r"\balways\b",
    r"\bconfirm before\b",
    r"\bask before\b",
    r"\bcheck with me before\b",
)
DIRECTIVE_JAPANESE_PATTERNS = (
    "してください",
    "して下さい",
    "してほしい",
    "して欲しい",
    "してもらいたい",
    "してから",
    "しないで",
    "やめて",
    "必ず",
    "絶対に",
    "確認してから",
    "先に確認",
    "聞いてから",
    "相談してから",
)


def normalize_match_text(text: str) -> str:
    normalized = text.lower()
    for source, target in MATCH_TEXT_NORMALIZATIONS:
        normalized = normalized.replace(source, target)
    return normalized


def normalized_directive_label(text: str) -> str:
    return normalize_match_text(text).strip(" \t\r\n-*_`'\"[](){}:;,.!?")


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
    parsed = urlsplit(raw_url)
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


def _token_overlap_ratio(left: str, right: str) -> float:
    left_tokens = tokenize(left)
    right_tokens = tokenize(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))


def _candidate_text_list(candidate: dict[str, Any], key: str, *, limit: int) -> list[str]:
    raw = candidate.get(key)
    if not isinstance(raw, list):
        return []
    return _dedupe_texts([str(item) for item in raw if str(item).strip()], limit=limit)


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


def _message_priority(text: str) -> tuple[int, int, int]:
    tokens = tokenize(text)
    token_count = len(tokens)
    task_shapes = infer_task_shapes([text], [])
    artifact_hints = infer_artifact_hints([text], [])
    has_specific_shape = any(shape not in GENERIC_TASK_SHAPES for shape in task_shapes)
    directive_like = is_directive_like_user_message(text)
    return (
        1 if has_specific_shape else 0,
        1 if directive_like else 0,
        len(task_shapes),
        len(artifact_hints) + token_count,
    )


def build_intent_trace(
    user_messages: list[str],
    assistant_messages: list[str],
    workspace: str | None,
) -> list[str]:
    cleaned_user_messages = [
        compact_snippet(cleaned, workspace, limit=PRIMARY_INTENT_LIMIT)
        for cleaned in (clean_user_message_text(message) for message in user_messages)
        if cleaned
    ]
    if cleaned_user_messages:
        return _dedupe_texts(cleaned_user_messages, limit=MAX_INTENT_TRACE_ITEMS)

    assistant_fallbacks = [
        compact_snippet(_collapse_whitespace(str(message)), workspace, limit=PRIMARY_INTENT_LIMIT)
        for message in assistant_messages
        if _collapse_whitespace(str(message))
    ]
    return _dedupe_texts(assistant_fallbacks, limit=MAX_INTENT_TRACE_ITEMS)


def _directive_kind(text: str, workspace: str | None) -> str | None:
    directive_label = normalized_directive_label(text)
    if directive_label in CONSTRAINT_RULE_NAMES:
        return "constraint"
    if directive_label in ACCEPTANCE_RULE_NAMES:
        return "acceptance"
    normalized_rules = [
        str(item.get("normalized") or "")
        for item in infer_rule_hints([text], workspace, role="user")
        if item.get("normalized")
    ]
    if any(rule in CONSTRAINT_RULE_NAMES for rule in normalized_rules):
        return "constraint"
    if any(rule in ACCEPTANCE_RULE_NAMES for rule in normalized_rules):
        return "acceptance"

    lowered = normalize_match_text(text)
    if any(pattern_in_text(lowered, keyword) for keyword in CONSTRAINT_KEYWORDS):
        return "constraint"
    if any(pattern_in_text(lowered, keyword) for keyword in ACCEPTANCE_KEYWORDS):
        return "acceptance"
    return None


def build_constraints(user_messages: list[str], workspace: str | None) -> list[str]:
    constraints: list[str] = []
    for message in user_messages:
        cleaned = clean_user_message_text(message)
        if not cleaned or not is_directive_like_user_message(cleaned):
            continue
        if _directive_kind(cleaned, workspace) != "constraint":
            continue
        directive_label = normalized_directive_label(cleaned)
        if directive_label in DIRECTIVE_RULE_NAMES:
            constraints.append(directive_label)
        else:
            constraints.append(compact_snippet(cleaned, workspace, limit=PRIMARY_INTENT_LIMIT))
    return _dedupe_texts(constraints, limit=MAX_CONSTRAINT_ITEMS)


def build_acceptance_criteria(user_messages: list[str], workspace: str | None) -> list[str]:
    criteria: list[str] = []
    for message in user_messages:
        cleaned = clean_user_message_text(message)
        if not cleaned or not is_directive_like_user_message(cleaned):
            continue
        if _directive_kind(cleaned, workspace) != "acceptance":
            continue
        directive_label = normalized_directive_label(cleaned)
        if directive_label in DIRECTIVE_RULE_NAMES:
            criteria.append(directive_label)
        else:
            criteria.append(compact_snippet(cleaned, workspace, limit=PRIMARY_INTENT_LIMIT))
    return _dedupe_texts(criteria, limit=MAX_ACCEPTANCE_CRITERIA_ITEMS)


def build_primary_intent_fields(
    user_messages: list[str],
    assistant_messages: list[str],
    workspace: str | None,
    *,
    user_message_source: str = PRIMARY_INTENT_SOURCE_RAW,
) -> tuple[str, str, str]:
    cleaned_candidates: list[tuple[tuple[int, int, int], int, int, str]] = []
    cleaned_fallbacks: list[tuple[int, int, str]] = []
    intent_trace = build_intent_trace(user_messages, assistant_messages, workspace)
    directive_extras: list[str] = []
    chronological_cleaned: list[str] = []

    for index, message in enumerate(user_messages):
        cleaned = clean_user_message_text(message)
        if not cleaned:
            continue
        chronological_cleaned.append(cleaned)
        token_count = len(tokenize(cleaned))
        if token_count >= 4 or is_directive_like_user_message(cleaned):
            cleaned_candidates.append((_message_priority(cleaned), index, len(cleaned), cleaned))
        else:
            cleaned_fallbacks.append((index, len(cleaned), cleaned))
        if is_directive_like_user_message(cleaned):
            directive_extras.append(cleaned)

    if cleaned_candidates:
        _priority, _index, _length, selected = max(
            cleaned_candidates,
            key=lambda item: (item[0], item[1], item[2]),
        )
        if _directive_kind(selected, workspace) is not None:
            for candidate_text in chronological_cleaned:
                if candidate_text == selected:
                    continue
                if _directive_kind(candidate_text, workspace) is None:
                    selected = candidate_text
                    break
        primary_parts = [selected]
        for directive in _dedupe_texts(directive_extras):
            if normalize_match_text(directive) == normalize_match_text(selected):
                continue
            if _token_overlap_ratio(selected, directive) >= 0.75:
                continue
            primary_parts.append(directive)
            if len(primary_parts) >= 1 + PRIMARY_INTENT_DIRECTIVE_EXTRAS:
                break
        return (
            compact_snippet("; ".join(primary_parts), workspace, limit=PRIMARY_INTENT_LIMIT),
            compact_snippet(" | ".join(intent_trace or [selected]), workspace, limit=FULL_USER_INTENT_LIMIT),
            user_message_source,
        )

    if cleaned_fallbacks:
        _index, _length, selected = max(cleaned_fallbacks, key=lambda item: (item[0], item[1]))
        return (
            compact_snippet(selected, workspace, limit=PRIMARY_INTENT_LIMIT),
            compact_snippet(" | ".join(intent_trace or [selected]), workspace, limit=FULL_USER_INTENT_LIMIT),
            user_message_source,
        )

    fallback_texts = user_messages + assistant_messages
    for message in fallback_texts:
        snippet = compact_snippet(str(message), workspace, limit=PRIMARY_INTENT_LIMIT)
        if snippet:
            return snippet, compact_snippet(str(message), workspace, limit=FULL_USER_INTENT_LIMIT), PRIMARY_INTENT_SOURCE_SUMMARY

    return "No primary intent captured", "No primary intent captured", PRIMARY_INTENT_SOURCE_SUMMARY


def feature_messages_for_packet(
    user_messages: list[str],
    assistant_messages: list[str],
) -> tuple[list[str], str]:
    normalized_user_messages = [message for message in (clean_user_message_text(raw) for raw in user_messages) if message]
    if normalized_user_messages:
        return normalized_user_messages, "user"
    normalized_assistant_messages = [_collapse_whitespace(str(message)) for message in assistant_messages if _collapse_whitespace(str(message))]
    return normalized_assistant_messages, "assistant_fallback"


def is_directive_like_user_message(text: str) -> bool:
    candidate = _collapse_whitespace(clean_user_message_text(text))
    if not candidate:
        return False
    lowered = candidate.lower()
    if any(re.search(pattern, lowered) for pattern in DIRECTIVE_ENGLISH_PATTERNS):
        return True
    if any(pattern in candidate for pattern in DIRECTIVE_JAPANESE_PATTERNS):
        return True
    if normalized_directive_label(candidate) in DIRECTIVE_RULE_NAMES:
        return True
    return False


def tokenize(value: str) -> set[str]:
    lowered = normalize_match_text(value)
    tokens = {TOKEN_SYNONYMS.get(token, token) for token in WORD_PATTERN.findall(lowered) if len(token) > 1}
    return {token for token in tokens if len(token) > 1}


def jaccard_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def overlap_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(len(left), len(right))


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


def _tool_trace_from_details(tool_call_details: list[dict[str, Any]], fallback_tools: list[str]) -> list[str]:
    trace = [str(detail.get("name") or "").strip().lower() for detail in tool_call_details if str(detail.get("name") or "").strip()]
    if not trace:
        trace = [str(tool).strip().lower() for tool in fallback_tools if str(tool or "").strip()]
    return trace[:MAX_TOOL_TRACE_ITEMS]


def _tool_argument_patterns(tool_call_details: list[dict[str, Any]]) -> list[str]:
    patterns = [
        str(detail.get("argument_pattern") or "").strip()
        for detail in tool_call_details
        if str(detail.get("argument_pattern") or "").strip()
    ]
    return _dedupe_texts(patterns, limit=MAX_TOOL_ARGUMENT_PATTERNS)


def _tool_call_examples(tool_call_details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    seen: set[str] = set()
    for detail in tool_call_details:
        if not isinstance(detail, dict):
            continue
        example = {
            key: value
            for key, value in detail.items()
            if key in {
                "name",
                "invocation_kind",
                "argument_keys",
                "argument_pattern",
                "referenced_files",
                "result_status",
                "exit_code",
                "error_excerpt",
            }
            and value not in (None, [], "")
        }
        if not example:
            continue
        key = json.dumps(example, ensure_ascii=True, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        examples.append(example)
        if len(examples) >= MAX_TOOL_CALL_EXAMPLES:
            break
    return examples


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


def infer_intent_tool_alignment(task_shapes: list[str], tool_signature: list[str]) -> dict[str, Any]:
    normalized_shapes = [str(shape) for shape in task_shapes if str(shape or "").strip()]
    normalized_tools = [str(tool).strip().lower() for tool in tool_signature if str(tool or "").strip()]
    expected_tools: set[str] = set()
    for shape in normalized_shapes:
        expected_tools.update(TASK_SHAPE_TOOL_HINTS.get(shape, set()))
    if not normalized_shapes or not normalized_tools or not expected_tools:
        return {
            "status": "unknown",
            "matched_tools": [],
            "expected_tools": sorted(expected_tools)[:5],
            "reason": "insufficient_signal",
        }
    matched_tools = sorted(set(normalized_tools) & expected_tools)
    if matched_tools:
        status = "aligned" if any(shape not in GENERIC_TASK_SHAPES for shape in normalized_shapes) else "indirect"
        return {
            "status": status,
            "matched_tools": matched_tools,
            "expected_tools": sorted(expected_tools)[:5],
            "reason": "shape_tool_overlap",
        }
    if set(normalized_tools) <= GENERIC_TOOL_SIGNATURES:
        return {
            "status": "indirect",
            "matched_tools": [],
            "expected_tools": sorted(expected_tools)[:5],
            "reason": "generic_tools_only",
        }
    return {
        "status": "mismatch",
        "matched_tools": [],
        "expected_tools": sorted(expected_tools)[:5],
        "reason": "no_shape_tool_overlap",
    }


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
    packet: dict[str, Any] = {
        "timestamps": [],
        "user_messages": [],
        "assistant_messages": [],
        "tools": [],
        "tool_calls": [],
        "pending_tool_calls": [],
        "has_non_user_activity": False,
    }
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
            packet = {
                "timestamps": [],
                "user_messages": [],
                "assistant_messages": [],
                "tools": [],
                "tool_calls": [],
                "pending_tool_calls": [],
                "has_non_user_activity": False,
            }
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
        packet = {
            "timestamps": [],
            "user_messages": [],
            "assistant_messages": [],
            "tools": [],
            "tool_calls": [],
            "pending_tool_calls": [],
            "has_non_user_activity": False,
        }

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

        if record_type == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message":
            ensure_packet_boundary(current_timestamp)
            message = str(payload.get("message") or "")
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


def infer_task_shapes(texts: list[str], tools: list[str]) -> list[str]:
    corpus = normalize_match_text(" ".join(texts + tools))
    specific: list[str] = []
    generic: list[str] = []
    for label, patterns in TASK_SHAPE_PATTERNS:
        if any(pattern_in_text(corpus, pattern) for pattern in patterns):
            if label in GENERIC_TASK_SHAPES:
                generic.append(label)
            else:
                specific.append(label)
    if specific:
        return (specific[:3] + generic[: max(0, 3 - len(specific))])[:3]
    return generic[:3]


def infer_artifact_hints(texts: list[str], tools: list[str]) -> list[str]:
    corpus = normalize_match_text(" ".join(texts + tools))
    hints: list[str] = []
    for label, patterns in ARTIFACT_HINT_PATTERNS:
        if any(pattern_in_text(corpus, pattern) for pattern in patterns):
            hints.append(label)
    return hints[:3]


def _infer_rule_items(
    texts: list[str],
    workspace: str | None,
    *,
    role: str,
    min_distinct_messages: int,
) -> list[dict[str, str]]:
    patterns = USER_REPEATED_RULE_PATTERNS if role == "user" else ASSISTANT_REPEATED_RULE_PATTERNS
    matched_messages: dict[str, set[str]] = defaultdict(set)
    snippets: dict[str, str] = {}

    for text in texts:
        candidate_text = clean_user_message_text(text) if role == "user" else _collapse_whitespace(str(text))
        if not candidate_text:
            continue
        if role == "user" and not is_directive_like_user_message(candidate_text):
            continue
        lowered = normalize_match_text(candidate_text)
        directive_label = normalized_directive_label(candidate_text)
        for label, rule_patterns in patterns:
            if directive_label == label or any(pattern_in_text(lowered, pattern) for pattern in rule_patterns):
                matched_messages[label].add(candidate_text)
                snippets.setdefault(
                    label,
                    label if directive_label == label else compact_snippet(candidate_text, workspace, limit=PRIMARY_INTENT_LIMIT),
                )

    rules: list[dict[str, str]] = []
    for label, _patterns in patterns:
        if len(matched_messages.get(label, set())) < min_distinct_messages:
            continue
        rules.append({"normalized": label, "raw_snippet": snippets.get(label, label)})
    return rules[:8]


def infer_rule_hints(
    texts: list[str],
    workspace: str | None,
    *,
    role: str,
) -> list[dict[str, str]]:
    return _infer_rule_items(
        texts,
        workspace,
        role=role,
        min_distinct_messages=1,
    )


def infer_repeated_rules(
    texts: list[str],
    workspace: str | None,
    *,
    role: str,
) -> list[dict[str, str]]:
    return _infer_rule_items(
        texts,
        workspace,
        role=role,
        min_distinct_messages=2,
    )


def most_common_tool(tools: list[str]) -> tuple[str, list[str], int]:
    if not tools:
        return "none", [], 0
    counts = Counter(tool for tool in tools if tool)
    ordered = [name for name, _count in counts.most_common()]
    top_tool = ordered[0] if ordered else "none"
    return top_tool, ordered[:5], sum(counts.values())


def normalize_primary_intent(
    messages: list[str],
    workspace: str | None,
    *,
    assistant_messages: list[str] | None = None,
    user_message_source: str = PRIMARY_INTENT_SOURCE_RAW,
) -> str:
    primary_intent, _full_user_intent, _intent_source = build_primary_intent_fields(
        messages,
        assistant_messages or [],
        workspace,
        user_message_source=user_message_source,
    )
    return primary_intent


def append_unique_snippet(bucket: list[str], text: str, workspace: str | None) -> None:
    snippet = compact_snippet(text, workspace)
    if snippet and snippet not in bucket and len(bucket) < MAX_SNIPPETS:
        bucket.append(snippet)


def packet_user_repeated_rules(packet: dict[str, Any]) -> list[dict[str, str]]:
    rules = packet.get("user_repeated_rules")
    if not isinstance(rules, list):
        rules = packet.get("repeated_rules") or []
    return [item for item in rules if isinstance(item, dict)]


def packet_user_rule_hints(packet: dict[str, Any]) -> list[dict[str, str]]:
    rules = packet.get("user_rule_hints")
    if not isinstance(rules, list):
        rules = packet.get("user_repeated_rules")
    if not isinstance(rules, list):
        rules = packet.get("repeated_rules") or []
    return [item for item in rules if isinstance(item, dict)]


def packet_assistant_repeated_rules(packet: dict[str, Any]) -> list[dict[str, str]]:
    rules = packet.get("assistant_repeated_rules")
    if not isinstance(rules, list):
        return []
    return [item for item in rules if isinstance(item, dict)]


def packet_assistant_rule_hints(packet: dict[str, Any]) -> list[dict[str, str]]:
    rules = packet.get("assistant_rule_hints")
    if not isinstance(rules, list):
        rules = packet.get("assistant_repeated_rules")
    if not isinstance(rules, list):
        return []
    return [item for item in rules if isinstance(item, dict)]


def skill_miner_packet_is_v2(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        packet_version = int(value.get("packet_version"))
    except (TypeError, ValueError):
        return False
    if packet_version != SKILL_MINER_PACKET_VERSION:
        return False
    required_strings = (
        "packet_id",
        "session_ref",
        "source",
        "timestamp",
        "primary_intent",
        "full_user_intent",
        "primary_intent_source",
    )
    for key in required_strings:
        if not str(value.get(key) or "").strip():
            return False
    if str(value.get("primary_intent_source") or "") not in PRIMARY_INTENT_SOURCES:
        return False
    required_lists = (
        "task_shape",
        "artifact_hints",
        "tool_signature",
        "representative_snippets",
        "user_rule_hints",
        "assistant_rule_hints",
        "user_repeated_rules",
        "assistant_repeated_rules",
    )
    for key in required_lists:
        if not isinstance(value.get(key), list):
            return False
    support = value.get("support")
    if not isinstance(support, dict):
        return False
    for key in ("message_count", "tool_call_count"):
        try:
            count = int(support.get(key))
        except (TypeError, ValueError):
            return False
        if count < 0:
            return False
    return True


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


def build_claude_session_ref(file_path: str, packet_start: Any) -> str:
    return f"claude:{file_path}:{timestamp_to_epoch(packet_start)}"


def build_codex_session_ref(session_id: str, packet_start: Any) -> str:
    return f"codex:{session_id}:{timestamp_to_epoch(packet_start)}"


def parse_session_ref(value: str) -> tuple[str, str, int]:
    if value.startswith("claude:"):
        remainder = value[len("claude:") :]
        path, _, epoch = remainder.rpartition(":")
        if not path or not epoch:
            raise ValueError(f"Invalid Claude session_ref: {value}")
        return "claude", path, int(epoch)
    if value.startswith("codex:"):
        remainder = value[len("codex:") :]
        session_id, _, epoch = remainder.rpartition(":")
        if not session_id or not epoch:
            raise ValueError(f"Invalid Codex session_ref: {value}")
        return "codex", session_id, int(epoch)
    raise ValueError(f"Unknown session_ref prefix: {value}")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


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


def build_packet(
    *,
    packet_id: str,
    source: str,
    session_ref: str,
    session_id: str | None,
    workspace: str | None,
    timestamp: str | None,
    user_messages: list[str],
    assistant_messages: list[str],
    tools: list[str],
    tool_call_details: list[dict[str, Any]] | None = None,
    referenced_files: list[str] | None = None,
    user_message_source: str = PRIMARY_INTENT_SOURCE_RAW,
    is_sidechain: bool | None = None,
) -> dict[str, Any]:
    feature_texts, feature_source = feature_messages_for_packet(user_messages, assistant_messages)
    normalized_tool_calls = [detail for detail in (tool_call_details or []) if isinstance(detail, dict)]
    tool_trace = _tool_trace_from_details(normalized_tool_calls, tools)
    top_tool, tool_signature, tool_call_count = most_common_tool(tool_trace)
    tool_argument_patterns = _tool_argument_patterns(normalized_tool_calls)
    tool_call_examples = _tool_call_examples(normalized_tool_calls)
    snippets: list[str] = []
    for message in feature_texts:
        append_unique_snippet(snippets, message, workspace)
    intent_trace = build_intent_trace(user_messages, assistant_messages, workspace)
    constraints = build_constraints(user_messages, workspace)
    acceptance_criteria = build_acceptance_criteria(user_messages, workspace)
    task_shape = infer_task_shapes(feature_texts, tool_trace)
    artifact_hints = infer_artifact_hints(feature_texts, tool_trace)
    primary_intent, full_user_intent, primary_intent_source = build_primary_intent_fields(
        user_messages,
        assistant_messages,
        workspace,
        user_message_source=user_message_source,
    )
    user_rule_hints = infer_rule_hints(user_messages, workspace, role="user")
    assistant_rule_hints = infer_rule_hints(assistant_messages, workspace, role="assistant")
    user_repeated_rules = infer_repeated_rules(user_messages, workspace, role="user")
    assistant_repeated_rules = infer_repeated_rules(assistant_messages, workspace, role="assistant")
    intent_tool_alignment = infer_intent_tool_alignment(task_shape, tool_signature)
    workflow_signals = infer_workflow_signals(user_messages, assistant_messages, normalized_tool_calls, workspace)
    contamination_signals: list[str] = []
    if is_sidechain:
        contamination_signals.append("sidechain")
    if feature_source == "assistant_fallback":
        contamination_signals.append("assistant_fallback")
    if primary_intent_source == PRIMARY_INTENT_SOURCE_SUMMARY:
        contamination_signals.append("summary_fallback")
    contamination_signals = _dedupe_texts(contamination_signals, limit=4)
    origin_hint = "human"
    if "sidechain" in contamination_signals:
        origin_hint = "parent_ai"
    elif contamination_signals:
        origin_hint = "unknown"
    user_signal_strength = "high"
    if "assistant_fallback" in contamination_signals or "summary_fallback" in contamination_signals:
        user_signal_strength = "low"
    elif primary_intent_source == PRIMARY_INTENT_SOURCE_HIGHLIGHT:
        user_signal_strength = "medium"
    packet = {
        "packet_version": SKILL_MINER_PACKET_VERSION,
        "packet_id": packet_id,
        "source": source,
        "session_ref": session_ref,
        "session_id": session_id,
        "workspace": workspace,
        "timestamp": timestamp,
        "top_tool": top_tool,
        "tool_signature": tool_signature,
        "tool_trace": tool_trace,
        "tool_argument_patterns": tool_argument_patterns,
        "tool_call_examples": tool_call_examples,
        "referenced_files": referenced_files or [],
        "task_shape": task_shape,
        "artifact_hints": artifact_hints,
        "primary_intent": primary_intent,
        "full_user_intent": full_user_intent,
        "primary_intent_source": primary_intent_source,
        "intent_trace": intent_trace,
        "constraints": constraints,
        "acceptance_criteria": acceptance_criteria,
        "intent_tool_alignment": intent_tool_alignment,
        "workflow_signals": workflow_signals,
        "origin_hint": origin_hint,
        "contamination_signals": contamination_signals,
        "user_signal_strength": user_signal_strength,
        "representative_snippets": snippets,
        "user_rule_hints": user_rule_hints,
        "assistant_rule_hints": assistant_rule_hints,
        "user_repeated_rules": user_repeated_rules,
        "assistant_repeated_rules": assistant_repeated_rules,
        "repeated_rules": list(user_repeated_rules),
        "support": {
            "message_count": len(user_messages) + len(assistant_messages),
            "tool_call_count": tool_call_count,
        },
    }
    if is_sidechain is not None:
        packet["is_sidechain"] = bool(is_sidechain)
    return packet


def candidate_label(packet: dict[str, Any]) -> str:
    task_shapes = packet.get("common_task_shapes") or packet.get("task_shape") or []
    artifact_hints = packet.get("artifact_hints") or []
    rule_hints = packet.get("rule_hints") or []
    intent = str(packet.get("primary_intent") or "").strip()
    if task_shapes:
        base_shape = str(task_shapes[0])
        base = base_shape.replace("_", " ")
        descriptors = [str(value) for value in artifact_hints[:2] if value]
        if not descriptors:
            descriptors = [str(value) for value in rule_hints[:1] if value]
        if intent and (base_shape in BROAD_LABEL_TASK_SHAPES or (len(descriptors) >= 2 and base_shape in {"prepare_report", "write_markdown"})):
            return summarize_text(intent, 64)
        if descriptors:
            return f"{base} ({', '.join(descriptors)})"
        return base
    if intent:
        return summarize_text(intent, 64)
    return "Unnamed candidate"


def candidate_sort_key(candidate: dict[str, Any]) -> tuple[float, int, str]:
    return (
        float(candidate.get("score", 0.0)),
        int(candidate.get("support", {}).get("total_packets", 0)),
        str(candidate.get("label", "")),
    )


def packet_sort_key(packet: dict[str, Any]) -> tuple[int, str]:
    return (compare_iso_timestamps(packet.get("timestamp")), str(packet.get("packet_id", "")))


def stable_block_key(packet: dict[str, Any]) -> str:
    return stable_block_keys(packet)[0]


def stable_block_keys(packet: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    top_tool = str(packet.get("top_tool") or "none")
    task_shapes = packet.get("task_shape") or []
    artifact_hints = [str(value) for value in packet.get("artifact_hints", []) if value]
    repeated_rules = [str(item.get("normalized") or "") for item in packet_user_rule_hints(packet) if item.get("normalized")]

    specific_shapes = [str(shape) for shape in task_shapes if str(shape) and str(shape) not in GENERIC_TASK_SHAPES]
    shape_candidates = _dedupe_texts(specific_shapes or [str(shape) for shape in task_shapes if shape], limit=2)
    artifact_candidates = _dedupe_texts(artifact_hints, limit=2)
    rule_candidates = _dedupe_texts(repeated_rules, limit=2)

    for shape in shape_candidates:
        for artifact in artifact_candidates:
            keys.append(f"task+artifact:{shape}:{artifact}")
        for rule in rule_candidates:
            keys.append(f"task+rule:{shape}:{rule}")
    for artifact in artifact_candidates:
        for rule in rule_candidates:
            keys.append(f"artifact+rule:{artifact}:{rule}")
    for shape in shape_candidates:
        keys.append(f"task:{shape}")
    for artifact in artifact_candidates:
        keys.append(f"artifact:{artifact}")
    for rule in rule_candidates:
        keys.append(f"rule:{rule}")
    if top_tool != "none" and top_tool not in GENERIC_TOOL_SIGNATURES:
        keys.append(f"tool:{top_tool}")
    elif top_tool != "none" and not keys:
        keys.append(f"tool:{top_tool}")
    if not keys:
        keys.append("misc")
    return list(dict.fromkeys(keys))[:MAX_BLOCK_KEYS]


def candidate_score(support: dict[str, Any]) -> float:
    total_packets = int(support.get("total_packets", 0))
    source_count = 0
    if int(support.get("claude_packets", 0)) > 0:
        source_count += 1
    if int(support.get("codex_packets", 0)) > 0:
        source_count += 1
    recent_packets = int(support.get("recent_packets_7d", 0))
    diversity_bonus = 1.5 if source_count >= 2 else 0.0
    recency_bonus = min(recent_packets, 3) * 0.5
    return round(float(total_packets) + diversity_bonus + recency_bonus, 2)


def build_candidate_quality(candidate: dict[str, Any], total_packets_all: int) -> dict[str, Any]:
    support = candidate.get("support", {})
    total_packets = int(support.get("total_packets", 0))
    claude_packets = int(support.get("claude_packets", 0))
    codex_packets = int(support.get("codex_packets", 0))
    recent_packets = int(support.get("recent_packets_7d", 0))
    source_count = int(claude_packets > 0) + int(codex_packets > 0)
    task_shapes = [str(value) for value in candidate.get("common_task_shapes", []) if value]
    tool_signatures = [str(value) for value in candidate.get("common_tool_signatures", []) if value]
    rule_hints = [str(value) for value in candidate.get("rule_hints", []) if value]
    representative_examples = [str(value) for value in candidate.get("representative_examples", []) if value]
    split_suggestions = [str(value) for value in candidate.get("split_suggestions", []) if str(value).strip()]
    near_matches = candidate.get("near_matches")
    contamination_signals = [str(value) for value in candidate.get("contamination_signals", []) if str(value).strip()]
    origin_hint = str(candidate.get("origin_hint") or "").strip()
    user_signal_strength = str(candidate.get("user_signal_strength") or "").strip()
    contaminated_packets = int(support.get("contaminated_packets", 0))

    quality_flags: list[str] = []
    cluster_share = (float(total_packets) / float(total_packets_all)) if total_packets_all > 0 else 0.0
    is_oversized_cluster = total_packets >= OVERSIZED_CLUSTER_MIN_PACKETS and cluster_share >= OVERSIZED_CLUSTER_MIN_SHARE
    if is_oversized_cluster:
        quality_flags.append("oversized_cluster")

    generic_task_shape = bool(task_shapes) and all(shape in GENERIC_TASK_SHAPES for shape in task_shapes[:3])
    if generic_task_shape:
        quality_flags.append("generic_task_shape")

    generic_tool_count = sum(1 for tool in tool_signatures[:4] if tool in GENERIC_TOOL_SIGNATURES)
    generic_tools = generic_tool_count >= 3
    if generic_tools:
        quality_flags.append("generic_tools")

    weak_semantic_cohesion = False
    if len(representative_examples) >= 2:
        left_tokens = tokenize(representative_examples[0])
        right_tokens = tokenize(representative_examples[1])
        weak_semantic_cohesion = jaccard_score(left_tokens, right_tokens) < 0.2
    if weak_semantic_cohesion:
        quality_flags.append("weak_semantic_cohesion")

    split_signal = len(split_suggestions) >= 2
    if split_signal:
        quality_flags.append("split_recommended")

    low_user_signal = user_signal_strength == "low"
    if low_user_signal:
        quality_flags.append("low_user_signal")

    uncertain_origin = origin_hint in {"unknown", "mixed", "parent_ai"}
    if uncertain_origin and contamination_signals:
        quality_flags.append("origin_uncertain")

    if contaminated_packets > 0:
        quality_flags.append("contaminated_candidate")

    near_match_scores: list[float] = []
    near_match_reasons: list[str] = []
    if isinstance(near_matches, list):
        for item in near_matches:
            if not isinstance(item, dict):
                continue
            try:
                near_match_scores.append(float(item.get("score", 0.0)))
            except (TypeError, ValueError):
                continue
            reason = str(item.get("reason") or "").strip()
            if reason:
                near_match_reasons.append(reason)
    # Intentional: ordinary near-matches are used to seed research targets, but they do not
    # block a ready candidate on their own. We only promote this to a blocking quality flag
    # when complete-link guard failures also appeared, because that means the cluster already
    # attempted a bridge merge that was rejected at the component boundary.
    near_match_dense = (
        split_signal
        and
        len(near_match_scores) >= NEAR_MATCH_DENSE_MIN_COUNT
        and "complete_link_guard" in near_match_reasons
    )
    if near_match_dense:
        quality_flags.append("near_match_dense")

    single_session_like = total_packets <= 1
    if single_session_like:
        quality_flags.append("single_session_like")

    score = 0
    if total_packets >= 4:
        score += 2
    elif total_packets >= 2:
        score += 1
    if source_count >= 2:
        score += 1
    if recent_packets >= 2:
        score += 1
    if rule_hints:
        score += 1
    if any(shape not in GENERIC_TASK_SHAPES for shape in task_shapes):
        score += 1
    if is_oversized_cluster:
        score -= 3
    if generic_task_shape:
        score -= 1
    if generic_tools:
        score -= 1
    if weak_semantic_cohesion:
        score -= 1
    if split_signal:
        score -= 1
    if near_match_dense:
        score -= 1
    if single_session_like:
        score -= 2
    if low_user_signal:
        score -= 2
    elif uncertain_origin:
        score -= 1

    confidence = "strong"
    if score < 1:
        confidence = "insufficient"
    elif score < 2:
        confidence = "weak"
    elif score < 4:
        confidence = "medium"

    generic_cluster = generic_task_shape and generic_tools
    proposal_ready = (
        confidence in {"strong", "medium"}
        and not is_oversized_cluster
        and not weak_semantic_cohesion
        and not generic_cluster
        and not split_signal
        and not near_match_dense
        and not single_session_like
        and not low_user_signal
        and not uncertain_origin
    )

    triage_status = "ready"
    if single_session_like:
        triage_status = "rejected"
    elif proposal_ready:
        triage_status = "ready"
    elif is_oversized_cluster or weak_semantic_cohesion or split_signal or near_match_dense or low_user_signal or uncertain_origin:
        triage_status = "needs_research"
    elif confidence == "insufficient":
        triage_status = "rejected"
    elif generic_cluster:
        triage_status = "needs_research"
    else:
        triage_status = "rejected"

    evidence_parts = [
        f"{total_packets} packets",
        f"Claude {claude_packets}",
        f"Codex {codex_packets}",
        f"recent7d {recent_packets}",
    ]
    if quality_flags:
        evidence_parts.append(f"flags: {', '.join(quality_flags)}")
    evidence_summary = " / ".join(evidence_parts)
    confidence_reason = evidence_summary if proposal_ready else f"{evidence_summary} / triage: {triage_status}"

    return {
        "confidence": confidence,
        "proposal_ready": proposal_ready,
        "triage_status": triage_status,
        "quality_flags": quality_flags,
        "evidence_summary": evidence_summary,
        "confidence_reason": confidence_reason,
    }


HOOK_TOOL_INDICATORS = {"lint", "eslint", "prettier", "black", "isort", "ruff", "mypy", "flake8", "shellcheck", "hadolint"}
HOOK_RULE_INDICATORS = {"tests-before-close", "format-rule"}
RULE_DOMINANT_INDICATORS = {"always-do", "never-do", "format-rule", "confirm-before", "findings-first", "file-line-refs", "concise-updates"}
SKILL_ARTIFACT_INDICATORS = {"report", "markdown", "review", "code"}
SKILL_SHAPE_INDICATORS = {"prepare_report", "write_markdown", "debug_failure", "implement_feature", "run_tests"}


def infer_suggested_kind(candidate: dict[str, Any]) -> str:
    return infer_suggested_kind_details(candidate)["kind"]


def annotate_unclustered_packet(packet: dict[str, Any]) -> dict[str, Any]:
    annotated = dict(packet)
    quality_flags = ["unclustered_only", "single_session_like"]
    annotated.update(
        {
            "confidence": "insufficient",
            "proposal_ready": False,
            "triage_status": "rejected",
            "quality_flags": quality_flags,
            "evidence_summary": "1 packet / unclustered / not proposal-ready",
            "confidence_reason": "single observed packet only; keep as reference, not as a proposal candidate",
        }
    )
    return annotated


def build_research_targets(
    group_packets: list[dict[str, Any]],
    near_matches: list[dict[str, Any]],
    packet_lookup: dict[str, dict[str, Any]],
    limit: int = DEFAULT_RESEARCH_REF_LIMIT,
) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    seen_refs: set[str] = set()

    def add_target(session_ref: str | None, reason: str, packet_id: str | None = None) -> None:
        if not session_ref or session_ref in seen_refs or len(targets) >= limit:
            return
        seen_refs.add(session_ref)
        entry = {"session_ref": session_ref, "reason": reason}
        if packet_id:
            entry["packet_id"] = packet_id
        targets.append(entry)

    if group_packets:
        representative_packets = sorted(group_packets, key=lambda item: int(item.get("support", {}).get("message_count", 0)), reverse=True)
        add_target(representative_packets[0].get("session_ref"), "representative", representative_packets[0].get("packet_id"))
        if len(representative_packets) > 1:
            add_target(representative_packets[1].get("session_ref"), "representative", representative_packets[1].get("packet_id"))

        outlier_packet = None
        if len(group_packets) > 2:
            outlier_packet = min(
                group_packets,
                key=lambda item: len(tokenize(str(item.get("primary_intent") or ""))),
            )
        if outlier_packet is not None:
            add_target(outlier_packet.get("session_ref"), "outlier", outlier_packet.get("packet_id"))

    for match in near_matches:
        packet_id = str(match.get("packet_id") or "")
        matched_packet = packet_lookup.get(packet_id, {})
        add_target(matched_packet.get("session_ref"), "near_match", packet_id or None)
        if len(targets) >= limit:
            break

    if len(targets) < limit:
        for packet in group_packets:
            add_target(packet.get("session_ref"), "fallback", packet.get("packet_id"))
            if len(targets) >= limit:
                break

    return targets[:limit]


def build_research_brief(candidate: dict[str, Any]) -> dict[str, Any]:
    label = str(candidate.get("label") or "candidate")
    quality_flags = [str(value) for value in candidate.get("quality_flags", []) if value]
    contamination_signals = [str(value) for value in candidate.get("contamination_signals", []) if str(value).strip()]
    origin_hint = str(candidate.get("origin_hint") or "").strip()
    intent_trace = _candidate_text_list(candidate, "intent_trace", limit=MAX_INTENT_TRACE_ITEMS)
    constraints = _candidate_text_list(candidate, "constraints", limit=MAX_CONSTRAINT_ITEMS)
    acceptance_criteria = _candidate_text_list(candidate, "acceptance_criteria", limit=MAX_ACCEPTANCE_CRITERIA_ITEMS)
    objective = f"Validate whether '{label}' is one repeatable automation candidate or a merged cluster that should be split or rejected."
    questions = [
        "Do the target refs show one stable objective repeated across sessions?",
        "Are multiple distinct task types mixed inside this candidate?",
        "If the cluster should be split, what is the cleanest split axis?",
        "After reading the target refs, should this candidate be promoted to ready, split for re-triage, or rejected?",
    ]
    decision_rules = [
        "Promote to ready only if the sampled refs show one coherent objective with reusable steps.",
        "Reject if the sampled refs are mostly one-off tasks or context-specific requests.",
        "Split and re-triage if the sampled refs contain clearly different objectives that only share generic tools or review-style language.",
    ]
    if "oversized_cluster" in quality_flags:
        decision_rules.append("Because this is an oversized cluster, test split-first before forcing one proposal label.")
    if "generic_task_shape" in quality_flags or "generic_tools" in quality_flags:
        decision_rules.append("Do not treat shared review/search tooling alone as evidence of one reusable automation pattern.")
    if "weak_semantic_cohesion" in quality_flags:
        decision_rules.append("If representative examples point to different goals, keep the candidate out of ready state.")
    if contamination_signals or origin_hint in {"unknown", "mixed", "parent_ai"}:
        questions.append("Do the sampled refs reflect a real human request, or are they dominated by assistant/internal scaffolding?")
        decision_rules.append("Reject or defer candidates whose evidence is dominated by assistant fallback, summary fallback, or internal instruction patterns.")
    if len(intent_trace) >= 2:
        questions.append("Which intent variants are the same workflow, and which ones indicate a mixed cluster?")
    if constraints:
        decision_rules.append("Keep the observed user constraints intact when deciding whether this should become one reusable workflow.")
    if acceptance_criteria:
        questions.append("Do the sampled refs preserve the observed acceptance/output expectations closely enough to justify one proposal?")

    return {
        "objective": objective,
        "questions": questions,
        "decision_rules": decision_rules,
        "target_refs": candidate.get("research_targets", []),
    }


def build_detail_signal(detail: dict[str, Any]) -> dict[str, Any]:
    messages = detail.get("messages", [])
    user_texts = [str(message.get("text") or "") for message in messages if isinstance(message, dict) and message.get("role") == "user"]
    assistant_texts = [str(message.get("text") or "") for message in messages if isinstance(message, dict) and message.get("role") == "assistant"]
    feature_texts, _feature_source = feature_messages_for_packet(user_texts, assistant_texts)
    tools = [str(tool.get("name") or "") for tool in detail.get("tool_calls", []) if isinstance(tool, dict) and tool.get("name")]
    task_shapes = infer_task_shapes(feature_texts, [])
    artifact_hints = infer_artifact_hints(feature_texts, [])
    user_rule_hints = infer_rule_hints(user_texts, str(detail.get("workspace") or ""), role="user")
    assistant_rule_hints = infer_rule_hints(assistant_texts, str(detail.get("workspace") or ""), role="assistant")
    user_repeated_rules = infer_repeated_rules(user_texts, str(detail.get("workspace") or ""), role="user")
    assistant_repeated_rules = infer_repeated_rules(assistant_texts, str(detail.get("workspace") or ""), role="assistant")
    primary_intent, _full_user_intent, _primary_intent_source = build_primary_intent_fields(
        user_texts,
        assistant_texts,
        str(detail.get("workspace") or ""),
        user_message_source=PRIMARY_INTENT_SOURCE_RAW,
    )
    return {
        "session_ref": detail.get("session_ref"),
        "task_shapes": task_shapes,
        "artifact_hints": artifact_hints,
        "user_rule_hints": [item.get("normalized") for item in user_rule_hints if item.get("normalized")],
        "assistant_rule_hints": [item.get("normalized") for item in assistant_rule_hints if item.get("normalized")],
        "user_repeated_rules": [item.get("normalized") for item in user_repeated_rules if item.get("normalized")],
        "assistant_repeated_rules": [item.get("normalized") for item in assistant_repeated_rules if item.get("normalized")],
        "repeated_rules": [item.get("normalized") for item in user_repeated_rules if item.get("normalized")],
        "constraints": build_constraints(user_texts, str(detail.get("workspace") or "")),
        "acceptance_criteria": build_acceptance_criteria(user_texts, str(detail.get("workspace") or "")),
        "tool_names": tools,
        "primary_intent": primary_intent,
    }


def _dominant_value_share(values: list[str]) -> tuple[str, int, float]:
    if not values:
        return "", 0, 0.0
    value, count = Counter(values).most_common(1)[0]
    return value, count, round(count / len(values), 3)


def _split_shape_groups(signals: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        task_shapes = [str(value) for value in signal.get("task_shapes", []) if value]
        primary_shape = next((shape for shape in task_shapes if shape not in GENERIC_TASK_SHAPES), "")
        if primary_shape:
            groups[primary_shape].append(signal)
    return groups


def _average_token_overlap(signals: list[dict[str, Any]]) -> float:
    token_sets = [tokenize(str(signal.get("primary_intent") or "")) for signal in signals if signal.get("primary_intent")]
    pair_scores: list[float] = []
    for index, left in enumerate(token_sets):
        for right in token_sets[index + 1 :]:
            pair_scores.append(jaccard_score(left, right))
    return round(sum(pair_scores) / len(pair_scores), 3) if pair_scores else 0.0


def _build_subcluster_triage(split_groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    triage_items: list[dict[str, Any]] = []
    for shape, shape_signals in sorted(split_groups.items()):
        artifact_values = [signal["artifact_hints"][0] for signal in shape_signals if signal.get("artifact_hints")]
        dominant_artifact, _artifact_count, artifact_share = _dominant_value_share(artifact_values)
        overlap = _average_token_overlap(shape_signals)
        if len(shape_signals) < 2:
            triage_status = "rejected"
            confidence = "insufficient"
        elif overlap >= 0.14 or artifact_share >= 0.6:
            triage_status = "ready"
            confidence = "medium" if overlap < 0.2 else "strong"
        else:
            triage_status = "needs_research"
            confidence = "weak"
        triage_items.append(
            {
                "split_label": shape,
                "triage_status": triage_status,
                "confidence": confidence,
                "session_refs": [signal.get("session_ref") for signal in shape_signals if signal.get("session_ref")],
                "artifact_hint": dominant_artifact or None,
                "average_overlap": overlap,
            }
        )
    return triage_items


def _candidate_detail_refs(candidate: dict[str, Any]) -> set[str]:
    refs = {
        str(session_ref)
        for session_ref in candidate.get("session_refs", [])
        if str(session_ref or "").strip()
    }
    for target in candidate.get("research_targets", []):
        if isinstance(target, dict):
            session_ref = str(target.get("session_ref") or "").strip()
            if session_ref:
                refs.add(session_ref)
    return refs


def judge_research_candidate(candidate: dict[str, Any], details: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_refs = _candidate_detail_refs(candidate)
    relevant_details = [
        detail
        for detail in details
        if isinstance(detail, dict)
        and (not candidate_refs or str(detail.get("session_ref") or "").strip() in candidate_refs)
    ]
    signals = [build_detail_signal(detail) for detail in relevant_details]
    if not signals:
        return {
            "recommendation": "reject_candidate",
            "proposed_triage_status": "rejected",
            "proposed_confidence": "insufficient",
            "summary": "No detail signals were available for research judgment.",
            "reasons": ["No detail records were resolved for the sampled refs."],
            "split_suggestions": [],
            "subcluster_triage": [],
            "detail_signals": [],
        }

    primary_shapes = [signal["task_shapes"][0] for signal in signals if signal.get("task_shapes")]
    distinct_primary_shapes = sorted(set(primary_shapes))
    non_generic_primary_shapes = sorted({shape for shape in distinct_primary_shapes if shape not in GENERIC_TASK_SHAPES})
    repeated_rule_count = sum(1 for signal in signals if signal.get("user_rule_hints"))
    primary_artifacts = [signal["artifact_hints"][0] for signal in signals if signal.get("artifact_hints")]
    dominant_artifact, dominant_artifact_count, dominant_artifact_share = _dominant_value_share(primary_artifacts)
    token_sets = [tokenize(str(signal.get("primary_intent") or "")) for signal in signals if signal.get("primary_intent")]
    pair_scores: list[float] = []
    for index, left in enumerate(token_sets):
        for right in token_sets[index + 1 :]:
            pair_scores.append(jaccard_score(left, right))
    average_overlap = round(sum(pair_scores) / len(pair_scores), 3) if pair_scores else 0.0

    most_common_shape = ""
    shape_count = 0
    if primary_shapes:
        most_common_shape, shape_count = Counter(primary_shapes).most_common(1)[0]

    reasons: list[str] = []
    split_suggestions: list[str] = []
    recommendation = "reject_candidate"
    proposed_triage_status = "rejected"
    proposed_confidence = "insufficient"

    split_groups = _split_shape_groups(signals)
    eligible_split_groups = sorted(shape for shape, shape_signals in split_groups.items() if len(shape_signals) >= 2)
    subcluster_triage = _build_subcluster_triage(split_groups) if split_groups else []
    split_first = len(non_generic_primary_shapes) >= 2 and average_overlap < 0.22
    promote_by_shape = shape_count >= 2 and average_overlap >= 0.12 and (repeated_rule_count >= 1 or most_common_shape not in GENERIC_TASK_SHAPES)
    promote_by_intent_artifact = (
        dominant_artifact_count >= 2
        and dominant_artifact_share >= 0.6
        and average_overlap >= 0.14
    )

    if split_first:
        recommendation = "split_candidate"
        proposed_triage_status = "needs_research"
        proposed_confidence = "weak"
        split_suggestions = eligible_split_groups or non_generic_primary_shapes
        reasons.append("Sampled refs contain multiple non-generic task objectives with low overlap; split-first is safer.")
    elif promote_by_shape or promote_by_intent_artifact:
        recommendation = "promote_ready"
        proposed_triage_status = "ready"
        proposed_confidence = "medium" if average_overlap < 0.2 else "strong"
        if promote_by_shape:
            reasons.append("Sampled refs show one repeatable objective with reusable steps.")
        else:
            reasons.append("Sampled refs stay consistent at the intent/artifact level even though task-shape evidence is weaker.")
    elif average_overlap < 0.08 and repeated_rule_count == 0:
        recommendation = "reject_candidate"
        proposed_triage_status = "rejected"
        proposed_confidence = "insufficient"
        reasons.append("Sampled refs do not show enough coherence to justify one automation candidate.")
    elif len(distinct_primary_shapes) >= 2 and average_overlap < 0.12:
        recommendation = "split_candidate"
        proposed_triage_status = "needs_research"
        proposed_confidence = "weak"
        split_suggestions = distinct_primary_shapes
        reasons.append("Sampled refs partially overlap but still mix multiple objectives.")
    else:
        recommendation = "reject_candidate"
        proposed_triage_status = "rejected"
        proposed_confidence = "weak"
        reasons.append("Sampled refs remain too generic to promote safely.")

    if "oversized_cluster" in candidate.get("quality_flags", []):
        if recommendation == "promote_ready":
            reasons.append("The original cluster was oversized, but sampled refs were coherent enough to recover to ready.")
        elif recommendation != "split_candidate":
            reasons.append("The original cluster was oversized, so split-first evidence was required before promotion.")
    if "generic_tools" in candidate.get("quality_flags", []) and recommendation != "promote_ready":
        reasons.append("Shared generic tools alone were not treated as reusable automation evidence.")
    if recommendation == "promote_ready" and dominant_artifact:
        reasons.append(f"Dominant artifact hint: {dominant_artifact} ({dominant_artifact_share:.2f} share).")

    summary = (
        f"recommendation={recommendation} / sampled_refs={len(signals)} / "
        f"primary_shapes={', '.join(distinct_primary_shapes) or 'none'} / "
        f"avg_overlap={average_overlap}"
    )
    return {
        "recommendation": recommendation,
        "proposed_triage_status": proposed_triage_status,
        "proposed_confidence": proposed_confidence,
        "summary": summary,
        "reasons": reasons,
        "split_suggestions": split_suggestions,
        "subcluster_triage": subcluster_triage,
        "detail_signals": signals,
    }


def _normalize_rule_line(line: str) -> str:
    stripped = re.sub(r"^\s*[-*]\s+", "", line.strip())
    stripped = re.sub(r"\s+", " ", stripped)
    return normalize_match_text(stripped)


def _split_section_lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines()]


def _find_daytrace_section(lines: list[str]) -> tuple[int, int] | None:
    start = -1
    for index, line in enumerate(lines):
        if line.strip() == DAYTRACE_RULES_SECTION:
            start = index
            break
    if start < 0:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return start, end


def _extract_existing_rule_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if _normalize_rule_line(line)]


def _rule_polarity(normalized_line: str) -> str:
    negative_markers = ("never", "avoid", "do not", "don't", "禁止", "しない", "しないこと")
    return "negative" if any(marker in normalized_line for marker in negative_markers) else "positive"


def _is_conflicting_rule(existing_line: str, proposed_line: str) -> bool:
    existing_normalized = _normalize_rule_line(existing_line)
    proposed_normalized = _normalize_rule_line(proposed_line)
    if not existing_normalized or not proposed_normalized or existing_normalized == proposed_normalized:
        return False
    existing_tokens = tokenize(existing_normalized)
    proposed_tokens = tokenize(proposed_normalized)
    overlap = jaccard_score(existing_tokens, proposed_tokens)
    if overlap < 0.7:
        return False
    return _rule_polarity(existing_normalized) != _rule_polarity(proposed_normalized)


def _is_duplicate_rule(existing_line: str, proposed_line: str) -> bool:
    existing_normalized = _normalize_rule_line(existing_line)
    proposed_normalized = _normalize_rule_line(proposed_line)
    if not existing_normalized or not proposed_normalized:
        return False
    if existing_normalized == proposed_normalized:
        return True
    if _is_conflicting_rule(existing_line, proposed_line):
        return False
    overlap = jaccard_score(tokenize(existing_normalized), tokenize(proposed_normalized))
    return overlap >= 0.75


def _normalize_proposed_rules(proposed_rules: list[str] | str) -> list[str]:
    if isinstance(proposed_rules, str):
        raw_lines = [line.strip() for line in proposed_rules.splitlines()]
    else:
        raw_lines = [str(line).strip() for line in proposed_rules]
    normalized = [line for line in raw_lines if _normalize_rule_line(line)]
    return normalized


def infer_suggested_kind_details(candidate: dict[str, Any]) -> dict[str, str]:
    task_shapes = [str(value) for value in candidate.get("common_task_shapes", candidate.get("task_shape", [])) if value]
    artifact_hints = [str(value) for value in candidate.get("artifact_hints", []) if value]
    rule_hints = [str(value) for value in candidate.get("rule_hints", []) if value]
    support = candidate.get("support") if isinstance(candidate.get("support"), dict) else {}
    total_packets = int(support.get("total_packets", 0))

    if "claude-md" in artifact_hints or any(rule in CLAUDE_MD_RULE_NAMES for rule in rule_hints):
        return {"kind": "CLAUDE.md", "reason": "rule-centric repeated instruction"}
    if task_shapes and all(shape in HOOK_SHAPES for shape in task_shapes[:2]):
        return {"kind": "hook", "reason": "deterministic repeated automation step"}
    if any(shape in SKILL_SHAPES for shape in task_shapes):
        return {"kind": "skill", "reason": "multi-step reusable workflow"}
    if total_packets >= 4 and (any(shape in AGENT_SHAPES for shape in task_shapes) or rule_hints):
        return {"kind": "agent", "reason": "behavior-oriented repeated guidance"}
    return {"kind": "skill", "reason": "default reusable workflow fallback"}


def _candidate_task_shapes(candidate: dict[str, Any]) -> list[str]:
    return [str(value).strip() for value in candidate.get("common_task_shapes", candidate.get("task_shape", [])) if str(value).strip()]


def _candidate_artifact_hints(candidate: dict[str, Any]) -> list[str]:
    return [str(value).strip() for value in candidate.get("artifact_hints", []) if str(value).strip()]


def _candidate_rule_hints(candidate: dict[str, Any]) -> list[str]:
    return [str(value).strip() for value in candidate.get("rule_hints", []) if str(value).strip()]


def _candidate_tool_signatures(candidate: dict[str, Any]) -> list[str]:
    return [
        str(value).strip()
        for value in candidate.get("common_tool_signatures", candidate.get("tool_signature", []))
        if str(value).strip()
    ]


def _candidate_total_packets(candidate: dict[str, Any]) -> int:
    support = candidate.get("support") if isinstance(candidate.get("support"), dict) else {}
    try:
        return int(support.get("total_packets", 0))
    except (TypeError, ValueError):
        return 0


def _candidate_intent_lines_for_role(candidate: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    label = str(candidate.get("label") or "").strip()
    if label:
        lines.append(label)
    for item in candidate.get("intent_trace") or []:
        t = str(item).strip()
        if t:
            lines.append(t)
    return lines[:6]


def _line_matches_agent_role_signal(line: str) -> bool:
    low = normalize_match_text(line).lower()
    return any(sub in low for sub in AGENT_ROLE_SUBSTRINGS)


def _candidate_has_agent_role_consistency_signal(candidate: dict[str, Any]) -> bool:
    """Two or more distinct label/intent lines mention role-like language (agent guardrail assist)."""
    distinct_role_lines = 0
    seen_normalized: set[str] = set()
    for line in _candidate_intent_lines_for_role(candidate):
        if not _line_matches_agent_role_signal(line):
            continue
        key = normalize_match_text(line).lower()
        if not key or key in seen_normalized:
            continue
        seen_normalized.add(key)
        distinct_role_lines += 1
    return distinct_role_lines >= 2


def _declarative_weight_for_claude_md(candidate: dict[str, Any]) -> float:
    if _candidate_has_claude_md_signal(candidate):
        return 100.0
    weight = 0.0
    rule_hints = _candidate_rule_hints(candidate)
    weight += sum(1.0 for rule in rule_hints if rule in CLAUDE_MD_RULE_NAMES)
    for item in candidate.get("constraints", []) or []:
        text = normalize_match_text(str(item)).lower()
        if any(keyword in text for keyword in CONSTRAINT_KEYWORDS):
            weight += 1.0
    for item in candidate.get("acceptance_criteria", []) or []:
        text = normalize_match_text(str(item)).lower()
        if any(keyword in text for keyword in ACCEPTANCE_KEYWORDS):
            weight += 0.5
    return weight


def _workflow_weight_for_claude_md(candidate: dict[str, Any]) -> float:
    weight = 0.0
    for shape in _candidate_task_shapes(candidate):
        if shape in SKILL_SHAPES or shape in SKILL_SHAPE_INDICATORS:
            weight += 1.5
        if shape in HOOK_SHAPES:
            weight += 1.0
    if _candidate_has_skill_signal(candidate):
        weight += 1.0
    return weight


def _claude_md_declarative_ratio(candidate: dict[str, Any]) -> float:
    declarative = _declarative_weight_for_claude_md(candidate)
    workflow = _workflow_weight_for_claude_md(candidate)
    if declarative >= 100.0:
        return 1.0
    total = declarative + workflow
    if total < 1e-9:
        return 0.0
    return declarative / total


def _candidate_qualifies_for_claude_md_kind(candidate: dict[str, Any]) -> bool:
    """Classic artifact/rule signal, or strong declarative-to-workflow ratio (Phase 3 guardrail)."""
    if _candidate_has_claude_md_signal(candidate):
        return True
    declarative = _declarative_weight_for_claude_md(candidate)
    workflow = _workflow_weight_for_claude_md(candidate)
    if declarative >= 100.0:
        return True
    if declarative < 2.0:
        return False
    total = declarative + workflow
    if total < 1e-9:
        return False
    return (declarative / total) >= 0.55


def _build_classification_guardrail_signals(candidate: dict[str, Any]) -> dict[str, Any]:
    declarative = _declarative_weight_for_claude_md(candidate)
    workflow = _workflow_weight_for_claude_md(candidate)
    return {
        "claude_md_classic_signal": _candidate_has_claude_md_signal(candidate),
        "declarative_weight": round(min(declarative, 100.0), 3),
        "workflow_weight": round(workflow, 3),
        "declarative_ratio": round(_claude_md_declarative_ratio(candidate), 4),
        "agent_role_consistency": _candidate_has_agent_role_consistency_signal(candidate),
        "claude_md_qualifies": _candidate_qualifies_for_claude_md_kind(candidate),
        "llm_confidence": str(candidate.get("llm_confidence") or "").strip() or None,
    }


def _valid_suggested_kind(value: Any) -> str:
    normalized = str(value or "").strip()
    return normalized if normalized in VALID_SUGGESTED_KINDS else ""


def _candidate_has_claude_md_signal(candidate: dict[str, Any]) -> bool:
    artifact_hints = _candidate_artifact_hints(candidate)
    rule_hints = _candidate_rule_hints(candidate)
    return "claude-md" in artifact_hints or any(rule in CLAUDE_MD_RULE_NAMES for rule in rule_hints)


def _candidate_has_skill_signal(candidate: dict[str, Any]) -> bool:
    task_shapes = _candidate_task_shapes(candidate)
    artifact_hints = _candidate_artifact_hints(candidate)
    return any(shape in SKILL_SHAPES or shape in SKILL_SHAPE_INDICATORS for shape in task_shapes) or any(
        hint in SKILL_ARTIFACT_INDICATORS for hint in artifact_hints
    )


def _candidate_meets_hook_guardrail(candidate: dict[str, Any]) -> bool:
    task_shapes = _candidate_task_shapes(candidate)
    tool_signatures = _candidate_tool_signatures(candidate)
    rule_hints = _candidate_rule_hints(candidate)
    if task_shapes and all(shape in HOOK_SHAPES for shape in task_shapes[:2]):
        return True
    # Narrow gate before hook-rule+tool: tests-before-close + run_tests first + packets (no tool proof).
    # Must run before HOOK_RULE_INDICATORS branch because tests-before-close is also in that set.
    if (
        task_shapes
        and task_shapes[0] == "run_tests"
        and "tests-before-close" in rule_hints
        and _candidate_total_packets(candidate) >= 3
    ):
        return True
    if any(rule in HOOK_RULE_INDICATORS for rule in rule_hints):
        return any(tool in HOOK_TOOL_INDICATORS or tool in {"pytest", "make"} for tool in tool_signatures)
    return False


def _candidate_meets_agent_guardrail(candidate: dict[str, Any]) -> bool:
    task_shapes = _candidate_task_shapes(candidate)
    rule_hints = _candidate_rule_hints(candidate)
    total_packets = _candidate_total_packets(candidate)
    if total_packets < 4:
        return False
    if _candidate_has_claude_md_signal(candidate):
        return False
    if any(shape in AGENT_SHAPES for shape in task_shapes) or any(
        rule not in CLAUDE_MD_RULE_NAMES for rule in rule_hints
    ):
        return True
    return _candidate_has_agent_role_consistency_signal(candidate)


def _classification_trace_entry(stage: str, kind: str, reason: str) -> dict[str, str]:
    return {
        "stage": stage,
        "kind": kind,
        "reason": reason,
    }


def _apply_classification_guardrail(
    candidate: dict[str, Any],
    proposed_kind: str,
    *,
    heuristic: dict[str, str],
) -> dict[str, str]:
    if proposed_kind == "hook" and not _candidate_meets_hook_guardrail(candidate):
        fallback = heuristic["kind"] if heuristic["kind"] != "hook" else "skill"
        return {
            "kind": fallback,
            "reason": "guardrail override: hook requires a deterministic automation trigger",
        }
    if proposed_kind == "agent":
        if _candidate_has_claude_md_signal(candidate):
            return {
                "kind": "CLAUDE.md",
                "reason": "guardrail override: rule-centric candidates should stay in CLAUDE.md",
            }
        if not _candidate_meets_agent_guardrail(candidate):
            fallback = heuristic["kind"] if heuristic["kind"] != "agent" else "skill"
            return {
                "kind": fallback,
                "reason": "guardrail override: agent requires stronger repeated behavior signals",
            }
    if proposed_kind == "CLAUDE.md" and not _candidate_qualifies_for_claude_md_kind(candidate):
        fallback = "skill" if _candidate_has_skill_signal(candidate) else heuristic["kind"]
        if fallback == "CLAUDE.md":
            fallback = "skill"
        return {
            "kind": fallback,
            "reason": "guardrail override: CLAUDE.md should stay rule-centric, not workflow-heavy",
        }
    if proposed_kind == "skill" and _candidate_qualifies_for_claude_md_kind(candidate) and not _candidate_has_skill_signal(candidate):
        return {
            "kind": "CLAUDE.md",
            "reason": "guardrail override: repeated rules without workflow steps belong in CLAUDE.md",
        }
    return {"kind": proposed_kind, "reason": ""}


def _normalize_candidate_kind(candidate: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(candidate)
    inferred = infer_suggested_kind_details(candidate)
    trace: list[dict[str, str]] = []
    raw_kind = _valid_suggested_kind(candidate.get("suggested_kind"))
    raw_reason = str(candidate.get("suggested_kind_reason") or "").strip()
    llm_kind = _valid_suggested_kind(candidate.get("llm_suggested_kind"))
    llm_reason = str(candidate.get("llm_reason") or candidate.get("classification_reason") or "").strip()

    if raw_kind:
        selected_kind = raw_kind
        selected_reason = raw_reason or "provided candidate classification"
        selected_source = str(candidate.get("suggested_kind_source") or "provided").strip() or "provided"
        trace.append(_classification_trace_entry("provided", raw_kind, selected_reason))
        if not llm_kind:
            normalized["suggested_kind"] = selected_kind
            normalized["suggested_kind_reason"] = selected_reason
            normalized["suggested_kind_source"] = selected_source
            normalized["classification_trace"] = trace
            normalized["classification_guardrail_signals"] = _build_classification_guardrail_signals(normalized)
            return normalized
    else:
        selected_kind = inferred["kind"]
        selected_reason = inferred["reason"]
        selected_source = "heuristic"
        trace.append(_classification_trace_entry("heuristic", inferred["kind"], inferred["reason"]))

    if llm_kind:
        selected_kind = llm_kind
        selected_reason = llm_reason or "llm classification overlay"
        selected_source = "llm"
        trace.append(_classification_trace_entry("llm", llm_kind, selected_reason))

    guarded = _apply_classification_guardrail(candidate, selected_kind, heuristic=inferred)
    final_kind = guarded["kind"]
    final_reason = guarded["reason"] or selected_reason
    final_source = selected_source
    if final_kind != selected_kind:
        final_source = "guardrail_override"
        trace.append(_classification_trace_entry("guardrail", final_kind, guarded["reason"]))

    normalized["suggested_kind"] = final_kind
    normalized["suggested_kind_reason"] = final_reason
    normalized["suggested_kind_source"] = final_source
    normalized["classification_trace"] = trace
    normalized["classification_guardrail_signals"] = _build_classification_guardrail_signals(normalized)
    return normalized


def merge_classification_into_candidate(candidate: dict[str, Any], classification_payload: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(candidate)
    if not classification_payload:
        return merged
    raw_overlay = classification_payload.get("classification", classification_payload)
    if not isinstance(raw_overlay, dict):
        return merged
    overlay = dict(raw_overlay)
    llm_kind = _valid_suggested_kind(
        overlay.get("llm_suggested_kind")
        or overlay.get("suggested_kind")
        or overlay.get("kind")
    )
    if llm_kind:
        merged["llm_suggested_kind"] = llm_kind
    llm_reason = str(
        overlay.get("llm_reason")
        or overlay.get("reason")
        or overlay.get("classification_reason")
        or ""
    ).strip()
    if llm_reason:
        merged["llm_reason"] = llm_reason
    why_not = overlay.get("why_not_other_kinds")
    if isinstance(why_not, list):
        merged["why_not_other_kinds"] = [str(item).strip() for item in why_not if str(item).strip()]
    llm_confidence = str(overlay.get("confidence") or "").strip()
    if llm_confidence:
        merged["llm_confidence"] = llm_confidence
    merged["classification_overlay"] = overlay
    return merged


def _skill_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_match_text(text))
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:48] or "daytrace-skill-draft"


def build_skill_creator_handoff(context: dict[str, Any]) -> dict[str, Any]:
    skill_name = str(context.get("skill_name") or "daytrace-skill-draft").strip() or "daytrace-skill-draft"
    goal = str(context.get("goal") or "this repeated workflow").strip() or "this repeated workflow"
    artifact_hints = ", ".join(str(item) for item in context.get("artifact_hints", []) if str(item).strip()) or "n/a"
    rule_hints = ", ".join(str(item) for item in context.get("rule_hints", []) if str(item).strip()) or "n/a"
    representative_examples = [str(item) for item in context.get("representative_examples", []) if str(item).strip()]
    intent_trace = [str(item) for item in context.get("intent_trace", []) if str(item).strip()]
    constraints = [str(item) for item in context.get("constraints", []) if str(item).strip()]
    acceptance_criteria = [str(item) for item in context.get("acceptance_criteria", []) if str(item).strip()]
    example_lines = "\n".join(f"- {example}" for example in representative_examples[:3]) or "- n/a"
    intent_lines = "\n".join(f"- {item}" for item in intent_trace[:3]) or "- n/a"
    constraint_lines = "\n".join(f"- {item}" for item in constraints[:3]) or "- n/a"
    acceptance_lines = "\n".join(f"- {item}" for item in acceptance_criteria[:3]) or "- n/a"

    prompt = "\n".join(
        [
            f"Create or refine a reusable skill named `{skill_name}`.",
            f"Goal: {goal}",
            f"Artifact hints: {artifact_hints}",
            f"Rule hints: {rule_hints}",
            "Representative examples:",
            example_lines,
            "Intent trace:",
            intent_lines,
            "Constraints:",
            constraint_lines,
            "Acceptance criteria:",
            acceptance_lines,
            "Use the official skill-creator workflow and treat the scaffold context as guidance, not a final draft.",
        ]
    )

    return {
        "tool": "skill-creator",
        "entrypoint": "/skill-creator",
        "official": True,
        "mode": "manual-handoff",
        "target_skill_name": skill_name,
        "suggested_invocation": f"/skill-creator {skill_name} をスキルにしてください",
        "prompt": prompt,
        "instructions": [
            "Open /skill-creator manually.",
            "Pass the scaffold context alongside this prompt.",
            "Let skill-creator produce the actual SKILL.md and any bundled resources.",
        ],
        "required_context_fields": [
            "skill_name",
            "goal",
            "task_shapes",
            "artifact_hints",
            "rule_hints",
            "intent_trace",
            "constraints",
            "acceptance_criteria",
            "representative_examples",
            "evidence_summaries",
        ],
    }


def _resolved_workspace_path(raw: str | None) -> str | None:
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        return str(Path(text).expanduser().resolve())
    except OSError:
        return text


def build_cross_repo_handoff_metadata(
    candidate: dict[str, Any],
    prepare_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Schema v2 fields for skill-creator handoff (cross-repo UX). Merged into skill_creator_handoff."""
    prepare_payload = prepare_payload or {}
    config = prepare_payload.get("config") if isinstance(prepare_payload.get("config"), dict) else {}
    raw_current = config.get("workspace")
    current_workspace = _resolved_workspace_path(str(raw_current).strip()) if isinstance(raw_current, str) and raw_current.strip() else None

    dominant_workspace = _resolved_workspace_path(str(candidate.get("dominant_workspace") or "").strip() or None)

    path_examples: list[str] = []
    wp = candidate.get("workspace_paths")
    if isinstance(wp, list):
        for item in wp[:8]:
            if isinstance(item, str) and item.strip():
                rp = _resolved_workspace_path(item.strip())
                if rp and rp not in path_examples:
                    path_examples.append(rp)

    support = candidate.get("support") if isinstance(candidate.get("support"), dict) else {}
    try:
        unique_ws = int(support.get("unique_workspaces", 0) or 0)
    except (TypeError, ValueError):
        unique_ws = 0

    signals: list[str] = []
    cross_repo = False
    handoff_scope = "current_repo"
    confidence = "low"
    target_hint: str | None = None
    resolution_note = ""

    if current_workspace and dominant_workspace and dominant_workspace != current_workspace:
        cross_repo = True
        handoff_scope = "other_repo"
        target_hint = dominant_workspace
        signals.append("dominant_workspace_mismatch")
        confidence = "high"
        resolution_note = (
            "クラスタの主たる workspace と観測時の --workspace が一致しません。"
            " skill-creator は対象リポジトリを開いた状態で実行してください。"
        )
    elif current_workspace and path_examples:
        outside = [
            p
            for p in path_examples
            if p != current_workspace and not is_within_path(p, current_workspace)
        ]
        if outside:
            cross_repo = True
            handoff_scope = "other_repo"
            target_hint = outside[0]
            signals.append("packet_workspace_outside_config_workspace")
            confidence = "medium"
            resolution_note = (
                "証跡の workspace の一部が観測時の workspace ルート外です。別リポジトリのログが混ざっている可能性があります。"
            )
    elif not current_workspace and dominant_workspace:
        target_hint = dominant_workspace
        signals.append("prepare_workspace_unset")
        resolution_note = (
            "観測に workspace フィルタが無い実行です。適用先はログ上の workspace を優先して確認してください。"
        )
        confidence = "low"
    elif unique_ws >= 2 and not cross_repo:
        signals.append("multi_workspace_cluster")
        resolution_note = "同一クラスタ内に複数 workspace があります。適用先を手元で確認してください。"
        confidence = "low"

    if not cross_repo:
        target_hint = target_hint or current_workspace or dominant_workspace
        if not resolution_note:
            resolution_note = "観測 workspace と同一リポジトリ向けの候補として扱っています。"

    display_name: str | None = None
    if target_hint:
        try:
            display_name = Path(target_hint).name or target_hint
        except (OSError, ValueError, TypeError):
            display_name = target_hint

    if cross_repo:
        execution_instruction = "\n".join(
            [
                "別リポジトリ向けの可能性が高いです。現在の CWD だけを信頼しないでください。",
                f"1. 対象リポジトリを開く（推奨: {target_hint or 'ログの workspace を確認'}）",
                "2. そのリポジトリをプロジェクトルートにした状態で /skill-creator を実行する",
                "3. この handoff の context_file（JSON bundle）の scaffold をプロンプトに含める",
            ]
        )
    else:
        base = target_hint or current_workspace or "このリポジトリ"
        execution_instruction = "\n".join(
            [
                f"1. 適用先のリポジトリを開く（目安: {base}）",
                "2. /skill-creator を実行し、この handoff の scaffold 情報を渡す",
            ]
        )

    meta = {
        "handoff_schema_version": 2,
        "cross_repo": cross_repo,
        "target_workspace_hint": target_hint,
        "current_workspace": current_workspace,
        "handoff_scope": handoff_scope,
        "execution_instruction": execution_instruction,
        "workspace_resolution_note": resolution_note,
        "cross_repo_confidence": confidence,
        "detection_signals": signals,
        "target_repo_display_name": display_name,
        "target_path_examples": path_examples[:3],
    }
    return meta


def merge_cross_repo_into_skill_handoff(
    handoff: dict[str, Any],
    candidate: dict[str, Any],
    prepare_payload: dict[str, Any] | None,
) -> None:
    """Mutate skill_creator_handoff with schema v2 cross-repo fields and extended instructions."""
    meta = build_cross_repo_handoff_metadata(candidate, prepare_payload)
    handoff.update(meta)
    base_instructions = [
        "Open /skill-creator manually.",
        "Pass the scaffold context alongside this prompt.",
        "Let skill-creator produce the actual SKILL.md and any bundled resources.",
    ]
    handoff["instructions"] = [
        meta["workspace_resolution_note"],
        *meta["execution_instruction"].split("\n"),
        *base_instructions,
    ]
    handoff["prompt"] = (
        str(handoff.get("prompt") or "")
        + "\n\n---\n"
        + f"Handoff scope: {meta['handoff_scope']}\n"
        + f"{meta['workspace_resolution_note']}\n\n"
        + meta["execution_instruction"]
    )


def candidate_split_suggestions(candidate: dict[str, Any]) -> list[str]:
    split_suggestions = candidate.get("split_suggestions")
    if isinstance(split_suggestions, list) and split_suggestions:
        return [str(value) for value in split_suggestions if str(value).strip()]
    judgment = candidate.get("research_judgment")
    if isinstance(judgment, dict):
        raw = judgment.get("split_suggestions")
        if isinstance(raw, list):
            return [str(value) for value in raw if str(value).strip()]
    return []


def _top_signal_values(values: list[str], limit: int) -> list[str]:
    counts: Counter[str] = Counter()
    ordered: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        counts[value] += 1
        if value not in ordered:
            ordered.append(value)
    ordered.sort(key=lambda item: (counts[item], item), reverse=True)
    return ordered[:limit]


def _split_child_candidate_id(parent_candidate_id: str, split_label: str, index: int) -> str:
    base_parent = str(parent_candidate_id or "candidate").strip() or "candidate"
    slug = _skill_slug(split_label or f"split-{index}")
    return f"{base_parent}--split-{index:02d}-{slug}"


def _source_name_from_session_ref(session_ref: str) -> str:
    ref = str(session_ref or "").strip()
    if ref.startswith("claude:"):
        return CLAUDE_SOURCE
    if ref.startswith("codex:"):
        return CODEX_SOURCE
    return "detail-research"


def _build_split_child_evidence_items(
    parent: dict[str, Any],
    *,
    session_refs: list[str],
    relevant_signals: list[dict[str, Any]],
    split_label: str,
) -> list[dict[str, str]]:
    evidence_items = parent.get("evidence_items")
    filtered_items: list[dict[str, str]] = []
    if isinstance(evidence_items, list):
        allowed_refs = {ref for ref in session_refs if ref}
        for item in evidence_items:
            if not isinstance(item, dict):
                continue
            if allowed_refs and str(item.get("session_ref") or "").strip() not in allowed_refs:
                continue
            summary = str(item.get("summary") or "").strip()
            if not summary:
                continue
            filtered_items.append(dict(item))
    if filtered_items:
        return filtered_items[:3]

    synthesized: list[dict[str, str]] = []
    for signal in relevant_signals[:3]:
        session_ref = str(signal.get("session_ref") or "").strip()
        summary = str(signal.get("primary_intent") or "").strip() or split_label.replace("_", " ")
        if not summary:
            continue
        synthesized.append(
            {
                "session_ref": session_ref,
                "source": _source_name_from_session_ref(session_ref),
                "summary": summary,
            }
        )
    return synthesized


def _materialize_split_candidates(parent: dict[str, Any]) -> list[dict[str, Any]]:
    judgment = parent.get("research_judgment")
    if not isinstance(judgment, dict) or str(judgment.get("recommendation") or "") != "split_candidate":
        return []

    subcluster_triage = judgment.get("subcluster_triage")
    if not isinstance(subcluster_triage, list) or not subcluster_triage:
        return []

    detail_signals = judgment.get("detail_signals")
    if not isinstance(detail_signals, list):
        detail_signals = []

    split_children: list[dict[str, Any]] = []
    parent_candidate_id = str(parent.get("candidate_id") or "candidate").strip() or "candidate"
    parent_label = str(parent.get("label") or "candidate").strip() or "candidate"

    for index, item in enumerate(subcluster_triage, start=1):
        if not isinstance(item, dict):
            continue

        split_label = str(item.get("split_label") or "").strip()
        triage_status = str(item.get("triage_status") or "").strip() or "needs_research"
        confidence = str(item.get("confidence") or parent.get("confidence") or "weak").strip() or "weak"
        session_refs = [
            str(session_ref).strip()
            for session_ref in item.get("session_refs", [])
            if str(session_ref or "").strip()
        ]
        relevant_signals = [
            signal
            for signal in detail_signals
            if isinstance(signal, dict) and str(signal.get("session_ref") or "").strip() in set(session_refs)
        ]

        task_shapes = _top_signal_values(
            [shape for signal in relevant_signals for shape in signal.get("task_shapes", [])],
            3,
        )
        if not task_shapes and split_label:
            task_shapes = [split_label]
        artifact_hints = _top_signal_values(
            [hint for signal in relevant_signals for hint in signal.get("artifact_hints", [])],
            3,
        )
        rule_hints = _top_signal_values(
            [
                hint
                for signal in relevant_signals
                for hint in [*signal.get("user_rule_hints", []), *signal.get("repeated_rules", [])]
            ],
            3,
        )
        tool_signatures = _top_signal_values(
            [tool for signal in relevant_signals for tool in signal.get("tool_names", [])],
            5,
        )
        representative_examples = _top_signal_values(
            [str(signal.get("primary_intent") or "").strip() for signal in relevant_signals],
            2,
        )
        intent_trace = _dedupe_texts(
            [str(signal.get("primary_intent") or "").strip() for signal in relevant_signals],
            limit=MAX_INTENT_TRACE_ITEMS,
        )
        constraints = _dedupe_texts(
            [value for signal in relevant_signals for value in signal.get("constraints", [])],
            limit=MAX_CONSTRAINT_ITEMS,
        )
        acceptance_criteria = _dedupe_texts(
            [value for signal in relevant_signals for value in signal.get("acceptance_criteria", [])],
            limit=MAX_ACCEPTANCE_CRITERIA_ITEMS,
        )

        evidence_items = _build_split_child_evidence_items(
            parent,
            session_refs=session_refs,
            relevant_signals=relevant_signals,
            split_label=split_label,
        )
        source_names = [_source_name_from_session_ref(ref) for ref in session_refs]
        support = {
            "total_packets": len(session_refs) or len(relevant_signals),
            "claude_packets": sum(1 for name in source_names if name == CLAUDE_SOURCE),
            "codex_packets": sum(1 for name in source_names if name == CODEX_SOURCE),
            "total_tool_calls": sum(len(signal.get("tool_names", [])) for signal in relevant_signals),
            "unique_workspaces": 0,
            "recent_packets_7d": len(session_refs) or len(relevant_signals),
        }
        overlap = item.get("average_overlap")
        overlap_suffix = f" / avg_overlap={overlap}" if isinstance(overlap, (int, float)) else ""
        label = candidate_label(
            {
                "common_task_shapes": task_shapes,
                "artifact_hints": artifact_hints,
                "rule_hints": rule_hints,
                "primary_intent": representative_examples[0] if representative_examples else split_label.replace("_", " "),
            }
        )

        split_children.append(
            {
                "candidate_id": _split_child_candidate_id(parent_candidate_id, split_label, index),
                "label": label,
                "triage_status": triage_status,
                "proposal_ready": triage_status == "ready",
                "confidence": confidence,
                "suggested_kind": "",
                "support": support,
                "common_task_shapes": task_shapes,
                "common_tool_signatures": tool_signatures,
                "artifact_hints": artifact_hints,
                "rule_hints": rule_hints,
                "representative_examples": representative_examples,
                "session_refs": session_refs,
                "near_matches": [],
                "research_targets": [],
                "evidence_items": evidence_items,
                "split_suggestions": [],
                "intent_trace": intent_trace,
                "constraints": constraints,
                "acceptance_criteria": acceptance_criteria,
                "score": float(parent.get("score", 0.0)),
                "quality_flags": [],
                "evidence_summary": (
                    f"split from {parent_label}: {split_label or 'subcluster'} / sampled_refs={len(session_refs) or len(relevant_signals)}"
                    f"{overlap_suffix}"
                ),
                "confidence_reason": (
                    f"親候補 {parent_label} を {split_label or 'subcluster'} に分割して再評価"
                    f"{overlap_suffix}"
                ),
                "split_origin": {
                    "parent_candidate_id": parent_candidate_id,
                    "parent_label": parent_label,
                    "split_label": split_label,
                },
                "dominant_workspace": parent.get("dominant_workspace"),
                "workspace_paths": list(parent.get("workspace_paths") or []),
            }
        )

    return split_children


def build_next_step_stub(candidate: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(candidate.get("suggested_kind") or "").strip()
    if kind not in {"hook", "agent"}:
        return None

    label = str(candidate.get("label") or "candidate").strip() or "candidate"
    task_shapes = [str(value) for value in candidate.get("common_task_shapes", candidate.get("task_shape", [])) if str(value).strip()]
    tool_names = [
        str(value)
        for value in candidate.get("common_tool_signatures", candidate.get("tool_signature", []))
        if str(value).strip() and str(value) not in GENERIC_TOOL_SIGNATURES
    ][:3]
    rule_hints = [str(value) for value in candidate.get("rule_hints", []) if str(value).strip()]
    constraints = _candidate_text_list(candidate, "constraints", limit=MAX_CONSTRAINT_ITEMS)
    acceptance_criteria = _candidate_text_list(candidate, "acceptance_criteria", limit=MAX_ACCEPTANCE_CRITERIA_ITEMS)
    representative_examples = [str(value) for value in candidate.get("representative_examples", []) if str(value).strip()]

    if kind == "hook":
        trigger_event = "Stop" if "run_tests" in task_shapes or "tests-before-close" in rule_hints else "PostToolUse"
        action_summary = "再現性の高い定型チェックを自動で挟む"
        if "run_tests" in task_shapes:
            action_summary = "関連変更があるときにテスト系コマンドを自動で実行する"
        elif tool_names:
            action_summary = f"{', '.join(tool_names)} 周辺の定型処理を自動化する"
        guard_condition = constraints[0] if constraints else "無関係な変更や一度きりの作業では実行しない"
        return {
            "kind": "hook",
            "prompt": f"「{label} を hook として設定しますか？」",
            "trigger_event": trigger_event,
            "target_tools": tool_names,
            "action_summary": action_summary,
            "guard_condition": guard_condition,
        }

    behavior_rules = _dedupe_texts([*rule_hints[:2], *constraints[:2], *acceptance_criteria[:1]], limit=3)
    role_summary = representative_examples[0] if representative_examples else f"{label} を継続的に補助する"
    return {
        "kind": "agent",
        "prompt": f"「{label} をエージェントとして作成しますか？」",
        "role_summary": role_summary,
        "behavior_rules": behavior_rules,
        "trigger": "同種の依頼が続く時に呼び出し、一貫した振る舞いを提供する",
    }


def _candidate_stable_identity_parts(normalized: dict[str, Any]) -> dict[str, Any]:
    """Label + intent/constraints/criteria slices used for decision_key and content_key."""
    return {
        "label": normalize_match_text(str(normalized.get("label") or normalized.get("primary_intent") or "").strip()),
        "intent_trace": [
            normalize_match_text(item)
            for item in _candidate_text_list(normalized, "intent_trace", limit=MAX_INTENT_TRACE_ITEMS)[:2]
        ],
        "constraints": [
            normalize_match_text(item)
            for item in _candidate_text_list(normalized, "constraints", limit=MAX_CONSTRAINT_ITEMS)[:2]
        ],
        "acceptance_criteria": [
            normalize_match_text(item)
            for item in _candidate_text_list(normalized, "acceptance_criteria", limit=MAX_ACCEPTANCE_CRITERIA_ITEMS)[:2]
        ],
    }


def _hash_stable_identity(identity: dict[str, Any]) -> str:
    serialized = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(serialized.encode("utf-8")).hexdigest()[:16]


def build_candidate_content_key(candidate: dict[str, Any]) -> str:
    """Stable key for carry-forward when suggested_kind changes (excluded from identity)."""
    normalized = _normalize_candidate_kind(candidate)
    return _hash_stable_identity(_candidate_stable_identity_parts(normalized))


def build_candidate_decision_key(candidate: dict[str, Any]) -> str:
    normalized = _normalize_candidate_kind(candidate)
    identity = {
        "suggested_kind": str(normalized.get("suggested_kind") or "").strip(),
        **_candidate_stable_identity_parts(normalized),
    }
    return _hash_stable_identity(identity)


def build_candidate_decision_stub(candidate: dict[str, Any]) -> dict[str, Any]:
    candidate = _normalize_candidate_kind(candidate)
    triage_status = str(candidate.get("triage_status") or "")
    if triage_status == "ready" and candidate.get("proposal_ready"):
        action = "adopt"
    elif triage_status == "needs_research":
        action = "defer"
    else:
        action = "reject"
    reason_codes = [str(flag) for flag in candidate.get("quality_flags", []) if str(flag).strip()]
    if not reason_codes:
        reason_codes = [triage_status or "unknown"]
    intent_trace = _candidate_text_list(candidate, "intent_trace", limit=MAX_INTENT_TRACE_ITEMS)
    constraints = _candidate_text_list(candidate, "constraints", limit=MAX_CONSTRAINT_ITEMS)
    acceptance_criteria = _candidate_text_list(candidate, "acceptance_criteria", limit=MAX_ACCEPTANCE_CRITERIA_ITEMS)
    support = candidate.get("support")
    prior_state = candidate.get("prior_decision_state")
    current_observation_count = 0
    if isinstance(support, dict):
        try:
            current_observation_count = int(support.get("total_packets", 0))
        except (TypeError, ValueError):
            current_observation_count = 0
    prior_observation_count = 0
    if isinstance(prior_state, dict):
        try:
            prior_observation_count = int(prior_state.get("observation_count", 0))
        except (TypeError, ValueError):
            prior_observation_count = 0
    decision_key = build_candidate_decision_key(candidate)
    content_key = build_candidate_content_key(candidate)
    return {
        "candidate_id": str(candidate.get("candidate_id") or candidate.get("packet_id") or ""),
        "decision_key": decision_key,
        "content_key": content_key,
        "label": str(candidate.get("label") or candidate.get("primary_intent") or ""),
        "recommended_action": action,
        "triage_status": triage_status,
        "suggested_kind": str(candidate.get("suggested_kind") or ""),
        "reason_codes": reason_codes,
        "split_suggestions": candidate_split_suggestions(candidate),
        "intent_trace": intent_trace,
        "constraints": constraints,
        "acceptance_criteria": acceptance_criteria,
        "user_decision": None,
        "user_decision_timestamp": None,
        "carry_forward": True,
        "observation_count": current_observation_count,
        "prior_observation_count": prior_observation_count,
        "observation_delta": current_observation_count - prior_observation_count,
    }


def build_learning_feedback(
    ready: list[dict[str, Any]],
    needs_research: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if ready:
        return {
            "status": "ready_candidates_available",
            "reason_summary": "formalizable patterns were found",
            "next_step": "adopt one ready candidate or inspect the scaffold context / next-step stub before applying it",
            "split_candidates": [],
        }

    split_candidates = [
        {
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "label": str(candidate.get("label") or ""),
            "split_suggestions": candidate_split_suggestions(candidate),
        }
        for candidate in needs_research
        if candidate_split_suggestions(candidate)
    ]
    return {
        "status": "needs_more_observation" if needs_research else "insufficient_signal",
        "reason_summary": _no_ready_reason_summary(needs_research, rejected),
        "next_step": _no_ready_next_step(metadata),
        "split_candidates": split_candidates,
    }


def build_claude_md_immediate_apply_preview(cwd: str | Path, proposed_rules: list[str] | str) -> dict[str, Any]:
    target_path = Path(cwd).expanduser().resolve() / CLAUDE_MD_FILENAME
    normalized_rules = _normalize_proposed_rules(proposed_rules)
    if not normalized_rules:
        return {
            "status": "empty_rules",
            "target_path": str(target_path),
            "applied": False,
            "preview": "",
            "rules_to_append": [],
        }
    candidate_lines = list(
        dict.fromkeys(RULE_BULLET_PREFIX + re.sub(r"^[-*]\s+", "", line) for line in normalized_rules)
    )
    if target_path.exists():
        current_text = target_path.read_text(encoding="utf-8")
        current_lines = _split_section_lines(current_text)
        section = _find_daytrace_section(current_lines)
    else:
        current_text = ""
        current_lines = []
        section = None
    existing_lines = _extract_existing_rule_lines(current_lines if section is None else current_lines[section[0] + 1 : section[1]])
    duplicates = [line for line in candidate_lines if any(_is_duplicate_rule(existing_line, line) for existing_line in existing_lines)]
    rules_to_append = [line for line in candidate_lines if line not in duplicates]

    conflict_pairs: list[dict[str, str]] = []
    for proposed_line in rules_to_append:
        for existing_line in existing_lines:
            if _is_conflicting_rule(existing_line, proposed_line):
                conflict_pairs.append({"existing": existing_line, "proposed": proposed_line})
                break
    for index, proposed_line in enumerate(rules_to_append):
        for prior_line in rules_to_append[:index]:
            if _is_conflicting_rule(prior_line, proposed_line):
                conflict_pairs.append({"existing": prior_line, "proposed": proposed_line})
                break

    if not rules_to_append:
        return {
            "status": "duplicate",
            "target_path": str(target_path),
            "applied": False,
            "preview": "",
            "duplicates": duplicates,
            "rules_to_append": [],
        }

    if section is None:
        updated_lines = current_lines[:]
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("")
        updated_lines.append(DAYTRACE_RULES_SECTION)
        updated_lines.append("")
        updated_lines.extend(rules_to_append)
    else:
        updated_lines = current_lines[: section[1]] + rules_to_append + current_lines[section[1] :]

    updated_text = "\n".join(updated_lines).rstrip() + "\n"
    preview = "".join(
        unified_diff(
            current_text.splitlines(keepends=True),
            updated_text.splitlines(keepends=True),
            fromfile="/dev/null" if not target_path.exists() else str(target_path),
            tofile=str(target_path),
        )
    )
    status = "conflict" if conflict_pairs else "ready_to_apply"
    return {
        "status": status,
        "target_path": str(target_path),
        "applied": False,
        "missing_file": not target_path.exists(),
        "preview": preview,
        "duplicates": duplicates,
        "conflicts": conflict_pairs,
        "rules_to_append": rules_to_append,
        "updated_text": updated_text,
    }


def apply_claude_md_immediate_rules(cwd: str | Path, proposed_rules: list[str] | str) -> dict[str, Any]:
    preview = build_claude_md_immediate_apply_preview(cwd, proposed_rules)
    if preview.get("status") != "ready_to_apply":
        return preview
    target_path = Path(str(preview["target_path"]))
    target_path.write_text(str(preview.get("updated_text") or ""), encoding="utf-8")
    applied = dict(preview)
    applied["status"] = "applied"
    applied["applied"] = True
    return applied


def merge_judgment_into_candidate(candidate: dict[str, Any], judgment_payload: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(candidate)
    if not judgment_payload:
        return merged
    raw_judgment = judgment_payload.get("judgment", judgment_payload)
    if not isinstance(raw_judgment, dict):
        return merged
    judgment = dict(raw_judgment)
    merged["research_judgment"] = judgment
    recommendation = str(judgment.get("recommendation") or "")
    proposed_triage_status = str(judgment.get("proposed_triage_status") or merged.get("triage_status") or "")
    proposed_confidence = str(judgment.get("proposed_confidence") or merged.get("confidence") or "")
    judgment_summary = str(judgment.get("summary") or "").strip()
    judgment_reasons = [str(reason).strip() for reason in judgment.get("reasons", []) if str(reason).strip()]
    merged["triage_status"] = proposed_triage_status
    merged["confidence"] = proposed_confidence
    merged["proposal_ready"] = recommendation == "promote_ready"
    if judgment_summary:
        merged["evidence_summary"] = judgment_summary
        merged["confidence_reason"] = judgment_summary
    elif judgment_reasons:
        merged["confidence_reason"] = judgment_reasons[0]
    if recommendation == "promote_ready":
        resolved_quality_flags = {
            str(flag).strip()
            for flag in merged.get("resolved_quality_flags", [])
            if str(flag).strip() in READY_BLOCKING_FLAGS
        }
        resolved_quality_flags.update(
            str(flag).strip()
            for flag in merged.get("quality_flags", [])
            if str(flag).strip() in READY_BLOCKING_FLAGS
        )
        if resolved_quality_flags:
            merged["resolved_quality_flags"] = sorted(resolved_quality_flags)
            judgment["resolved_quality_flags"] = sorted(resolved_quality_flags)
            merged["quality_flags"] = [
                flag for flag in merged.get("quality_flags", []) if str(flag).strip() not in READY_BLOCKING_FLAGS
            ]
    return merged


def _is_oversized_and_unresolved(candidate: dict[str, Any]) -> bool:
    """Return True if candidate still carries oversized_cluster after research resolution."""
    quality_flags = candidate.get("quality_flags") or []
    if "oversized_cluster" not in quality_flags:
        return False
    if "oversized_cluster" in _resolved_ready_blocking_flags(candidate):
        return False
    return True


def _resolved_ready_blocking_flags(candidate: dict[str, Any]) -> set[str]:
    resolved_flags = {
        str(flag).strip()
        for flag in candidate.get("resolved_quality_flags", [])
        if str(flag).strip() in READY_BLOCKING_FLAGS
    }
    if resolved_flags:
        return resolved_flags
    judgment = candidate.get("research_judgment")
    if not isinstance(judgment, dict) or judgment.get("recommendation") != "promote_ready":
        return set()
    explicit_flags = {
        str(flag).strip()
        for flag in judgment.get("resolved_quality_flags", [])
        if str(flag).strip() in READY_BLOCKING_FLAGS
    }
    if explicit_flags:
        return explicit_flags
    return {
        str(flag).strip()
        for flag in candidate.get("quality_flags", [])
        if str(flag).strip() in READY_BLOCKING_FLAGS
    }


def _ready_state_guard_reasons(candidate: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    quality_flags = {str(flag) for flag in candidate.get("quality_flags", []) if str(flag).strip()}
    resolved_flags = _resolved_ready_blocking_flags(candidate)
    unresolved_flags = sorted(flag for flag in READY_BLOCKING_FLAGS if flag in quality_flags and flag not in resolved_flags)
    if unresolved_flags:
        labels = [READY_BLOCKING_FLAG_LABELS.get(flag, flag) for flag in unresolved_flags]
        reasons.append(f"未解消の注意信号={','.join(labels)}")
    return reasons


_CONFIDENCE_SORT_PRIORITY = {"strong": 0, "medium": 1, "weak": 2}


def _ready_candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int]:
    """Sort ready candidates by confidence tier (strong first), then total_packets descending."""
    conf = str(candidate.get("confidence") or "").strip()
    support = candidate.get("support") if isinstance(candidate.get("support"), dict) else {}
    total = int(support.get("total_packets", 0)) if support else 0
    return (_CONFIDENCE_SORT_PRIORITY.get(conf, 3), -total)


def build_proposal_sections(
    prepare_payload: dict[str, Any],
    judgments_by_candidate_id: dict[str, dict[str, Any]] | None = None,
    classifications_by_candidate_id: dict[str, dict[str, Any]] | None = None,
    *,
    markdown_classification_detail: bool = False,
) -> dict[str, Any]:
    judgments_by_candidate_id = judgments_by_candidate_id or {}
    classifications_by_candidate_id = classifications_by_candidate_id or {}
    ready: list[dict[str, Any]] = []
    needs_research: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for raw_candidate in prepare_payload.get("candidates", []):
        if not isinstance(raw_candidate, dict):
            continue
        candidate_id = str(raw_candidate.get("candidate_id") or "")
        candidate = merge_judgment_into_candidate(raw_candidate, judgments_by_candidate_id.get(candidate_id))
        candidate = merge_classification_into_candidate(candidate, classifications_by_candidate_id.get(candidate_id))
        split_children = _materialize_split_candidates(candidate)
        if split_children:
            for child in split_children:
                child_triage = str(child.get("triage_status") or "")
                if child_triage == "ready" and child.get("proposal_ready"):
                    child = _normalize_candidate_kind(child)
                    if str(child.get("suggested_kind") or "") == "skill":
                        scaffold_context = build_skill_scaffold_context(child)
                        child["skill_scaffold_context"] = scaffold_context
                        skill_handoff = build_skill_creator_handoff(scaffold_context)
                        merge_cross_repo_into_skill_handoff(skill_handoff, child, prepare_payload)
                        child["skill_creator_handoff"] = skill_handoff
                    elif str(child.get("suggested_kind") or "") in {"hook", "agent"}:
                        next_step_stub = build_next_step_stub(child)
                        if next_step_stub is not None:
                            child["next_step_stub"] = next_step_stub
                    ready.append(child)
                elif child_triage == "needs_research":
                    needs_research.append(child)
                else:
                    rejected.append(child)
            continue

        triage_status = str(candidate.get("triage_status") or "")

        if triage_status == "ready" and candidate.get("proposal_ready"):
            guard_reasons = _ready_state_guard_reasons(candidate)
            if guard_reasons:
                candidate["triage_status"] = "needs_research"
                candidate["proposal_ready"] = False
                candidate["confidence_reason"] = (
                    f"{candidate.get('confidence_reason', '')} "
                    f"(ready guard: {'; '.join(guard_reasons)} のため有望候補に留めました)"
                ).strip()
                triage_status = "needs_research"

        if triage_status == "ready" and candidate.get("proposal_ready"):
            candidate = _normalize_candidate_kind(candidate)
            if str(candidate.get("suggested_kind") or "") == "skill":
                scaffold_context = build_skill_scaffold_context(candidate)
                candidate["skill_scaffold_context"] = scaffold_context
                skill_handoff = build_skill_creator_handoff(scaffold_context)
                merge_cross_repo_into_skill_handoff(skill_handoff, candidate, prepare_payload)
                candidate["skill_creator_handoff"] = skill_handoff
            elif str(candidate.get("suggested_kind") or "") in {"hook", "agent"}:
                next_step_stub = build_next_step_stub(candidate)
                if next_step_stub is not None:
                    candidate["next_step_stub"] = next_step_stub
            ready.append(candidate)
        elif triage_status == "needs_research":
            needs_research.append(candidate)
        else:
            rejected.append(candidate)

    for packet in prepare_payload.get("unclustered", []):
        if isinstance(packet, dict):
            rejected.append(annotate_unclustered_packet(packet))

    ready.sort(key=_ready_candidate_sort_key)

    markdown = build_proposal_markdown(
        ready,
        needs_research,
        rejected,
        metadata=prepare_payload,
        markdown_classification_detail=markdown_classification_detail,
    )
    selection_prompt = "候補番号を入力すると /skill-creator による登録フローが始まります。選ばなかった候補は次回以降も引き続き提案されます。複数登録したい場合は 1 つずつ選択してください。" if ready else None
    decision_log_stub = [build_candidate_decision_stub(candidate) for candidate in ready + needs_research + rejected]
    learning_feedback = build_learning_feedback(
        ready,
        needs_research,
        rejected,
        metadata=prepare_payload,
    )
    observation_contract = build_observation_contract(prepare_payload)
    return {
        "ready": ready,
        "needs_research": needs_research,
        "rejected": rejected,
        "selection_prompt": selection_prompt,
        "markdown": markdown,
        "markdown_classification_detail": markdown_classification_detail,
        "decision_log_stub": decision_log_stub,
        "learning_feedback": learning_feedback,
        "observation_contract": observation_contract,
        "summary": {
            "ready_count": len(ready),
            "needs_research_count": len(needs_research),
            "rejected_count": len(rejected),
            "triaged_total": len(ready) + len(needs_research) + len(rejected),
        },
    }


def _observation_scope_line(metadata: dict[str, Any] | None) -> str:
    metadata = metadata or {}
    config = metadata.get("config") if isinstance(metadata.get("config"), dict) else {}
    sources = metadata.get("sources") if isinstance(metadata.get("sources"), list) else []

    workspace = config.get("workspace")
    workspace_label = "current workspace"
    if isinstance(workspace, str) and workspace.strip():
        workspace_label = Path(workspace).name or workspace
    elif config.get("all_sessions"):
        workspace_label = "all sessions"

    days = config.get("effective_days") or config.get("days") or "?"

    source_names: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        status = str(source.get("status") or "")
        if status and status != "success":
            continue
        name = str(source.get("name") or source.get("source") or "").strip()
        if name:
            source_names.append(name)
    source_label = ", ".join(source_names) if source_names else "source 不明"
    return f"観測範囲: {workspace_label} / 直近 {days}日間 / {source_label}"


def _triaged_candidate_count(
    ready: list[dict[str, Any]],
    needs_research: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
) -> int:
    """Rows after proposal triage (split materialization, unclustered → rejected). Canonical for chat headers."""
    return len(ready) + len(needs_research) + len(rejected)


def _no_ready_reason_summary(needs_research: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> str:
    if any("oversized_cluster" in (candidate.get("quality_flags") or []) for candidate in needs_research if isinstance(candidate, dict)):
        return "巨大クラスタが多く、用途が粗すぎて提案候補にしにくい"
    if needs_research:
        return "有望な候補はあるが、まだ意味のまとまりが弱い"
    if rejected:
        return "観測窓内の候補が単発または一般化不足だった"
    return "観測できる反復パターンがまだ少ない"


def _no_ready_next_step(metadata: dict[str, Any] | None) -> str:
    metadata = metadata or {}
    config = metadata.get("config") if isinstance(metadata.get("config"), dict) else {}
    days = config.get("effective_days") or config.get("days")
    if isinstance(days, int) and days <= 7:
        return "同じ workspace で 2-3 週間使い続けると、反復パターンが明確化しやすい"
    return "同じ作業パターンが数回たまったタイミングで再観測すると、候補が浮上しやすい"


def build_observation_contract(metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = metadata or {}
    config = metadata.get("config") if isinstance(metadata.get("config"), dict) else {}
    sources = metadata.get("sources") if isinstance(metadata.get("sources"), list) else []

    workspace = config.get("workspace")
    workspace_label = "current workspace"
    if isinstance(workspace, str) and workspace.strip():
        workspace_label = Path(workspace).name or workspace
    elif config.get("all_sessions"):
        workspace_label = "all sessions"

    successful_sources: list[str] = []
    degraded_sources: list[dict[str, str]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        name = str(source.get("name") or source.get("source") or "unknown").strip() or "unknown"
        status = str(source.get("status") or "").strip()
        if status in {"", "success"}:
            successful_sources.append(name)
            continue
        degraded_sources.append(
            {
                "name": name,
                "status": status or "unknown",
                "reason": str(source.get("reason") or source.get("message") or status or "unknown"),
            }
        )

    adaptive_window = config.get("adaptive_window") if isinstance(config.get("adaptive_window"), dict) else {}
    input_fidelity = str(config.get("input_fidelity") or "original")
    adaptive_expanded = bool(adaptive_window.get("expanded"))
    return {
        "mode": str(config.get("observation_mode") or ("all-sessions" if config.get("all_sessions") else "workspace")),
        "workspace_label": workspace_label,
        "days": config.get("effective_days") or config.get("days"),
        "successful_sources": successful_sources,
        "input_fidelity": input_fidelity,
        "degraded": input_fidelity == "approximate" or bool(degraded_sources),
        "degraded_sources": degraded_sources,
        "adaptive_window": {
            "enabled": bool(adaptive_window.get("enabled")),
            "expanded": adaptive_expanded,
            "reason": adaptive_window.get("reason"),
            "initial_days": adaptive_window.get("initial_days") or config.get("days"),
            "effective_days": config.get("effective_days") or config.get("days"),
            "fallback_days": adaptive_window.get("fallback_days"),
        },
    }


def _observation_stats_lines(metadata: dict[str, Any] | None) -> list[str]:
    """Build observation stats lines for enriched 0-candidate output."""
    metadata = metadata or {}
    summary = metadata.get("summary") if isinstance(metadata.get("summary"), dict) else {}
    config = metadata.get("config") if isinstance(metadata.get("config"), dict) else {}
    total_packets = summary.get("total_packets", 0)
    raw_candidates = metadata.get("candidates")
    cand_n = len(raw_candidates) if isinstance(raw_candidates, list) else 0
    ref_tc = summary.get("total_candidates")
    ref_n = int(ref_tc) if isinstance(ref_tc, int) else 0
    total_candidates = max(cand_n, ref_n)
    days = config.get("effective_days") or config.get("days") or "?"
    sources = metadata.get("sources") if isinstance(metadata.get("sources"), list) else []
    source_names = [
        str(s.get("name") or "")
        for s in sources
        if isinstance(s, dict) and str(s.get("status") or "").strip() in {"", "success"}
    ]
    lines = [
        f"観測サマリ: {total_packets}件のセッションを {days}日間にわたり観測し、{total_candidates}件のクラスタを検出",
    ]
    if source_names:
        lines.append(f"使用ソース: {', '.join(source_names)}")
    return lines


def _growth_signal_lines(needs_research: list[dict[str, Any]]) -> list[str]:
    """Build growth signal lines from needs_research candidates."""
    if not needs_research:
        return []
    lines = ["成長兆候（もう少しで提案に届く候補）:"]
    for candidate in needs_research[:3]:
        label = candidate.get("label", "unnamed")
        support = candidate.get("support") if isinstance(candidate.get("support"), dict) else {}
        total = int(support.get("total_packets", 0))
        lines.append(f"  - {label} ({total}回出現)")
    return lines


def _degraded_mode_lines(metadata: dict[str, Any] | None) -> list[str]:
    contract = build_observation_contract(metadata)
    adaptive_window = contract.get("adaptive_window", {})
    lines: list[str] = []

    if adaptive_window.get("expanded"):
        reason = str(adaptive_window.get("reason") or "unknown")
        initial_days = adaptive_window.get("initial_days") or contract.get("days")
        fallback_days = adaptive_window.get("fallback_days") or adaptive_window.get("effective_days") or contract.get("days")
        lines.append(
            f"注記: 観測窓を {initial_days}日 -> {fallback_days}日に自動拡張しました（reason: {reason}）"
        )

    if contract.get("input_fidelity") == "approximate":
        lines.append("注記: 入力の一部が近似復元データです。候補の確信度は保守的に扱ってください。")

    degraded_sources = [
        f"{str(source.get('name') or 'unknown')}({str(source.get('reason') or source.get('status') or 'unknown')})"
        for source in contract.get("degraded_sources", [])
        if isinstance(source, dict)
    ]
    if degraded_sources:
        lines.append("注記: 一部ソースが degraded です: " + ", ".join(sorted(degraded_sources)))

    return lines


def build_proposal_markdown(
    ready: list[dict[str, Any]],
    needs_research: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    *,
    metadata: dict[str, Any] | None = None,
    markdown_classification_detail: bool = False,
) -> str:
    lines: list[str] = []
    lines.append("### 観測範囲")
    lines.append(_observation_scope_line(metadata))
    degraded_lines = _degraded_mode_lines(metadata)
    if degraded_lines:
        lines.extend(degraded_lines)
    lines.append("")
    triaged_total = _triaged_candidate_count(ready, needs_research, rejected)
    lines.append(
        f"> 候補内訳: 適用 {len(ready)} / 追加観測 {len(needs_research)} / 観測ノート {len(rejected)}（合計 {triaged_total}）"
    )
    lines.append("")
    lines.append("## 提案（アクション候補）")
    if ready:
        for index, candidate in enumerate(ready, start=1):
            lines.extend(
                proposal_item_lines(
                    index,
                    candidate,
                    include_classification=True,
                    markdown_classification_detail=markdown_classification_detail,
                )
            )
    else:
        lines.append("今回は有力候補なし")
        detected_count = _triaged_candidate_count(ready, needs_research, rejected)
        lines.append("")
        lines.extend(_observation_stats_lines(metadata))
        lines.append(f"検出候補数: {detected_count}件中 0 件が提案条件を満たした")
        growth_lines = _growth_signal_lines(needs_research)
        if growth_lines:
            lines.extend(growth_lines)
        lines.append(f"見送り理由の傾向: {_no_ready_reason_summary(needs_research, rejected)}")
        lines.append(f"候補が増える条件: {_no_ready_next_step(metadata)}")
        first_split_candidate = next((candidate for candidate in needs_research if candidate_split_suggestions(candidate)), None)
        if isinstance(first_split_candidate, dict):
            lines.append(
                "次に育てやすい候補: "
                f"{first_split_candidate.get('label', 'candidate')} -> {', '.join(candidate_split_suggestions(first_split_candidate)[:3])}"
            )

    lines.append("")
    lines.append("## 有望候補（もう少し観測が必要）")
    if needs_research:
        for index, candidate in enumerate(needs_research, start=1):
            lines.extend(proposal_item_lines(index, candidate, include_classification=False))
    else:
        lines.append("なし")

    lines.append("")
    lines.append("## 観測ノート")
    if rejected:
        for index, candidate in enumerate(rejected[:5], start=1):
            lines.extend(rejected_item_lines(index, candidate))
    else:
        lines.append("なし")

    if ready:
        lines.append("")
        lines.append("候補番号を入力すると /skill-creator による登録フローが始まります。選ばなかった候補は次回以降も引き続き提案されます。複数登録したい場合は 1 つずつ選択してください。")
    return "\n".join(lines)


def build_skill_scaffold_summary_lines(candidate: dict[str, Any]) -> list[str]:
    context = candidate.get("skill_scaffold_context")
    if not isinstance(context, dict):
        return []

    lines: list[str] = []
    goal = str(context.get("goal") or "").strip()
    if goal:
        lines.append(f"   scaffold goal: {goal}")

    summary_parts: list[str] = []
    artifact_hints = [str(item).strip() for item in context.get("artifact_hints", []) if str(item).strip()]
    rule_hints = [str(item).strip() for item in context.get("rule_hints", []) if str(item).strip()]
    observation_count = context.get("observation_count")
    if artifact_hints:
        summary_parts.append(f"成果物={', '.join(artifact_hints[:2])}")
    if rule_hints:
        summary_parts.append(f"ルール={', '.join(rule_hints[:2])}")
    if isinstance(observation_count, int) and observation_count > 0:
        summary_parts.append(f"観測={observation_count}回")
    if summary_parts:
        lines.append(f"   scaffold要点: {' / '.join(summary_parts)}")

    representative_examples = [
        summarize_text(str(item).strip(), 100)
        for item in context.get("representative_examples", [])
        if str(item).strip()
    ]
    if representative_examples:
        lines.append(f"   scaffold例: {representative_examples[0]}")
    return lines


_KIND_DISPLAY_LABELS: dict[str, str] = {
    "CLAUDE.md": "種類: プロジェクト設定（CLAUDE.md）",
    "skill": "種類: 再利用スキル",
    "hook": "種類: 自動チェック（hook）",
    "agent": "種類: 専用エージェント",
}

_KIND_ACTION_LINES: dict[str, tuple[str, str]] = {
    "CLAUDE.md": (
        "プロジェクト設定に追加すれば、毎回の指示が不要になります",
        "すぐに CLAUDE.md に追加できます",
    ),
    "skill": (
        "再利用コマンドとして保存すれば、同じ作業を素早く再現できます",
        "/skill-creator で生成できます",
    ),
    "hook": (
        "自動チェックとして設定すれば、手動確認が不要になります",
        "この場で hook を作成できます",
    ),
    "agent": (
        "専用エージェントとして作成すれば、この役割を任せられます",
        "この場でエージェントを作成できます",
    ),
}


def proposal_item_lines(
    index: int,
    candidate: dict[str, Any],
    *,
    include_classification: bool,
    markdown_classification_detail: bool = False,
) -> list[str]:
    # display_label: LLM が生成する表示専用ラベル。なければ Python の label にフォールバック。
    _display = (candidate.get("display_label") or candidate.get("label") or "Unnamed candidate").strip()
    # cross-repo 候補は title 行にマーカーを付ける（skill 種別のみ）
    _handoff_meta = candidate.get("skill_creator_handoff")
    _is_cross_repo = (
        isinstance(_handoff_meta, dict)
        and bool(_handoff_meta.get("cross_repo"))
        and str(candidate.get("suggested_kind") or "") == "skill"
    )
    _title = f"{_display}  ＊別リポジトリ向け" if _is_cross_repo else _display
    lines = [f"{index}. {_title}"]
    if include_classification:
        kind = str(candidate.get("suggested_kind") or "TBD").strip()
        kind_label = _KIND_DISPLAY_LABELS.get(kind, f"種類: {kind}")
        lines.append(f"   {kind_label}")
        source = str(candidate.get("suggested_kind_source") or "").strip()
        if source in {"llm", "guardrail_override"}:
            trace = candidate.get("classification_trace")
            if markdown_classification_detail and isinstance(trace, list):
                trace_parts = []
                for item in trace:
                    if not isinstance(item, dict):
                        continue
                    stage = str(item.get("stage") or "").strip()
                    kind = str(item.get("kind") or "").strip()
                    if stage and kind:
                        trace_parts.append(f"{stage}={kind}")
                if trace_parts:
                    lines.append(f"   分類トレース: {' / '.join(trace_parts)}")
            reason = str(candidate.get("suggested_kind_reason") or "").strip()
            if markdown_classification_detail:
                if reason:
                    lines.append(f"   分類理由: {reason}")
            else:
                kind = str(candidate.get("suggested_kind") or "").strip() or "?"
                src_label = "llm" if source == "llm" else "guardrail"
                short = reason if len(reason) <= 120 else (reason[:117] + "...")
                lines.append(f"   分類: 最終={kind}（{src_label}）" + (f" — {short}" if short else ""))
        handoff_scope = candidate.get("skill_creator_handoff")
        if str(candidate.get("suggested_kind") or "") == "skill" and isinstance(handoff_scope, dict):
            if handoff_scope.get("cross_repo"):
                lines.append("   適用先: 別リポジトリ（現在の CWD ではなく、handoff の target repo で /skill-creator）")
            elif str(handoff_scope.get("handoff_scope") or "") == "current_repo":
                lines.append("   適用先: 現在のリポジトリ")
            res_note = str(handoff_scope.get("workspace_resolution_note") or "").strip()
            if res_note and len(res_note) <= 200:
                lines.append(f"   workspace 注記: {res_note}")
    _CONFIDENCE_LABELS = {
        "strong": "確度: 高い — 複数セッション・複数ソースで繰り返し観測",
        "medium": "確度: 中程度 — 複数セッションで出現、もう少し定着を見たい",
        "weak": "確度: まだ弱い — 出現回数が少なく、今後の観測次第",
    }
    _conf = candidate.get("confidence", "")
    _conf_label = _CONFIDENCE_LABELS.get(_conf)
    if _conf_label is None:
        _conf_display = candidate.get("confidence", "不明")
        _conf_label = f"確度: {_conf_display}"
    # display_confidence_reason: LLM が生成する候補固有の確度説明。
    # 存在する場合はテンプレの "— …" 部分を置換する。
    _display_conf_reason = str(candidate.get("display_confidence_reason") or "").strip()
    if _display_conf_reason:
        _conf_prefix = _conf_label.split(" — ")[0]  # "確度: 高い" などのプレフィックス
        _conf_label = f"{_conf_prefix} — {_display_conf_reason}"
    _prior_state = candidate.get("prior_decision_state") if isinstance(candidate.get("prior_decision_state"), dict) else {}
    _prior_obs = int(_prior_state.get("observation_count", 0)) if _prior_state else 0
    _support_for_delta = candidate.get("support") if isinstance(candidate.get("support"), dict) else {}
    _current_obs = int(_support_for_delta.get("total_packets", 0)) if _support_for_delta else 0
    _obs_delta = _current_obs - _prior_obs if _prior_obs > 0 else 0
    if _obs_delta > 0:
        _conf_label = f"{_conf_label}（前回比 +{_obs_delta} 観測）"
    lines.append(f"   {_conf_label}")
    support = candidate.get("support") if isinstance(candidate.get("support"), dict) else {}
    total_packets = support.get("total_packets")
    claude_packets = int(support.get("claude_packets", 0)) if support else 0
    codex_packets = int(support.get("codex_packets", 0)) if support else 0
    source_count = int(claude_packets > 0) + int(codex_packets > 0)
    if not include_classification and isinstance(total_packets, int) and total_packets > 0:
        source_label = f"{source_count}ソース" if source_count > 0 else "source 不明"
        lines.append(f"   出現: {total_packets}回 / {source_label}")
    lines.extend(build_evidence_chain_lines(candidate))
    constraints = _candidate_text_list(candidate, "constraints", limit=MAX_CONSTRAINT_ITEMS)
    acceptance_criteria = _candidate_text_list(candidate, "acceptance_criteria", limit=MAX_ACCEPTANCE_CRITERIA_ITEMS)
    intent_trace = _candidate_text_list(candidate, "intent_trace", limit=MAX_INTENT_TRACE_ITEMS)
    if intent_trace:
        lines.append(f"   意図トレース: {' / '.join(intent_trace[:2])}")
    if constraints:
        lines.append(f"   制約: {' / '.join(constraints[:2])}")
    if acceptance_criteria:
        lines.append(f"   受け入れ条件: {' / '.join(acceptance_criteria[:2])}")
    contamination_signals = [str(item).strip() for item in candidate.get("contamination_signals", []) if str(item).strip()]
    origin_hint = str(candidate.get("origin_hint") or "").strip()
    user_signal_strength = str(candidate.get("user_signal_strength") or "").strip()
    if contamination_signals or origin_hint in {"parent_ai", "mixed", "unknown"} or user_signal_strength in {"medium", "low"}:
        note_parts: list[str] = []
        if origin_hint:
            note_parts.append(f"origin={origin_hint}")
        if user_signal_strength:
            note_parts.append(f"user_signal={user_signal_strength}")
        if contamination_signals:
            note_parts.append(f"signals={','.join(contamination_signals[:4])}")
        lines.append(f"   注記: {' / '.join(note_parts)}")
    judgment = candidate.get("research_judgment")
    if include_classification:
        handoff = candidate.get("skill_creator_handoff")
        if str(candidate.get("suggested_kind") or "") == "skill" and isinstance(handoff, dict):
            lines.extend(build_skill_scaffold_summary_lines(candidate))
            invocation = str(handoff.get("suggested_invocation") or "").strip()
            if invocation:
                lines.append(f"   公式 handoff: {invocation}")
        next_step_stub = candidate.get("next_step_stub")
        if isinstance(next_step_stub, dict):
            lines.append(f"   次ステップ: {next_step_stub.get('prompt', '')}")
            if str(next_step_stub.get("kind") or "") == "hook":
                lines.append(
                    f"   stub: trigger={next_step_stub.get('trigger_event', 'n/a')} / action={next_step_stub.get('action_summary', 'n/a')}"
                )
            elif str(next_step_stub.get("kind") or "") == "agent":
                lines.append(f"   stub: role={next_step_stub.get('role_summary', 'n/a')}")
        resolved_flags = [
            READY_BLOCKING_FLAG_LABELS.get(str(flag).strip(), str(flag).strip())
            for flag in candidate.get("resolved_quality_flags", [])
            if str(flag).strip()
        ]
        if resolved_flags:
            lines.append(f"   追加調査で確認済み: {', '.join(resolved_flags[:4])}")
        _kind = str(candidate.get("suggested_kind") or "").strip()
        _action = _KIND_ACTION_LINES.get(_kind)
        if _action:
            lines.append(f"   効果: {_action[0]}")
            lines.append(f"   → {_action[1]}")
        else:
            lines.append(f"   効果: {candidate.get('label', 'この候補')} を再利用可能にできます")
    elif isinstance(judgment, dict):
        lines.append(f"   現状: {judgment.get('summary', candidate.get('confidence_reason', '追加調査が必要'))}")
        if candidate_split_suggestions(candidate):
            lines.append(f"   分割候補: {', '.join(candidate_split_suggestions(candidate)[:3])}")
        lines.append("   次のステップ: 追加でログをためて再観測し、分割が必要か判断する")
    else:
        lines.append(f"   現状: {candidate.get('confidence_reason', '追加調査が必要')}")
        if candidate_split_suggestions(candidate):
            lines.append(f"   分割候補: {', '.join(candidate_split_suggestions(candidate)[:3])}")
        lines.append("   次のステップ: 1-2 週間ほど運用してから再観測し、意味のまとまりを確認する")
    return lines


def rejected_item_lines(index: int, candidate: dict[str, Any]) -> list[str]:
    label = candidate.get("label") or candidate.get("primary_intent") or candidate.get("packet_id") or "reference item"
    reason = candidate.get("confidence_reason") or candidate.get("evidence_summary") or "根拠不足"
    return [
        f"{index}. {label}",
        f"   理由: {reason}",
    ]


def build_evidence_chain_lines(candidate: dict[str, Any]) -> list[str]:
    lines = ["   根拠:"]
    evidence_items = candidate.get("evidence_items")
    if not isinstance(evidence_items, list) or not evidence_items:
        fallback = str(candidate.get("evidence_summary") or "n/a")
        lines.append(f"   - {fallback}")
        return lines

    for item in evidence_items[:3]:
        if not isinstance(item, dict):
            continue
        timestamp = str(item.get("timestamp") or "").strip()
        source = str(item.get("source") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not summary:
            summary = "summary unavailable"
        prefix_parts = [value for value in (timestamp, source) if value]
        prefix = " ".join(prefix_parts)
        if prefix:
            lines.append(f"   - {prefix}: {summary}")
        else:
            lines.append(f"   - {summary}")
    return lines


def recent_packet_count(timestamps: list[str], latest_timestamp: str | None) -> int:
    if not latest_timestamp:
        return 0
    latest = ensure_datetime(latest_timestamp)
    if latest is None:
        return 0
    threshold = latest.timestamp() - (7 * 24 * 60 * 60)
    count = 0
    for timestamp in timestamps:
        current = ensure_datetime(timestamp)
        if current and current.timestamp() >= threshold:
            count += 1
    return count


def build_skill_scaffold_context(candidate: dict[str, Any]) -> dict[str, Any]:
    """Build structured context for skill scaffold draft generation.

    This provides the input for skill-creator to generate a SKILL.md draft.
    DayTrace produces the context; skill-creator produces the skill.
    """
    label = str(candidate.get("label") or "Unnamed skill")
    task_shapes = [str(s) for s in candidate.get("common_task_shapes", candidate.get("task_shape", [])) if s]
    artifact_hints = [str(a) for a in candidate.get("artifact_hints", []) if a]
    rule_hints = [str(r) for r in candidate.get("rule_hints", []) if r]
    intent_trace = _candidate_text_list(candidate, "intent_trace", limit=MAX_INTENT_TRACE_ITEMS)
    constraints = _candidate_text_list(candidate, "constraints", limit=MAX_CONSTRAINT_ITEMS)
    acceptance_criteria = _candidate_text_list(candidate, "acceptance_criteria", limit=MAX_ACCEPTANCE_CRITERIA_ITEMS)
    representative_examples = [str(e) for e in candidate.get("representative_examples", []) if e]
    evidence_items = candidate.get("evidence_items", [])
    support = candidate.get("support") if isinstance(candidate.get("support"), dict) else {}

    goal = f"{label} を再利用可能なスキルとして保存する"
    if task_shapes:
        goal = f"{task_shapes[0].replace('_', ' ')} ベースの {label} を再利用可能なスキルとして保存する"

    execution_hints: list[str] = []
    if artifact_hints:
        execution_hints.append(f"成果物タイプ: {', '.join(artifact_hints)}")
    if rule_hints:
        execution_hints.append(f"適用ルール: {', '.join(rule_hints)}")

    evidence_summaries = []
    for item in (evidence_items or [])[:3]:
        if isinstance(item, dict):
            summary = str(item.get("summary") or "").strip()
            if summary:
                evidence_summaries.append(summary)

    return {
        "skill_name": _skill_slug(label),
        "goal": goal,
        "task_shapes": task_shapes,
        "artifact_hints": artifact_hints,
        "rule_hints": rule_hints,
        "intent_trace": intent_trace,
        "constraints": constraints,
        "acceptance_criteria": acceptance_criteria,
        "execution_hints": execution_hints,
        "representative_examples": representative_examples[:3],
        "evidence_summaries": evidence_summaries,
        "observation_count": int(support.get("total_packets", 0)),
        "source_diversity": int(support.get("claude_packets", 0) > 0) + int(support.get("codex_packets", 0) > 0),
    }


def workspace_matches(candidate: str | None, workspace: Path | None) -> bool:
    if workspace is None:
        return True
    return is_within_path(candidate, workspace)
