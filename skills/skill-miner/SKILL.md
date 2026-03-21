---
name: skill-miner
description: >
  Claude / Codex 履歴から反復パターンや定着させたい作法を抽出し、
  recurring workflow / repeated instruction を
  `CLAUDE.md` / `skill` / `hook` / `agent` のどれに適用すべきか評価して proposal を返す。
user-invocable: true
---

# Skill Miner

AI 会話履歴を横断して、定着させたい作法を `extract / classify / evaluate / propose` するための skill。

## Goal

- Claude / Codex 履歴から反復パターンを抽出する
- 候補を `CLAUDE.md` / `skill` / `hook` / `agent` の 4 分類で判定する
- `提案（アクション候補） / 有望候補（もう少し観測が必要） / 観測ノート` の main UX で返す
- proposal phase では raw history を再読込せず、prepare の contract だけで根拠表示を完結させる

やらないこと:

- `plugin` 分類
- `skill` / `hook` / `agent` の即時生成
- `daily-report` / `post-draft` の処理

## Inputs

aggregator は使わない。`skill-miner` 専用 CLI だけを使う。

スクリプトはこの `SKILL.md` と同じ plugin 内の `scripts/` にある。
このディレクトリから `../..` を辿った先を `${CLAUDE_PLUGIN_ROOT}` として扱う。

提案フェーズ:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_prepare.py --input-source auto --store-path ~/.daytrace/daytrace.sqlite3 --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl
```

workspace 制限を外して広域観測する場合:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_prepare.py --input-source auto --store-path ~/.daytrace/daytrace.sqlite3 --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl --all-sessions
```

補足:

- デフォルト観測窓は 7 日
- `--all-sessions` は workspace 制限を外すだけで、7 日窓は維持する
- `--input-source auto` は store-backed `observations` を優先し、該当データが無い時だけ raw history へフォールバックする
- 日報と同じ「報告日」に窓を合わせる場合は `--reference-date YYYY-MM-DD`（`daily_report_projection` の `report_date` と一致。06:00 境界は `aggregate_core.resolve_date_filters` と揃う）
- `--store-path` を付けると candidate を `patterns` として store へ更新し、旧 raw path との比較が必要な期間は `--compare-legacy` を併用できる
- `workspace` モード（`--all-sessions` を付けない通常実行。`--workspace` 未指定時は `cwd` を使う）だけ、packet / candidate が少なすぎる場合に 30 日へ自動拡張する
- full-history 相当の観測が必要な場合は、B0 観測（改善優先度を決めるための実データ観測）用に `--all-sessions --days 3650 --dump-intents` のように明示する

追加調査の detail 再取得:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_detail.py --refs "<session_ref_1>" "<session_ref_2>"
```

追加調査後の結論判定:

```bash
SESSION_TMP="${SESSION_TMP:-$(mktemp -d "${TMPDIR:-/tmp}/daytrace-session-XXXXXX")}"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_research_judge.py --candidate-file "$SESSION_TMP/prepare.json" --candidate-id "<candidate_id>" --detail-file "$SESSION_TMP/detail.json"
```

最終 proposal 組み立て（classification overlay がある場合は `--classification-file` を繰り返す）:
```bash
SESSION_TMP="${SESSION_TMP:-$(mktemp -d "${TMPDIR:-/tmp}/daytrace-session-XXXXXX")}"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_proposal.py \
  --prepare-file "$SESSION_TMP/prepare.json" \
  --judge-file "$SESSION_TMP/judge.json" \
  --classification-file "$SESSION_TMP/classifications/c1.json" \
  --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl \
  --skill-creator-handoff-dir ~/.daytrace/skill-creator-handoffs \
  > "$SESSION_TMP/proposal.json"
```
ユーザー判断の writeback:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_decision.py --proposal-file "$SESSION_TMP/proposal.json" --candidate-index 1 --decision adopt --completion-state completed --output-file "$SESSION_TMP/user-decision.json"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_proposal.py --prepare-file "$SESSION_TMP/prepare.json" --judge-file "$SESSION_TMP/judge.json" --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl --skill-creator-handoff-dir ~/.daytrace/skill-creator-handoffs --user-decision-file "$SESSION_TMP/user-decision.json" > "$SESSION_TMP/proposal-final.json"
```
永続化 path の扱い:
- `skill_miner_prepare.py` と `skill_miner_proposal.py` は同じ `--decision-log-path` を共有する
- 一時 JSON は固定 `/tmp/*.json` ではなく、`mktemp -d` で作った session-specific temp dir に置く
- `skill_miner_decision.py` は proposal 選択結果を `--user-decision-file` 互換 JSON に正規化する
- `skill_miner_proposal.py` の skill handoff は `--skill-creator-handoff-dir` に保存される
- CLI 自体は既定値を持つが、orchestration 側では副作用を意図的に扱うため path を明示する

## Execution Rules

1. まず `skill_miner_prepare.py` を 1 回だけ実行する
2. デフォルト観測窓は `7` 日
3. `--all-sessions` は workspace 制限を外すモードであり、無制限読み込みではない
4. `workspace` モード（`--all-sessions` なし）は 7 日で開始し、packet / candidate が少なすぎる時だけ 30 日へ自動拡張する
5. adaptive window は `workspace` モードにだけ持たせる
6. 実行モードは CLI 引数だけで決める。state file は持たない
7. `candidates` と `unclustered` を `ready` / `needs_research` / `rejected` に分ける
8. 正式提案は `proposal_ready=true` の候補だけを採用し、返却件数は `prepare` 側の `top_n` に従う（デフォルト `10`）。`0 件` でも正常系として扱う
9. `needs_research` 候補だけ、必要な場合に限って `research_targets` を使って 1 回だけ追加調査する
10. `skill_miner_research_judge.py` の結論を proposal に反映し、`提案（アクション候補） / 有望候補 / 観測ノート` を返す
11. `提案（アクション候補）` がある時だけ、次セッションでどれを apply するかを確認する

## Division of Labor

### Python side

- raw Claude / Codex JSONL を直接読む
- Claude は時間 gap と `isSidechain` で logical session に分割する
- packets を cluster 化して ranked `candidates` を返す
- `session_ref` を発行する
- candidate ごとに `evidence_items[]` を最大 3 件作る
- `--dump-intents` 指定時だけ `intent_analysis` を返す

### LLM side

- `candidates` を 3 区分にトリアージする
- 正式提案に進める候補だけを 4 分類へ仮分類する
- `なぜこの候補か` と `なぜその分類か` を説明する
- 適用アクション（CLAUDE.md apply / skill scaffold / hook・agent 設計案）は `skill-applier` skill に委ねる

やってはいけないこと:

- proposal phase で raw history を読み直す
- Python 側の cluster を捨てて candidate を再構築する
- ユーザーが選ぶ前に detail を大量取得する

## Prepare Output Reading Guide

`skill_miner_prepare.py` の主な読みどころ:

- `config.days`
  - 初期観測窓。通常は `7`
- `config.effective_days`
  - 実際に使われた観測窓。workspace adaptive window で `30` になる場合がある
- `config.all_sessions`
  - `true` の時は workspace 制限だけを外す
- `config.adaptive_window`
  - workspace モード（`--all-sessions` なし）で 30 日へ拡張したか、その判定基準と初期件数
- `config.adaptive_window.expanded`
  - adaptive window が発火したかどうかを示す canonical な判定キー
- `summary`
  - packet 数、candidate 数、blocking の規模。adaptive window 判定は含めない
- `candidates[].support`
  - 出現回数、source 多様性、直近性
- `candidates[].confidence`, `proposal_ready`, `triage_status`
  - 候補の強さと triage 結果
- `candidates[].evidence_items`
  - proposal 用の根拠チェーン
- `candidates[].research_targets`
  - `needs_research` 候補で優先して detail を取る ref
- `candidates[].research_brief`
  - 追加調査で見るべき観点
- `intent_analysis`
  - `--dump-intents` 指定時だけ出る B0 観測用サマリ

`evidence_items[]` contract:

```json
{
  "session_ref": "codex:abc123:1710000000",
  "timestamp": "2026-03-10T09:00:00+09:00",
  "source": "codex-history",
  "summary": "SKILL.md の構造確認を行い、提案理由を整理"
}
```

注意:

- `primary_intent` は packet ごとの主目的を短く正規化した文字列
- canonical packet は schema v2 (`packet_version=2`) と required fields が揃った時だけ再利用される
- `user_rule_hints` は 1 回出現の user directive を clustering 用に保持し、`user_repeated_rules` は strict repeated evidence として別に残る
- `task_shape` / `artifact_hints` / `representative_snippets` は cleaned user text を優先し、assistant text は user text が無い時だけ fallback で使う
- `user_rule_hints` は directive-only で、用語説明や差分説明の mention は rule count に入れない
- `summary` は `primary_intent` 優先、空なら snippet 由来
- `proposal` 側は `evidence_items[]` を使って表示し、raw history を再読込しない
- path は `[WORKSPACE]`、URL はドメインだけにマスクされる
- stale store slice を品質回復したい時は code path ではなく aggregate/backfill を再実行する

## Classification Rules

分類先は 4 つだけ使う。詳細な境界ケースは `references/classification.md` を参照する。
B0 観測の方法と優先順位ルールは `references/b0-observation.md` を参照する。

- `CLAUDE.md`
  - repo ローカルで毎回守らせたい原則
- `skill`
  - 明確な入出力を持つ多段フロー
- `hook`
  - 判断不要で自動実行向きの機械処理
- `agent`
  - 継続的な役割や行動原則が価値の中心

除外:

- `plugin`
  - v2 の一次分類では使わない

## Triage Rules

### `ready`

- `proposal_ready=true`
- `confidence` が `strong` または `medium`

### `needs_research`

- 巨大クラスタ
- 汎用 task shape / 汎用 tool に偏る
- `quality_flags` に注意信号がある

### `rejected`

- `unclustered`
- `confidence=insufficient`
- 単発に近い、または一般化が弱い

ルール:

- 正式提案の返却件数は `prepare` 側の `top_n` に従う（デフォルト `10`）
- `0 件` でも失敗扱いにせず、理由と次回への示唆を返す
- `needs_research` 候補は必要な場合だけ detail を取る

## Proposal Output Contract

`skill_miner_proposal.py` の stdout は machine-actionable な JSON object であり、`markdown` はその 1 フィールドに過ぎない。
下流の skill-applier や decision writeback はこの JSON を直接消費する。

主要フィールド:

| フィールド | 型 | 消費者 |
|-----------|-----|--------|
| `ready[]` | candidate objects | skill-applier が適用アクションに使う |
| `ready[].skill_scaffold_context` | object | skill scaffold draft の構造化入力 |
| `ready[].skill_creator_handoff` | object | skill-creator への handoff prompt + context_file |
| `ready[].next_step_stub` | object | hook/agent 設計案の構造化入力 |
| `markdown` | string | ユーザー向け提案表示（人間可読ビュー） |
| `selection_prompt` | string\|null | 候補選択プロンプト（ready > 0 の時のみ） |
| `decision_log_stub[]` | objects | decision writeback + carry-forward |
| `observation_contract` | object | 観測条件メタデータ |
| `learning_feedback` | object | 0 件時の成長シグナル |
| `summary` | object | `ready_count`, `needs_research_count`, `rejected_count` |
| `persistence` | object | decision_log / handoff の書き込み結果 |

フィールド定義の詳細は `references/proposal-json-contract.md` を参照する。

## Proposal Format (Markdown View)

既定の `markdown` は **最終 `suggested_kind`・短い分類サマリ（LLM/guardrail 時）・根拠・次の一手**を中心にし、`classification_trace` の段階展開は省略する。詳細は `--markdown-classification-detail` または `ready[]` の JSON を参照。

### Chat-side compact 表（`daytrace-session` / `docs/output-polish.md` §6）

統合セッションでは、チャットの第一画面を次の表形式にする（`ready[]` を 1 行ずつ埋める）。**全文の `markdown` は `output_dir` の `proposal.md` に保存**し、チャットには表＋一行要約＋パスを返す。

```text
| # | 候補 | 種類 | 確度 | 効果 | アクション |
|---|------|------|------|------|-----------|
| 1 | {label} | CLAUDE.md / skill / hook / agent | {高い/中程度/まだ弱い 等} | {次回どう楽になるか一言} | {すぐ追加可 / skill-creator 等} |
```

- **適用スコープ**（`workspace-local` / `global-personal`）は `docs/output-polish.md` §9-4 の任意メタ。表に列として必須にしない。本文や注記で短く付けてもよい
- `/skill-miner` 単体実行時も、チャット切れ防止のため同じく `proposal.md` へ保存することを推奨

proposal の冒頭には観測範囲を明示し、3 区分で返す。
内部 triage key（`ready` / `needs_research` / `rejected`）はそのままで、ユーザー向け見出しだけを変更する。

`intent_trace` ルール:

- proposal markdown には `intent_trace` を直接展開しない（raw intent の羅列はノイズになるため）
- 根拠表示は `evidence_items[].summary` で完結させる
- `intent_trace` は `decision_log_stub` にのみ含める（デバッグ・監査用詳細は Decision Log Contract を参照）
- LLM が分類 override する場合、`intent_trace` を根拠として内部メモやユーザー向け短文に引用してよい（`[DayTrace]` は使わない）
- `needs_research` の `research_brief.questions` に intent 不一致を含めてよい
- contamination signal（`origin_hint`, `user_signal_strength`, `contamination_signals`）は markdown に短い注記として出してよい
- `origin_hint=""` は legacy packet 由来の「未観測」を意味し、単独では汚染疑いとして扱わない

```markdown
### 観測範囲
観測範囲: {workspace名} / 直近 {N}日間 / {使用した source リスト}

## 提案（アクション候補）

1. 候補名
   固定先: skill
   確度: 中程度 — 複数セッションで出現、もう少し定着を見たい
   根拠:
   - 2026-03-08T10:00:00+09:00 claude-history: findings-first review を要求
   - 2026-03-10T09:00:00+09:00 codex-history: 同系の review 指示を再確認
   期待効果: 同種作業の再利用フローを安定化できる
   → この作法を固定すれば、毎回の指示が不要になります

## 有望候補（もう少し観測が必要）

1. 候補名
   確度: まだ弱い — 出現回数が少なく、今後の観測次第
   出現: 3回 / 2ソース
   根拠:
   - 2026-03-08T10:00:00+09:00 claude-history: 汎用 review 指示
   現状: 巨大クラスタで意味の異なる作業が混ざる可能性がある
   次のステップ: 1-2 週間の運用後に再観測で分割判断

## 観測ノート

1. 候補名または項目種別
   理由: 1 回だけの出現で、繰り返しパターンとは判断できませんでした
```

ルール:

- `提案（アクション候補） / 有望候補 / 観測ノート` を main UX にする
- `提案（アクション候補）` だけを重要度順に並べる
- `有望候補` には `現状` と `次のステップ` を書く
- `観測ノート` には 1 文で理由を書く
- `提案（アクション候補）` が 1 件以上ある時だけ、末尾に候補選択プロンプトを付けて次セッションの apply / draft 選択へ進める
- contamination signal がある候補は `注記:` を付け、内部運用疑い・assistant fallback 依存・summary fallback 依存を短く示してよい

### 0 件時の出力

`proposal_ready=true` の候補が 0 件の場合も正常系として、以下を返す:

```markdown
### 観測範囲
観測範囲: {workspace名} / 直近 {N}日間 / {source}

## 提案（アクション候補）
今回は有力候補なし

検出候補数: {N}件中 0 件が提案条件を満たした
見送り理由の傾向: {主な理由（例: 観測窓が短い / oversized cluster / セッション数が少ない）}
候補が増える条件: {いつ再実行すると候補が出やすいか（例: 同じ workspace で 2-3 週間使い続けると反復パターンが明確化しやすい）}
```

## Decision Log Contract
`decision_log_stub[]` は proposal ごとに全候補分を出力し、次回判定への橋渡しに使う。
ユーザーが具体的な adopt / defer / reject を返した場合は、`skill_miner_decision.py` で `--user-decision-file` を作り、`skill_miner_proposal.py` を同じ `--decision-log-path` で再実行して persist する。
`proposal.json` は `skill_miner_proposal.py` の stdout を redirect して作る。`candidate-index` は 1-based（最初の候補は `1`）。
```json
{
  "decision_key": "stable-match-key",
  "content_key": "stable-content-key-without-kind",
  "candidate_id": "id",
  "label": "display name",
  "recommended_action": "adopt | defer | reject",
  "triage_status": "ready | needs_research | rejected",
  "suggested_kind": "CLAUDE.md | skill | hook | agent",
  "reason_codes": ["quality_flag_1", "..."],
  "split_suggestions": ["split_axis_1"],
  "intent_trace": ["intent_1", "intent_2"],
  "user_decision": null,
  "user_decision_timestamp": null,
  "carry_forward": true,
  "observation_count": 3,
  "prior_observation_count": 0,
  "observation_delta": 3
}
```
フィールド説明:
- `decision_key`: 次回 prepare の readback に使うキー（`suggested_kind` を含む）。分類が変わると値も変わる
- `content_key`: 分類に依存しない候補本体のキー。`decision_key` が一致しないが同一候補のときの二次マッチに使う（詳細は `references/carry-forward-state-machine.md`）
- `user_decision`: セッション中にユーザーが adopt / defer / reject を選んだ場合のみ埋まる。Python 側は `null` で初期化する
- `user_decision_timestamp`: `user_decision` 設定時の ISO8601。Python 側は `null` で初期化する
- `carry_forward`: 次回 prepare で考慮すべきか。デフォルト `true`
- `intent_trace`: 監査用。proposal markdown には展開しない
- `decision_log_stub` は次回判定用の機械的な橋渡しに限定し、分類 override の長い説明は保持しない
分類 override の記録ルール:
- override 理由は `decision_log_stub` ではなく、人間向けの候補説明または `### パターン提案` の一行要約に短く残す
- 推奨フォーマット: `分類 override: heuristic=<from> → final=<to> / reason: <short reason>`
- `daytrace-session` 配下では **compact 表 + `proposal.md`** を正とし、`[DayTrace]` プレフィックスは使わない
- standalone の `skill-miner` でも上記を推奨（長文のみチャットは切れやすい）
次回判定への反映ルール（詳細は `references/carry-forward-state-machine.md` を参照）:
- `user_decision="adopt"` かつ `CLAUDE.md` → CLAUDE.md に追記済み。次回は `## DayTrace Suggested Rules` と照合して重複 skip
- `user_decision="adopt"` かつ `skill/hook/agent` → 生成成功（`done`）を確認できた場合のみ `carry_forward=false` で次回 suppress。成功未確認・中断時は `defer` 扱いで suppress しない
- `user_decision="defer"` → 次回も候補化される。`observation_count` 増加で confidence が自然に上がる。`observation_delta` で変化量を追跡
- `user_decision="reject"` → 永続 reject しない。再浮上条件（evidence_changed / support_grew / time_elapsed）を満たした場合のみ再出現。いずれも未達なら suppress
- `user_decision=null` → 未選択。`carry_forward=true` のまま次回に自然再出現する

## Deep Research Rules

`needs_research` 候補だけ追加調査してよい。

- 1 candidate あたり最大 5 refs
- 追加調査は 1 回まで
- `research_targets` と `research_brief` を優先して使う
- 追加調査しても粒度が粗い場合は `観測ノート` に落とす

追加調査後:

- `promote_ready`
  - `提案（アクション候補）` へ移す
- `split_candidate`
  - `有望候補` に残し、必要なら分割軸を書く
- `reject_candidate`
  - `観測ノート` に移す

## Apply Actions

候補選択後の適用アクション（CLAUDE.md apply / skill scaffold / hook・agent 設計案）は `skill-applier` skill が担う。
詳細は `skills/skill-applier/SKILL.md` を参照する。

## Phase 3 classify 対象の絞り込み

LLM classification overlay は **全候補ではなく曖昧候補だけ**にかける。`rejected` は原則 overlay なし。明らかな `hook`・強い `CLAUDE.md` シグナルでヒューリスティックが閉じている候補は省略してよい。

- 規準の全文: `references/classify-target-selection.md`
- proposal の **markdown** は既定で分類内部を圧縮（1 行サマリ）。長い `classification_trace` を載せる場合は `skill_miner_proposal.py --markdown-classification-detail`
- **JSON** の `ready[]` は常に `classification_trace` / `classification_guardrail_signals` を保持（内部 contract を壊さない）

## Pre-Classification Contract

`suggested_kind` は Python 側の `infer_suggested_kind()` がヒューリスティックに事前付与する。
LLM は override できるが、明確な理由がない限り Python のデフォルトを尊重する。

**生成経路（どちらでも可）**:

- **親エージェント**: `prepare.json` を読み、`references/classification-prompt.md` の contract に従って候補ごとに overlay JSON を書き出す
- **子サブエージェント**: 親が `candidate_id` と抜粋を渡し、サブが 1 候補 1 ファイルの overlay を返す（contract は同一）

いずれの経路でも、確定処理は script 側に集約する。`skill_miner_proposal.py` に `--classification-file <json>` を **候補ごとに繰り返し**渡すと merge + guardrail で final kind になる。ファイルが無い・JSON 破損のファイルはスキップされ、当該候補は heuristic のみにフォールバックする。

判定ルール（優先順）:

1. `CLAUDE.md`: `artifact_hints` に `claude-md` または `rule_hints` に CLAUDE.md 系ルール名 → `CLAUDE.md`
2. `hook`: 上位 `task_shape` が全て hook 向き（`run_tests` 等） → `hook`
3. `skill`: 非汎用 `task_shape` が 1 つ以上 → `skill`
4. `agent`: `total_packets >= 4` かつ（agent 向き `task_shape` または `rule_hints` あり） → `agent`
5. フォールバック: 上記いずれにも該当しない → `skill`

`agent` は Python 側がヒューリスティックに候補提示できるが、条件が厳しいため実際に付与されるケースは少ない。
LLM は `suggested_kind_source="heuristic"` の場合、evidence を確認して override してよい。

LLM が override する条件:

- candidate の `representative_examples` を読み、明らかに別分類が適切な場合
- Python 側が `skill` をデフォルトで返したが、内容がルール固定だけで手順がない場合（→ `CLAUDE.md` に override）
- Python 側が `skill` をデフォルトで返したが、「どう振る舞うか」が主題で継続的役割が明白な場合（→ `agent` に override）
- Python 側が `agent` を返したが、定型フローに落とせる場合（→ `skill` に override）
- override 時は判断ログに理由を記録する
- proposal JSON では `suggested_kind_source=llm | guardrail_override` と `classification_trace` に反映される
- Phase 3 以降、`ready[]` の各候補に **`classification_guardrail_signals`**（宣言的比率・役割一貫性・従来 CLAUDE.md シグナル等）が付く。overlay の `confidence` は **guardrail 分岐に使わず**、観測・プロンプト改善用。境界と few-shot は `references/classification.md` / `references/classification-prompt.md` を参照

contamination signal を見た時の追加ルール:

- `origin_hint="parent_ai"` または `contamination_signals` に `sidechain` を含む候補は、原則 `ready` にしない。`needs_research` または `rejected` に落とし、内部オーケストレーション由来の可能性を明示する
- `user_signal_strength="low"` かつ `contamination_signals` に `assistant_fallback` または `summary_fallback` を含む候補は、原則 `ready` にしない
- `origin_hint=""`（legacy packet 由来の metadata 欠落）は単独では downgrade 根拠にしない
- contamination signal がある候補を昇格させる場合は、「実際の人間依頼が中心である」ことを `evidence_items[]` または detail で確認した短い理由が必須
- `research_brief.questions` に「実際の人間依頼か、assistant / internal scaffolding 優位か」を含めてよい
- user-facing proposal では内部由来の疑いを `注記:` として見える化し、無言で `CLAUDE.md` や `skill` に昇格させない

## Oversized Cluster Guard

`oversized_cluster` / `weak_semantic_cohesion` / `split_recommended` / `near_match_dense` は research 段階の blocking signal とみなす。
これらが未解消のまま `ready` に入ることはない。

- judgment なしの blocking signal → `needs_research` に強制
- judgment で `promote_ready` された candidate → 追加調査で blocking signal を解消済みとして `提案（アクション候補）` へ昇格してよい
- proposal markdown には `追加調査で確認済み: ...` を出し、何を解消して ready にしたかを残す
- oversized が解消されたことを claim するのは「cluster 全体が縮んだ」という意味ではなく、「sampled refs では 1 つの再利用可能パターンとして説明できた」という意味に限る

contamination guard:

- `low_user_signal` / `origin_uncertain` / `contaminated_candidate` は internal-signal guard とみなし、未解消のまま `ready` に入れない
- これらの flag は assistant fallback や sidechain 混入の疑いを表す。raw history の量ではなく、由来の確からしさを下げる signal として扱う
- legacy packet に contamination metadata が無い場合は、この guard を発火させない


## Completion Check

- `prepare` は 1 回だけ実行している
- 期間 contract は `7 日開始 + workspace-only adaptive 30 日` / `--all-sessions` に固定されている
- 4 分類以外の古い説明が残っていない
- `suggested_kind` が Python の `infer_suggested_kind()` で事前付与されている
- `oversized_cluster` が ready に流れていない
- `low_user_signal` / `origin_uncertain` / `contaminated_candidate` が未解消のまま ready に流れていない
- proposal phase の根拠が `evidence_items[]` だけで表示できる
- `0 件` を正常系として扱い、観測サマリと成長兆候を表示している
- 返却上限は `prepare` の `top_n` と一致している
