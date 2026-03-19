#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from common import emit, error_response

DECISION_SOURCE = "skill-miner-decision"
VALID_USER_DECISIONS = {"adopt", "defer", "reject"}
VALID_COMPLETION_STATES = {"completed", "pending"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a normalized user-decision payload for skill_miner_proposal.py.")
    parser.add_argument("--proposal-file", required=True, help="Path to the JSON file produced by skill_miner_proposal.py.")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--candidate-id", help="Candidate ID to update.")
    selector.add_argument("--candidate-index", type=int, help="1-based index into the ready candidate list.")
    parser.add_argument("--decision", required=True, choices=sorted(VALID_USER_DECISIONS), help="Requested user decision.")
    parser.add_argument(
        "--completion-state",
        choices=sorted(VALID_COMPLETION_STATES),
        help="Required for --decision adopt. Use completed only when the fixed action actually succeeded.",
    )
    parser.add_argument("--output-file", help="Optional path to write the decision payload JSON.")
    return parser


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def _find_candidate_by_id(proposal: dict[str, Any], candidate_id: str) -> tuple[dict[str, Any], str, int | None]:
    candidate_id = candidate_id.strip()
    for section_name in ("ready", "needs_research", "rejected"):
        section = proposal.get(section_name, [])
        if not isinstance(section, list):
            continue
        for index, candidate in enumerate(section, start=1):
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("candidate_id") or candidate.get("packet_id") or "").strip() == candidate_id:
                return candidate, section_name, index
    raise ValueError(f"Candidate ID not found: {candidate_id}")


def _find_candidate_by_index(proposal: dict[str, Any], candidate_index: int) -> tuple[dict[str, Any], str, int]:
    ready = proposal.get("ready", [])
    if not isinstance(ready, list):
        raise ValueError("Proposal JSON does not contain a ready candidate list")
    if candidate_index <= 0 or candidate_index > len(ready):
        raise ValueError(f"--candidate-index must be between 1 and {len(ready)}")
    candidate = ready[candidate_index - 1]
    if not isinstance(candidate, dict):
        raise ValueError(f"Ready candidate #{candidate_index} is not a JSON object")
    return candidate, "ready", candidate_index


def resolve_candidate(proposal: dict[str, Any], *, candidate_id: str | None, candidate_index: int | None) -> tuple[dict[str, Any], str, int | None]:
    if candidate_id:
        return _find_candidate_by_id(proposal, candidate_id)
    if candidate_index is not None:
        return _find_candidate_by_index(proposal, candidate_index)
    raise ValueError("Either --candidate-id or --candidate-index is required")


def normalize_decision(*, decision: str, completion_state: str | None) -> tuple[str, bool, str]:
    if decision != "adopt":
        if completion_state is not None:
            raise ValueError("--completion-state can only be used with --decision adopt")
        return decision, True, f"{decision}_carry_forward"

    if completion_state is None:
        raise ValueError("--completion-state is required when --decision adopt")
    if completion_state == "completed":
        return "adopt", False, "adopt_completed"
    return "defer", True, "adopt_pending_normalized_to_defer"


def build_decision_payload(
    candidate: dict[str, Any],
    *,
    section_name: str,
    candidate_index: int | None,
    decision: str,
    completion_state: str | None,
) -> dict[str, Any]:
    normalized_decision, carry_forward, normalization_reason = normalize_decision(
        decision=decision,
        completion_state=completion_state,
    )
    timestamp = datetime.now().astimezone().isoformat()
    decision_entry = {
        "candidate_id": str(candidate.get("candidate_id") or candidate.get("packet_id") or "").strip(),
        "decision_key": str(candidate.get("decision_key") or ""),
        "label": str(candidate.get("label") or ""),
        "suggested_kind": str(candidate.get("suggested_kind") or ""),
        "user_decision": normalized_decision,
        "user_decision_timestamp": timestamp,
        "carry_forward": carry_forward,
    }
    return {
        "status": "success",
        "source": DECISION_SOURCE,
        "selected_candidate": {
            "candidate_id": decision_entry["candidate_id"],
            "label": decision_entry["label"],
            "suggested_kind": decision_entry["suggested_kind"],
            "section": section_name,
            "index": candidate_index,
        },
        "normalization": {
            "requested_decision": decision,
            "completion_state": completion_state,
            "persisted_user_decision": normalized_decision,
            "carry_forward": carry_forward,
            "reason": normalization_reason,
        },
        "decision": decision_entry,
        "decisions": [decision_entry],
    }


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        proposal = load_json(Path(args.proposal_file).expanduser().resolve())
        candidate, section_name, candidate_index = resolve_candidate(
            proposal,
            candidate_id=args.candidate_id,
            candidate_index=args.candidate_index,
        )
        payload = build_decision_payload(
            candidate,
            section_name=section_name,
            candidate_index=candidate_index,
            decision=args.decision,
            completion_state=args.completion_state,
        )
        if args.output_file:
            output_path = Path(args.output_file).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            payload["output_file"] = str(output_path)
        emit(payload)
    except Exception as exc:
        emit(error_response(DECISION_SOURCE, str(exc)))


if __name__ == "__main__":
    main()
