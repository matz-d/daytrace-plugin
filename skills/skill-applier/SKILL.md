---
name: skill-applier
description: >
  skill-miner が提案した候補を実際の成果物（CLAUDE.md ルール追記、skill scaffold、hook/agent 設計案）に
  固定化する。「提案を適用して」「CLAUDE.md に追加して」「skill にして」「hook 化して」と言われた時に使う。
  daytrace-session Phase 3 の固定化アクションも担う。
user-invocable: true
---

# Skill Applier

skill-miner の proposal を concrete artifact に固定化する skill。

## Goal

- miner が返した proposal の selected candidate を受け取り、`suggested_kind` に応じた固定化アクションを実行する
- 固定化後のユーザー判断（adopt / defer / reject）を decision log に writeback する
- 各 kind の詳細仕様は `references/` に分離し、本体は dispatch と共通ルールに集中する

やらないこと:

- パターン抽出・分類・triage（skill-miner の責務）
- proposal の生成（skill-miner の責務）
- skill の直接生成やデプロイ（skill-creator の責務）

## Inputs

skill-miner の `proposal.json`（`skill_miner_proposal.py` の stdout JSON）を直接消費する。
JSON contract の全体像は `skills/skill-miner/references/proposal-json-contract.md` を参照する。

本 skill が使う主要フィールド:

- `ready[]` — 固定化対象の候補リスト
- `ready[].suggested_kind` — dispatch 分岐キー
- `ready[].skill_scaffold_context` — skill scaffold draft の構造化入力（kind=skill）
- `ready[].skill_creator_handoff` — skill-creator への handoff prompt + context_file（kind=skill）
- `ready[].next_step_stub` — hook/agent 設計案の構造化入力（kind=hook|agent）
- `ready[].evidence_items[]` — 根拠表示用
- `decision_log_stub[]` — decision writeback の対象

## Dispatch Rules

候補の `suggested_kind` に応じて固定化パスを分岐する:

| suggested_kind | アクション | 詳細仕様 |
|---------------|-----------|---------|
| `CLAUDE.md` | Immediate Apply — diff preview → apply | `references/claude-md-apply.md` |
| `skill` | Scaffold Draft — context 構造化 → skill-creator handoff | `references/skill-scaffold.md` |
| `hook` | Design Proposal — 設計案提示 → 次セッション | `references/hook-agent-nextstep.md` |
| `agent` | Design Proposal — 設計案提示 → 次セッション | `references/hook-agent-nextstep.md` |

共通ルール:

- `CLAUDE.md` だけが low-risk immediate apply path を持つ
- `skill` / `hook` / `agent` は即時生成しない（設計案や handoff の提示のみ）
- detail phase でも raw history 全量には戻らない

## Scripts

スクリプトはこの `SKILL.md` と同じ plugin 内の `scripts/` にある。
このディレクトリから `../..` を辿った先を `${CLAUDE_PLUGIN_ROOT}` として扱う。

Detail 取得（選択候補の session_refs だけ）:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_detail.py --refs "<session_ref_1>" "<session_ref_2>"
```

Decision writeback:

```bash
SESSION_TMP="${SESSION_TMP:-$(mktemp -d "${TMPDIR:-/tmp}/daytrace-session-XXXXXX")}"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_decision.py --proposal-file "$SESSION_TMP/proposal.json" --candidate-index 1 --decision adopt --completion-state completed --output-file "$SESSION_TMP/user-decision.json"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_proposal.py --prepare-file "$SESSION_TMP/prepare.json" --judge-file "$SESSION_TMP/judge.json" --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl --skill-creator-handoff-dir ~/.daytrace/skill-creator-handoffs --user-decision-file "$SESSION_TMP/user-decision.json" > "$SESSION_TMP/proposal-final.json"
```

## Detail / Draft Rules

- `提案（固定化を推奨）` に候補がある場合だけ、1 件選んでもらう
- 選択候補の `session_refs` だけを `skill_miner_detail.py --refs ...` で取得する
- `CLAUDE.md` は immediate apply path で対応する（`references/claude-md-apply.md`）
- `skill` は Skill Scaffold Draft Spec に従い scaffold context を出す（`references/skill-scaffold.md`）
- `hook` / `agent` は Next Step Contract に従う（`references/hook-agent-nextstep.md`）
- detail phase でも raw history 全量には戻らない

## Decision Writeback

ユーザーが adopt / defer / reject を返した場合:

1. `skill_miner_decision.py` で `--user-decision-file` を生成する
2. `skill_miner_proposal.py` を同じ paths で再実行して persist する
3. `candidate-index` は 1-based（最初の候補は `1`）

完了状態の記録:

- `CLAUDE.md` apply 成功 → `--decision adopt --completion-state completed`
- `skill` scaffold 提示完了 → `--completion-state completed` は `done` 明示確認時のみ（確認手順は `references/skill-scaffold.md` の「done 確認フロー」に従う）
- `hook` / `agent` 設計案提示 → 成功未確認のまま session を閉じる場合は `adopt` を確定させず、`pending` 経由で `defer` 扱い
- ユーザーが「あとで」「今回は見送る」→ `defer` / `reject` を記録

## Completion Check

- selected candidate に対して適切な固定化パスが実行されている
- `CLAUDE.md` の場合: diff preview が表示され、apply/skip の結果が記録されている
- `skill` の場合: scaffold context が構造化されて提示されている
- `hook` / `agent` の場合: 設計案が提示されている
- user decision が writeback されている（選択があった場合）
