#!/usr/bin/env python3
"""Mechanical artifact formatter for DayTrace outputs.

Responsibilities (Python side — deterministic, no LLM):
  - path_sanitize:           replace /Users/…/ absolute paths with [PATH]
  - normalize_source_names:  canonicalize source identifiers to display names
  - check_forbidden_words:   detect prohibited vocabulary (warn-only)
  - check_english_leakage:   detect internal English phrases in Japanese output (warn-only)
  - inject_mixed_scope_note: prepend scope note when scope_mode == "mixed"
  - inject_footer:           append 再構成元 footer (mode-dependent)

Responsibilities (LLM / SKILL.md side — semantic, not here):
  - action-level vocabulary conversion
  - tone adjustment (share vs private)
  - fact / assumption distinction
  - forbidden word natural replacement

Usage:
    result = ArtifactFormatter().apply(FormatterInput(raw_text=..., mode="report-share", ...))
    print(result.text)
    if result.warnings:
        print("warnings:", result.warnings)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

# ---------------------------------------------------------------------------
# Source name normalization mapping
# Canonical list — keep in sync with daytrace-session/SKILL.md source table.
# ---------------------------------------------------------------------------
SOURCE_NAME_MAP: dict[str, str] = {
    "git-history": "Git の変更履歴",
    "claude-history": "Claude の会話ログ",
    "codex-history": "Codex の会話ログ",
    "chrome-history": "ブラウザの閲覧ログ",
    "workspace-file-activity": "workspace のファイル作業痕跡",
}

# ---------------------------------------------------------------------------
# Forbidden word definitions
# Two categories:
#   INTERNAL_TERMS  — internal state / trace identifiers that must not appear in output
#   PRODUCT_COPY    — prohibited product-copy vocabulary (from post-draft/SKILL.md)
# Both are warn-only; replacement is the LLM's responsibility.
# ---------------------------------------------------------------------------
FORBIDDEN_INTERNAL_TERMS: tuple[str, ...] = (
    "candidate_id",
    "triage_status",
    "internal state",
    "internal trace",
    "classification_trace",
    "suggested_kind",
    "Continuing autonomously",
)

FORBIDDEN_PRODUCT_COPY: tuple[str, ...] = (
    "寄り道",
    "今日の重心",
    "実装密度の高い1日",
    "ハッカソン提出を控え",
)

# English phrases that indicate internal reasoning leakage
FORBIDDEN_ENGLISH_LEAKAGE: tuple[str, ...] = (
    "Continuing autonomously",
    "Let me think",
    "I will now",
    "Step 1:",
    "Step 2:",
    "Step 3:",
    "Based on the above",
    "In summary,",
)

# Regex pattern for absolute user paths  (/Users/<name>/...)
_PATH_PATTERN = re.compile(r"/Users/[^/\s,\"'`\])\n]+(?:/[^\s,\"'`\])\n]*)*")


# ---------------------------------------------------------------------------
# Shape definitions
# ---------------------------------------------------------------------------

@dataclass
class FormatterInput:
    raw_text: str
    mode: str = "report-private"
    """One of: report-private | report-share | post-draft | proposal"""
    scope_mode: str = "single"
    """One of: single | mixed"""
    sources: list[str] = field(default_factory=list)
    session_date: str | None = None


@dataclass
class Patch:
    """Audit record of a single substitution applied by the formatter."""
    kind: str
    """E.g. 'path_sanitize', 'source_normalize'."""
    original: str
    replacement: str
    count: int = 1


@dataclass
class FormatterResult:
    text: str
    warnings: list[str] = field(default_factory=list)
    patches: list[Patch] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

class ArtifactFormatter:
    """Apply all mechanical transforms to a raw artifact string."""

    def apply(self, inp: FormatterInput) -> FormatterResult:
        text = inp.raw_text
        warnings: list[str] = []
        patches: list[Patch] = []

        text, _patches = self.path_sanitize(text)
        patches.extend(_patches)

        text, _patches = self.normalize_source_names(text)
        patches.extend(_patches)

        _warns = self.check_forbidden_words(text)
        warnings.extend(_warns)

        _warns = self.check_english_leakage(text)
        warnings.extend(_warns)

        text = self.inject_mixed_scope_note(text, inp)

        text = self.inject_footer(text, inp)

        return FormatterResult(text=text, warnings=warnings, patches=patches)

    # ------------------------------------------------------------------
    # Individual transforms
    # ------------------------------------------------------------------

    def path_sanitize(self, text: str) -> tuple[str, list[Patch]]:
        """Replace absolute /Users/... paths with [PATH].

        Returns the sanitized text and a list of Patch records (one per
        unique path found).
        """
        found: dict[str, int] = {}
        for match in _PATH_PATTERN.finditer(text):
            found[match.group()] = found.get(match.group(), 0) + 1

        patches: list[Patch] = []
        for original, count in found.items():
            text = text.replace(original, "[PATH]")
            patches.append(Patch(kind="path_sanitize", original=original, replacement="[PATH]", count=count))

        return text, patches

    def normalize_source_names(self, text: str) -> tuple[str, list[Patch]]:
        """Replace raw source identifiers with canonical display names."""
        patches: list[Patch] = []
        for raw, display in SOURCE_NAME_MAP.items():
            count = text.count(raw)
            if count:
                text = text.replace(raw, display)
                patches.append(Patch(kind="source_normalize", original=raw, replacement=display, count=count))
        return text, patches

    def check_forbidden_words(self, text: str) -> list[str]:
        """Return warnings for any prohibited vocabulary found in text."""
        warnings: list[str] = []
        for term in FORBIDDEN_INTERNAL_TERMS + FORBIDDEN_PRODUCT_COPY:
            if term in text:
                warnings.append(f"forbidden_word: '{term}' が出力に含まれています")
        return warnings

    def check_english_leakage(self, text: str) -> list[str]:
        """Return warnings for internal English phrases found in text."""
        warnings: list[str] = []
        for phrase in FORBIDDEN_ENGLISH_LEAKAGE:
            if phrase in text:
                warnings.append(f"english_leakage: '{phrase}' が出力に含まれています")
        return warnings

    def inject_mixed_scope_note(self, text: str, inp: FormatterInput) -> str:
        """Prepend a mixed-scope note when scope_mode is 'mixed'."""
        if inp.scope_mode != "mixed":
            return text
        note_lines = [
            "> ⚠️ **混在スコープ**: この出力は複数のワークスペースまたはセッションにまたがる情報を含みます。",
            "> 内容の適用範囲をご確認ください。",
            "",
        ]
        return "\n".join(note_lines) + text

    def inject_footer(self, text: str, inp: FormatterInput) -> str:
        """Append 再構成元 footer.

        - report-share: minimal footer (no raw source list)
        - report-private / post-draft / proposal: full source list
        """
        if not inp.sources and not inp.session_date:
            return text

        if inp.mode == "report-share":
            footer_lines = [
                "",
                "---",
                "_この日報は DayTrace により自動生成されました。_",
            ]
        else:
            parts: list[str] = []
            if inp.session_date:
                parts.append(f"観測日: {inp.session_date}")
            if inp.sources:
                normalized = [SOURCE_NAME_MAP.get(s, s) for s in inp.sources]
                parts.append(f"再構成元: {' / '.join(normalized)}")
            footer_lines = ["", "---"] + [f"_{p}_" for p in parts]

        return text + "\n".join(footer_lines)


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def format_artifact(
    raw_text: str,
    *,
    mode: str = "report-private",
    scope_mode: str = "single",
    sources: Sequence[str] | None = None,
    session_date: str | None = None,
) -> FormatterResult:
    """Shorthand for ArtifactFormatter().apply(FormatterInput(...))."""
    return ArtifactFormatter().apply(
        FormatterInput(
            raw_text=raw_text,
            mode=mode,
            scope_mode=scope_mode,
            sources=list(sources) if sources else [],
            session_date=session_date,
        )
    )
