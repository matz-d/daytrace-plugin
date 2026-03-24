---
name: skill-applier
description: >
  skill-miner が提案した候補を実際の成果物に変換する。
  CLAUDE.md ルール追記、skill scaffold、hook 設定生成、agent 定義生成を行う。
  「提案を適用して」「CLAUDE.md に追加して」「skill にして」「hook 化して」「エージェントにして」と言われた時に使う。
  daytrace-session Phase 4（Pattern Mining）のアクション実行も担う。
user-invocable: true
---

# Skill Applier

skill-miner の proposal を concrete artifact に変換する skill。

## Goal

- miner が返した proposal の selected candidate を受け取り、`suggested_kind` に応じたアクションを実行する
- アクション後のユーザー判断（adopt / defer / reject）を decision log に writeback する
- 各 kind の詳細仕様は `references/` に分離し、本体は dispatch と共通ルールに集中する

やらないこと:

- パターン抽出・分類・triage（skill-miner の責務）
- proposal の生成（skill-miner の責務）
- skill の直接生成やデプロイ（skill-creator の責務）

## Inputs

skill-miner の `proposal.json`（`skill_miner_proposal.py` の stdout JSON）を直接消費する。
JSON contract の全体像は `skills/skill-miner/references/proposal-json-contract.md` を参照する。

本 skill が使う主要フィールド:

- `ready[]` — 適用対象の候補リスト
- `ready[].suggested_kind` — dispatch 分岐キー
- `ready[].skill_scaffold_context` — skill scaffold draft の構造化入力（kind=skill）
- `ready[].skill_creator_handoff` — skill-creator への handoff（`presentation_block` で target repo + 手順を提示。cross-repo は `references/skill-scaffold.md` / `skill-miner/references/cross-repo-handoff.md`）
- `ready[].next_step_stub` — hook/agent 生成の構造化入力（kind=hook|agent）
- `ready[].evidence_items[]` — 根拠表示用
- `decision_log_stub[]` — decision writeback の対象

## Dispatch Rules

候補の `suggested_kind` に応じてアクションパスを分岐する:

| suggested_kind | アクション | 詳細仕様 |
|---------------|-----------|---------|
| `CLAUDE.md` | Immediate Apply — diff preview → apply | `references/claude-md-apply.md` |
| `skill` | Scaffold Draft — context 構造化 → skill-creator handoff | `references/skill-scaffold.md` |
| `hook` | Guided Creation — 設計案提示 → 承認後 settings.json に生成 | `references/hook-agent-nextstep.md`, `references/hook-creation-guide.md` |
| `agent` | Guided Creation — 設計案提示 → 承認後 agents/ に生成 | `references/hook-agent-nextstep.md`, `references/agent-creation-guide.md` |

共通ルール:

- `CLAUDE.md` だけが low-risk immediate apply path を持つ（diff preview → 即時適用）
- `skill` は skill-creator に handoff する（scaffold context の提示のみ）
- `hook` / `agent` はユーザー承認後にこの skill 内で生成する
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

- `提案（アクション候補）` に候補がある場合だけ、1 件選んでもらう
- 選択候補の `session_refs` だけを `skill_miner_detail.py --refs ...` で取得する
- `CLAUDE.md` は immediate apply path で対応する（`references/claude-md-apply.md`）
- `skill` は Skill Scaffold Draft Spec に従い scaffold context を出す（`references/skill-scaffold.md`）
- `hook` / `agent` は Guided Creation Contract に従う（`references/hook-agent-nextstep.md` + フォーマット仕様は `references/hook-creation-guide.md` / `references/agent-creation-guide.md`）
- detail phase でも raw history 全量には戻らない
- `hook` / `agent` の承認後は **Workspace チェック**を行い、同一 repo なら Claude 直接生成、cross-repo なら handoff JSON を生成する（詳細: `references/hook-agent-nextstep.md`）

## Decision Writeback

ユーザーが adopt / defer / reject を返した場合:

1. `skill_miner_decision.py` で `--user-decision-file` を生成する
2. `skill_miner_proposal.py` を同じ paths で再実行して persist する
3. `candidate-index` は 1-based（最初の候補は `1`）

完了状態の記録:

- `CLAUDE.md` apply 成功 → `--decision adopt --completion-state completed`
- `skill` scaffold 提示完了 → `--completion-state completed` は `done` 明示確認時のみ（確認手順は `references/skill-scaffold.md` の「done 確認フロー」に従う）
- `hook` 生成成功（同一 repo: ファイル書き込み完了時 / cross-repo: handoff JSON 生成完了時）→ `--decision adopt --completion-state completed`
- `agent` 生成成功（同一 repo: ファイル書き込み完了時 / cross-repo: handoff JSON 生成完了時）→ `--decision adopt --completion-state completed`
- `hook` / `agent` 生成をユーザーが承認しなかった場合は `pending` 経由で `defer` 扱い
- ユーザーが「あとで」「今回は見送る」→ `defer` / `reject` を記録

## Completion Check

- selected candidate に対して適切なアクションパスが実行されている
- `CLAUDE.md` の場合: diff preview が表示され、apply/skip の結果が記録されている
- `skill` の場合: scaffold context が構造化されて提示されている
- `hook` の場合: 設計案が提示され、承認後に同一 repo では settings.json と .sh が生成、cross-repo では handoff JSON が生成されている
- `agent` の場合: 設計案が提示され、承認後に同一 repo では agents/ に .md が生成、cross-repo では handoff JSON が生成されている
- user decision が writeback されている（選択があった場合）
