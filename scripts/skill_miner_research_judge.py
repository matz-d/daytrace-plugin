#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import emit, error_response
from skill_miner_common import RESEARCH_JUDGE_SOURCE, judge_research_candidate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Judge deep research results for a skill-miner candidate.")
    parser.add_argument("--candidate-file", required=True, help="Path to the JSON file produced by skill_miner_prepare.py.")
    parser.add_argument("--candidate-id", required=True, help="candidate_id to judge from the prepare payload.")
    parser.add_argument("--detail-file", required=True, help="Path to the JSON file produced by skill_miner_detail.py.")
    return parser


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def select_candidate(payload: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("candidate_id") == candidate_id:
                return candidate
    if payload.get("candidate_id") == candidate_id:
        return payload
    raise ValueError(f"candidate_id not found: {candidate_id}")


def select_details(payload: dict[str, Any]) -> list[dict[str, Any]]:
    details = payload.get("details")
    if not isinstance(details, list):
        raise ValueError("detail payload must contain a details list")
    return [detail for detail in details if isinstance(detail, dict)]


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        candidate_payload = load_json(Path(args.candidate_file).expanduser().resolve())
        detail_payload = load_json(Path(args.detail_file).expanduser().resolve())
        candidate = select_candidate(candidate_payload, args.candidate_id)
        details = select_details(detail_payload)
        judgment = judge_research_candidate(candidate, details)
        emit(
            {
                "status": "success",
                "source": RESEARCH_JUDGE_SOURCE,
                "candidate_id": args.candidate_id,
                "judgment": judgment,
            }
        )
    except Exception as exc:
        emit(error_response(RESEARCH_JUDGE_SOURCE, str(exc)))


if __name__ == "__main__":
    main()
