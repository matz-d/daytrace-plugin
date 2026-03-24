#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from common import emit, error_response
from skill_miner_common import (
    DEFAULT_DECISION_LOG_PATH,
    DEFAULT_SKILL_CREATOR_HANDOFF_DIR,
    PROPOSAL_SOURCE,
    build_classification_target_candidates,
    build_evidence_chain_lines,
    build_proposal_markdown as build_markdown,
    build_proposal_sections,
    proposal_item_lines,
    rejected_item_lines,
)
VALID_USER_DECISIONS = {"adopt", "defer", "reject"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build proposal sections from skill-miner prepare output and optional research judgments.")
    parser.add_argument("--prepare-file", required=True, help="Path to the JSON file produced by skill_miner_prepare.py.")
    parser.add_argument("--judge-file", action="append", default=[], help="Path to a JSON file produced by skill_miner_research_judge.py.")
    parser.add_argument("--classification-file", action="append", default=[], help="Path to a JSON file with classification overlay for one candidate.")
    parser.add_argument("--decision-log-path", help="Optional JSONL path to persist decision_log_stub entries.")
    parser.add_argument("--skill-creator-handoff-dir", help="Optional directory to persist JSON skill-creator handoff bundles (context + handoff metadata).")
    parser.add_argument("--user-decision-file", help="Optional JSON file with user decisions to persist alongside decision stubs.")
    parser.add_argument(
        "--markdown-classification-detail",
        action="store_true",
        help="Include full classification trace and per-stage reasons in proposal markdown (default: compact one-line summary).",
    )
    parser.add_argument(
        "--classification-targets-only",
        action="store_true",
        help="Emit only the candidate list that should receive classification overlays, after prepare+judge merge.",
    )
    return parser


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def load_judgments(paths: list[str]) -> dict[str, dict[str, Any]]:
    judgments: dict[str, dict[str, Any]] = {}
    for raw_path in paths:
        payload = load_json(Path(raw_path).expanduser().resolve())
        candidate_id = payload.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id:
            judgments[candidate_id] = payload
    return judgments


def load_classification_overlays(paths: list[str]) -> dict[str, dict[str, Any]]:
    overlays: dict[str, dict[str, Any]] = {}
    for raw_path in paths:
        try:
            payload = load_json(Path(raw_path).expanduser().resolve())
        except (OSError, ValueError, json.JSONDecodeError):
            # Malformed or unreadable overlay: skip so proposal falls back to heuristic for that candidate.
            continue
        candidate_id = payload.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id:
            overlays[candidate_id] = payload
    return overlays


def load_user_decisions(path: str | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    payload = load_json(Path(path).expanduser().resolve())
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        return {}

    overlays: dict[str, dict[str, Any]] = {}
    for item in decisions:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or "").strip()
        user_decision = str(item.get("user_decision") or "").strip()
        if not candidate_id or user_decision not in VALID_USER_DECISIONS:
            continue
        carry_forward = item.get("carry_forward")
        if not isinstance(carry_forward, bool):
            carry_forward = user_decision != "adopt"
        overlays[candidate_id] = {
            "user_decision": user_decision,
            "user_decision_timestamp": item.get("user_decision_timestamp"),
            "carry_forward": carry_forward,
        }
    return overlays


def resolve_output_path(value: str | None, *, default: Path) -> Path:
    return Path(value).expanduser().resolve() if value else default.resolve()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")


def recorded_at_iso() -> str:
    return datetime.now().astimezone().isoformat()


def decision_log_rows(proposal: dict[str, Any], prepare_payload: dict[str, Any], *, recorded_at: str) -> list[dict[str, Any]]:
    config = prepare_payload.get("config", {}) if isinstance(prepare_payload.get("config"), dict) else {}
    observation_mode = str(config.get("observation_mode") or ("all-sessions" if config.get("all_sessions") else "workspace"))
    workspace = config.get("workspace")
    effective_days = config.get("effective_days", config.get("days"))
    rows: list[dict[str, Any]] = []
    for item in proposal.get("decision_log_stub", []):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "record_type": "skill_miner_decision_stub",
                "recorded_at": recorded_at,
                "workspace": workspace,
                "observation_mode": observation_mode,
                "effective_days": effective_days,
                "candidate_id": item.get("candidate_id"),
                "decision_key": item.get("decision_key"),
                "content_key": item.get("content_key"),
                "label": item.get("label"),
                "recommended_action": item.get("recommended_action"),
                "triage_status": item.get("triage_status"),
                "suggested_kind": item.get("suggested_kind"),
                "reason_codes": item.get("reason_codes", []),
                "split_suggestions": item.get("split_suggestions", []),
                "intent_trace": item.get("intent_trace", []),
                "constraints": item.get("constraints", []),
                "acceptance_criteria": item.get("acceptance_criteria", []),
                "user_decision": item.get("user_decision"),
                "user_decision_timestamp": item.get("user_decision_timestamp"),
                "carry_forward": item.get("carry_forward", True),
                "observation_count": item.get("observation_count", 0),
                "prior_observation_count": item.get("prior_observation_count", 0),
                "observation_delta": item.get("observation_delta", 0),
            }
        )
    return rows


def apply_user_decisions(
    proposal: dict[str, Any],
    overlays_by_candidate_id: dict[str, dict[str, Any]],
    *,
    recorded_at: str,
) -> dict[str, Any]:
    if not overlays_by_candidate_id:
        return {
            "applied": 0,
            "matched_candidate_ids": [],
            "unmatched_candidate_ids": [],
        }

    matched_candidate_ids: list[str] = []

    for item in proposal.get("decision_log_stub", []):
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or "").strip()
        overlay = overlays_by_candidate_id.get(candidate_id)
        if not overlay:
            continue
        matched_candidate_ids.append(candidate_id)
        item["user_decision"] = overlay["user_decision"]
        item["user_decision_timestamp"] = overlay.get("user_decision_timestamp") or recorded_at
        item["carry_forward"] = overlay.get("carry_forward", True)

    for section in ("ready", "needs_research", "rejected"):
        candidates = proposal.get(section, [])
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            candidate_id = str(candidate.get("candidate_id") or candidate.get("packet_id") or "").strip()
            overlay = overlays_by_candidate_id.get(candidate_id)
            if not overlay:
                continue
            candidate["user_decision"] = overlay["user_decision"]
            candidate["user_decision_timestamp"] = overlay.get("user_decision_timestamp") or recorded_at
            candidate["carry_forward"] = overlay.get("carry_forward", True)

    matched_set = set(matched_candidate_ids)
    return {
        "applied": len(matched_set),
        "matched_candidate_ids": sorted(matched_set),
        "unmatched_candidate_ids": sorted(candidate_id for candidate_id in overlays_by_candidate_id if candidate_id not in matched_set),
    }


def persist_decision_log_stub(
    proposal: dict[str, Any],
    prepare_payload: dict[str, Any],
    *,
    decision_log_path: Path,
    recorded_at: str,
) -> dict[str, Any]:
    rows = decision_log_rows(proposal, prepare_payload, recorded_at=recorded_at)
    if not rows:
        return {
            "attempted": False,
            "status": "skipped",
            "reason": "no_entries",
            "path": str(decision_log_path),
            "entries_written": 0,
        }
    try:
        append_jsonl(decision_log_path, rows)
    except Exception as exc:
        return {
            "attempted": True,
            "status": "failed",
            "path": str(decision_log_path),
            "entries_written": 0,
            "message": str(exc),
        }
    return {
        "attempted": True,
        "status": "persisted",
        "path": str(decision_log_path),
        "entries_written": len(rows),
    }


def safe_slug(value: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in value)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")
    return slug[:64] or "skill-handoff"


def finalize_skill_handoff_presentation(handoff: dict[str, Any], context_file: str) -> None:
    """Fill presentation_block after context_file path is known (schema v2 UX)."""
    cross = bool(handoff.get("cross_repo"))
    target = str(handoff.get("target_workspace_hint") or "").strip() or "（ログの workspace を確認）"
    current = handoff.get("current_workspace")
    current_label = str(current).strip() if current else "（未設定 / 全日程観測）"
    mode = str(handoff.get("suggested_operation_mode") or "").strip()
    mode_reason = str(handoff.get("operation_mode_reason") or "").strip()
    lines = ["```text"]
    if cross:
        lines.append("この候補は別リポジトリ向けです。現在の CWD ではなく、対象リポジトリを開いてから適用してください。")
    else:
        lines.append("この候補は観測したリポジトリ向けです。開いているプロジェクトが一致しているか確認してください。")
    lines.extend(
        [
            "",
            f"target repo: {target}",
            f"current workspace (観測時 --workspace): {current_label}",
            f"handoff file: {context_file}",
            *( [f"suggested next operation: {mode}"] if mode else [] ),
            *( [f"reason: {mode_reason}"] if mode_reason else [] ),
            "",
            "実行すること:",
        ]
    )
    if cross:
        lines.extend(
            [
                "1. 対象リポジトリを開く（上記 target repo）",
                "2. handoff を正本扱いせず、repo の生ファイル・実データを先に確認する",
                "3. 既存 artifact がある場合は保守・更新 skill として再設計してよい",
                "4. そのルートで /skill-creator を実行する",
                "5. この JSON の context は scaffold としてプロンプトに含める",
            ]
        )
    else:
        lines.extend(
            [
                "1. 適用先リポジトリを開く",
                "2. handoff を正本扱いせず、repo の生ファイル・実データを先に確認する",
                "3. 既存 artifact がある場合は保守・更新 skill として再設計してよい",
                "4. /skill-creator を実行し、この handoff の scaffold を渡す",
            ]
        )
    cd_hint = handoff.get("target_workspace_hint")
    if cross and isinstance(cd_hint, str) and cd_hint.startswith("/"):
        lines.extend(["", f"例: cd {cd_hint}"])
    copy_prompt = (
        f"/skill-creator {context_file} 以下の内容をもとにスキルを作成してください。"
        " handoff は正本ではないため、先に適用先 repo の生ファイルと実データを確認し、"
        "既存 artifact がある場合は保守・更新 skill として再設計してください。"
        " 曖昧な部分は ask_user で確認してください。"
    )
    lines.extend(["", "この文言をコピーして使ってください:", copy_prompt])
    lines.append("```")
    handoff["presentation_block"] = "\n".join(lines)


def persist_skill_creator_handoffs(
    proposal: dict[str, Any],
    *,
    handoff_dir: Path,
    recorded_at: str,
) -> dict[str, Any]:
    ready_candidates = proposal.get("ready", [])
    if not isinstance(ready_candidates, list):
        ready_candidates = []
    persisted: list[dict[str, Any]] = []
    try:
        ensure_dir(handoff_dir)
        for index, candidate in enumerate(ready_candidates, start=1):
            if not isinstance(candidate, dict):
                continue
            context = candidate.get("skill_scaffold_context")
            handoff = candidate.get("skill_creator_handoff")
            if not isinstance(context, dict) or not isinstance(handoff, dict):
                continue
            skill_name = str(context.get("skill_name") or candidate.get("label") or f"skill-{index}")
            candidate_id = str(candidate.get("candidate_id") or f"candidate-{index}")
            # latest-wins per candidate_id: stable name avoids duplicate handoff files on re-run
            file_name = f"handoff-{safe_slug(candidate_id)}.json"
            bundle_path = handoff_dir / file_name
            updated_handoff = dict(handoff)
            finalize_skill_handoff_presentation(updated_handoff, str(bundle_path))
            bundle = {
                "handoff_schema_version": 2,
                "record_type": "skill_creator_handoff",
                "recorded_at": recorded_at,
                "candidate_id": candidate.get("candidate_id"),
                "label": candidate.get("label"),
                "suggested_kind": candidate.get("suggested_kind"),
                "context": context,
                "handoff": updated_handoff,
            }
            bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            updated_handoff["context_file"] = str(bundle_path)
            updated_handoff["context_format"] = "json"
            candidate["skill_creator_handoff"] = updated_handoff
            persisted.append(
                {
                    "candidate_id": candidate.get("candidate_id"),
                    "skill_name": skill_name,
                    "context_file": str(bundle_path),
                }
            )
    except Exception as exc:
        return {
            "attempted": True,
            "status": "failed",
            "dir": str(handoff_dir),
            "items_written": 0,
            "message": str(exc),
            "items": persisted,
        }
    if not persisted:
        return {
            "attempted": False,
            "status": "skipped",
            "reason": "no_skill_candidates",
            "dir": str(handoff_dir),
            "items_written": 0,
            "items": [],
        }
    return {
        "attempted": True,
        "status": "persisted",
        "dir": str(handoff_dir),
        "items_written": len(persisted),
        "items": persisted,
    }


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        prepare_payload = load_json(Path(args.prepare_file).expanduser().resolve())
        judgments = load_judgments(args.judge_file)
        classifications = load_classification_overlays(args.classification_file)
        if args.classification_targets_only:
            targets = build_classification_target_candidates(
                prepare_payload,
                judgments_by_candidate_id=judgments,
                classifications_by_candidate_id=classifications,
            )
            emit(
                {
                    "status": "success",
                    "source": PROPOSAL_SOURCE,
                    "mode": "classification_targets",
                    "summary": {
                        "target_count": len(targets),
                        "target_candidate_ids": [item.get("candidate_id") for item in targets],
                    },
                    "classification_targets": targets,
                }
            )
            return
        proposal = build_proposal_sections(
            prepare_payload,
            judgments_by_candidate_id=judgments,
            classifications_by_candidate_id=classifications,
            markdown_classification_detail=args.markdown_classification_detail,
        )
        recorded_at = recorded_at_iso()
        user_decisions = load_user_decisions(args.user_decision_file)
        user_decision_overlay = apply_user_decisions(
            proposal,
            user_decisions,
            recorded_at=recorded_at,
        )
        decision_log_path = resolve_output_path(args.decision_log_path, default=DEFAULT_DECISION_LOG_PATH)
        handoff_dir = resolve_output_path(args.skill_creator_handoff_dir, default=DEFAULT_SKILL_CREATOR_HANDOFF_DIR)
        decision_log_result = persist_decision_log_stub(
            proposal,
            prepare_payload,
            decision_log_path=decision_log_path,
            recorded_at=recorded_at,
        )
        skill_creator_handoff_result = persist_skill_creator_handoffs(
            proposal,
            handoff_dir=handoff_dir,
            recorded_at=recorded_at,
        )
        emit(
            {
                "status": "success",
                "source": PROPOSAL_SOURCE,
                "recorded_at": recorded_at,
                "persistence": {
                    "decision_log": decision_log_result,
                    "skill_creator_handoff": skill_creator_handoff_result,
                },
                "user_decision_overlay": user_decision_overlay,
                **proposal,
            }
        )
    except Exception as exc:
        emit(error_response(PROPOSAL_SOURCE, str(exc)))


if __name__ == "__main__":
    main()
