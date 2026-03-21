# Proposal JSON Contract

`skill_miner_proposal.py` の stdout JSON の全体構造と下流消費パスを定義する。

## Top-Level Shape

```json
{
  "status": "success",
  "source": "skill-miner-proposal",
  "recorded_at": "ISO8601",

  "ready":             [/* candidate objects */],
  "needs_research":    [/* candidate objects */],
  "rejected":          [/* candidate objects */],

  "markdown":          "rendered proposal markdown (human-readable view)",
  "markdown_classification_detail": false,
  "selection_prompt":  "string | null",
  "decision_log_stub": [/* decision stub objects */],
  "learning_feedback": {/* status, reason_summary, next_step */},
  "observation_contract": {/* mode, workspace_label, days, sources, fidelity */},
  "summary": {
    "ready_count": 0,
    "needs_research_count": 0,
    "rejected_count": 0,
    "triaged_total": 0
  },

  "persistence": {
    "decision_log":         {/* attempted, status, path, entries_written */},
    "skill_creator_handoff": {/* attempted, status, dir, items_written, items[] */}
  },
  "user_decision_overlay": {
    "applied": 0,
    "matched_candidate_ids": [],
    "unmatched_candidate_ids": []
  }
}
```

`markdown_classification_detail` は `skill_miner_proposal.py` の `--markdown-classification-detail` に対応する。`false`（既定）のときは `markdown` 内の LLM / guardrail 由来の説明を 1 行に圧縮し、`classification_trace` の長い展開は省略する。`ready[]` 各要素の `classification_trace` / `classification_guardrail_signals` は **常に** machine-actionable として保持される。

`summary.triaged_total` は `ready_count + needs_research_count + rejected_count`（proposal 組み立て後の行数）。チャット見出しの「候補合計」や `proposal.md` 先頭の `候補内訳` は **これと各 count を正**とする。`prepare.json` の `summary.total_candidates` だけを併用すると分割材料化やメタ不整合でズレうるため、ユーザー向け件数は `proposal.json` の `summary` を使う。

## Ready Candidate Object

`ready[]` の各要素は候補の全情報 + kind ごとの machine-actionable payload を含む。

```json
{
  "candidate_id": "string",
  "label": "identity key (Python生成。carry-forward に使用。LLM は変更しない)",
  "suggested_kind": "CLAUDE.md | skill | hook | agent",
  "suggested_kind_source": "provided | heuristic | llm | guardrail_override",
  "classification_trace": [
    { "stage": "heuristic | provided | llm | guardrail", "kind": "skill", "reason": "..." }
  ],
  "confidence": "strong | medium",
  "proposal_ready": true,
  "triage_status": "ready",
  "origin_hint": "human | mixed | parent_ai | unknown | \"\"",
  "user_signal_strength": "high | medium | low | unknown",
  "contamination_signals": ["assistant_fallback", "summary_fallback", "sidechain"],
  "evidence_items": [
    { "session_ref": "...", "timestamp": "ISO8601", "source": "...", "summary": "..." }
  ],
  "support": {
    "total_packets": 5,
    "claude_packets": 3,
    "codex_packets": 2,
    "contaminated_packets": 0
  },

  "skill_scaffold_context":  {/* skill candidates only */},
  "skill_creator_handoff":   {/* skill candidates only */},
  "next_step_stub":          {/* hook/agent candidates only */},

  "classification_guardrail_signals": {
    "claude_md_classic_signal": true,
    "declarative_weight": 100.0,
    "workflow_weight": 5.0,
    "declarative_ratio": 1.0,
    "agent_role_consistency": false,
    "claude_md_qualifies": true,
    "llm_confidence": "high"
  }
}
```

補足:

- `label` は Python が生成する carry-forward 用 identity key。`decision_key` / `content_key` の材料になるため、LLM は変更しない。ユーザー向けの表示には `display_label` を使う（下記参照）
- `display_label` は LLM が表示直前に生成する表示専用の名前（JSON フィールドには存在しない）。生成ルールは `skill-miner/SKILL.md` の **Display Label Rules** を参照。`decision_key` / `content_key` には無関係
- `classification_guardrail_signals` は Phase 3 で追加された **観測用** フィールド。guardrail が参照した宣言的比率・役割一貫性・従来の CLAUDE.md artifact 有無などをまとめる。`llm_confidence` は overlay の `confidence` があれば反映し、**guardrail の分岐は現状これに依存しない**（ログ分析・プロンプト改善用）
- `origin_hint` は packet 群の由来ヒント。`human` は通常の人間主導パターン、`parent_ai` は sidechain/subagent 起点、`mixed` は人間起点と内部足場が混在、`unknown` は補助 signal が弱く判定保留、空文字は legacy packet 由来で未観測を意味する
- `user_signal_strength` は `primary_intent` がどれだけ user 側から復元できたかのヒント。`low` は assistant fallback や summary fallback に依存した candidate を示す
- `contamination_signals` は user-facing proposal を慎重に扱うべき補助 signal。現時点の代表値は `assistant_fallback`, `summary_fallback`, `sidechain`
- `support.contaminated_packets` は contamination signal を持つ packet 数。0 でない場合は `ready` に上げる前に signal を確認する
- `classification_trace` は分類の採用経路を表す。MVP では `provided / heuristic / llm / guardrail` の順で最大 3-4 ステップ入る

### `skill_scaffold_context` (suggested_kind=skill)

skill-creator への引き継ぎ context。`skill-applier` が Scaffold Draft として提示する。

```json
{
  "skill_name": "slug-name",
  "goal": "... を再利用可能なスキルとして保存する",
  "task_shapes": ["review_code", "..."],
  "artifact_hints": ["skill-md", "..."],
  "rule_hints": ["findings-first", "..."],
  "intent_trace": ["intent_1", "..."],
  "constraints": ["constraint_1", "..."],
  "acceptance_criteria": ["criteria_1", "..."],
  "execution_hints": ["成果物タイプ: ...", "適用ルール: ..."],
  "representative_examples": ["example_1", "..."],
  "evidence_summaries": ["summary_1", "..."],
  "observation_count": 5,
  "source_diversity": 2
}
```

### `skill_creator_handoff` (suggested_kind=skill)

skill-creator に渡すプロンプトと metadata。`--skill-creator-handoff-dir` 指定時は JSON ファイルとして永続化される。

**schema v2（cross-repo）:** `handoff_schema_version`, `cross_repo`, `target_workspace_hint`, `current_workspace`, `handoff_scope`（`current_repo` \| `other_repo`）, `execution_instruction`, `workspace_resolution_note`, `presentation_block`（永続化後）等。判定仕様は `references/cross-repo-handoff.md`。

```json
{
  "tool": "skill-creator",
  "entrypoint": "/skill-creator",
  "handoff_schema_version": 2,
  "cross_repo": false,
  "handoff_scope": "current_repo",
  "target_workspace_hint": "/abs/path/to/repo",
  "current_workspace": "/abs/path/to/repo",
  "execution_instruction": "1. 適用先のリポジトリを開く（目安: …）\n2. /skill-creator を実行し…",
  "workspace_resolution_note": "観測 workspace と同一リポジトリ向けの候補として扱っています。",
  "presentation_block": "(fenced text block; includes handoff file path after persist)",
  "prompt": "Create or refine …",
  "context_file": "/path/to/persisted-bundle.json",
  "context_format": "json"
}
```

### `next_step_stub` (suggested_kind=hook|agent)

hook/agent 候補の設計案。`skill-applier` が Design Proposal として提示する。

hook:
```json
{
  "kind": "hook",
  "prompt": "「{label} を hook にしてください」と次セッションで指示",
  "trigger_event": "PostToolUse | Stop | ...",
  "target_tools": ["tool_name"],
  "action_summary": "実行内容の 1 文説明",
  "guard_condition": "実行しない条件の 1 文説明"
}
```

agent:
```json
{
  "kind": "agent",
  "prompt": "「{label} を agent にしてください」と次セッションで指示",
  "role_summary": "1 文での役割定義",
  "behavior_rules": ["rule_1", "rule_2"],
  "trigger_description": "いつこの agent を使うか",
  "reference_examples": ["example_1"]
}
```

## Decision Log Stub Object

`decision_log_stub[]` の各要素。全候補分出力される。

```json
{
  "candidate_id": "string",
  "decision_key": "stable-match-key",
  "content_key": "stable-content-key-without-kind",
  "label": "display name",
  "recommended_action": "adopt | defer | reject",
  "triage_status": "ready | needs_research | rejected",
  "suggested_kind": "CLAUDE.md | skill | hook | agent",
  "reason_codes": ["quality_flag_1"],
  "split_suggestions": ["split_axis_1"],
  "intent_trace": ["intent_1", "intent_2"],
  "constraints": ["constraint_1"],
  "acceptance_criteria": ["criteria_1"],
  "user_decision": null,
  "user_decision_timestamp": null,
  "carry_forward": true,
  "observation_count": 3,
  "prior_observation_count": 0,
  "observation_delta": 3
}
```

- `decision_key`: `suggested_kind` を含むため、分類が変わると値が変わる（次回 prepare の一次マッチ用）
- `content_key`: `label` + `intent_trace` / `constraints` / `acceptance_criteria` の先頭スライスのみから生成。分類変更後も同じ候補なら安定する（carry-forward の二次マッチ用）。`skill_miner_proposal.py` の decision log 永続化行にも含まれる

## Observation Contract

`observation_contract` は観測条件のメタデータ。proposal の信頼性判断に使う。

```json
{
  "mode": "workspace | all-sessions",
  "workspace_label": "project-name",
  "days": 7,
  "successful_sources": ["git-history", "claude-history"],
  "input_fidelity": "original | approximate",
  "degraded": false,
  "degraded_sources": [],
  "adaptive_window": {
    "enabled": true,
    "expanded": false,
    "reason": null,
    "initial_days": 7,
    "effective_days": 7,
    "fallback_days": 30
  }
}
```

補足（整合ルール）:

- `input_fidelity` を正とし、近似入力判定は `input_fidelity == "approximate"` で行う。
- adaptive window の有効/拡張判定は `adaptive_window` 配下のみを参照する。
- contamination signal は `observation_contract` には入れず、candidate object 側で扱う。汚染疑いの判断は `origin_hint`, `user_signal_strength`, `contamination_signals`, `support.contaminated_packets` を組み合わせて行う。

## Learning Feedback

`learning_feedback` は 0 件時の成長シグナル。daytrace-session の enriched output に使う。

```json
{
  "status": "ready_candidates_available | needs_more_observation | insufficient_signal",
  "reason_summary": "human-readable reason",
  "next_step": "what to do next",
  "split_candidates": [
    { "candidate_id": "...", "label": "...", "split_suggestions": ["..."] }
  ]
}
```

## Persistence Results

`persistence` は副作用の実行結果を返す。CLI 引数で path を明示した場合のみ有効。

### `persistence.decision_log`
```json
{
  "attempted": true,
  "status": "persisted | skipped | failed",
  "path": "/path/to/decisions.jsonl",
  "entries_written": 6
}
```

### `persistence.skill_creator_handoff`
```json
{
  "attempted": true,
  "status": "persisted | skipped | failed",
  "dir": "/path/to/handoff-dir",
  "items_written": 1,
  "items": [
    { "candidate_id": "...", "skill_name": "...", "context_file": "/path/to/bundle.json" }
  ]
}
```

## 下流消費パス

| 消費者 | 使用フィールド | 目的 |
|--------|---------------|------|
| LLM (proposal 表示) | `markdown`, `selection_prompt` | ユーザーへの提案表示 |
| skill-applier | `ready[].skill_scaffold_context` | skill scaffold draft 提示 |
| skill-applier | `ready[].skill_creator_handoff` | skill-creator への handoff |
| skill-applier | `ready[].next_step_stub` | hook/agent 設計案提示 |
| skill_miner_decision.py | `decision_log_stub[]` | user decision の writeback |
| 次回 prepare | `decision_log_stub[]` via JSONL | carry-forward state machine |
| daytrace-session | `summary`, `observation_contract` | structured judgment log |
| daytrace-session | `learning_feedback` | 0 件時の enriched output |
