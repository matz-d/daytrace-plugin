---
name: daytrace-session
description: >
  「今日の振り返りをお願い」の一言で、ローカルログの収集・日報生成・投稿下書き・反復パターン提案まで
  自律的に完走する統合セッション。振り返りをまとめて、全部やって、1日のまとめ、と言われた時に使う。
user-invocable: true
---

# DayTrace Session

1 回の依頼で「収集 → 日報 → 投稿下書き → パターン提案（必要なら追加調査）」まで自律的に完走するオーケストレーション skill。

## Goal

- 1 回の依頼で全フェーズを追加指示なしで完走する
- ユーザー向けチャットに **`[DayTrace]` / `[DayTrace:trace]` / Phase 番号メタ**を出さない（内部判断は短い日本語の見出しと状態行で示す。`docs/output-polish.md` §6）
- ソース欠損やデータ不足でも止まらず、できる範囲で最後まで進む
- 最後に実施内容のサマリを返す（§5-5 の mixed-scope 注記と再構成元の要約を含む）
- ready proposal がある時は、Phase 4（Pattern Mining）内の適用アクションまで閉じる

やらないこと:

- 個別 skill の出力フォーマットや品質基準を上書きすること
- `Escalation Conditions` 以外で途中 ask を増やすこと
- フェーズ間で人の確認を待つこと（proposal 選択 / 適用確認を除く）

## Inputs

- 対象日: 指定がなければ `today`
- workspace: 任意。補助フィルタ
- mode: `自分用` or `共有用`。未指定なら `自分用` + 条件付き共有用自動生成

## Entry Contract

- ask は入口 0 回を基本とする
- 「今日の振り返りをお願い」「1日のまとめ」「全部やっておいて」「今日の活動を整理して」などから日付を抽出する
- mode / workspace / topic / reader は自然言語から抽出できればそれを使い、取れなければデフォルト
- `Escalation Conditions` の例外を除き、途中で追加 ask しない
- Phase 4 の候補選択（`selection_prompt`）は optional commit step であり、曖昧性解消の Ask ではない。選択がなければ pending のまま Phase 5 へ進む。実行環境で choices UI が使える場合は番号入力より choices を優先する
- パターン提案の件数表示は `prepare.json` ではなく **`proposal.json` の `summary`** を正とする（`triaged_total`＝合計、`ready_count`＝すぐ適用候補）。`markdown` 先頭の `候補内訳` 行とも一致させる

## Scripts

スクリプトはこの `SKILL.md` と同じ plugin 内の `scripts/` にある。
このディレクトリから `../..` を辿った先を `${CLAUDE_PLUGIN_ROOT}` として扱う。

Phase 1 Data Collection:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/daily_report_projection.py --date today --all-sessions
```

Phase 1 の JSON に `report_date` と `output_dir` が含まれる（単日スコープ時）。`output_dir` は projection が **返却前にディレクトリを作成済み**（`~/.daytrace` 未作成でも可）。日報・投稿下書き・提案の Markdown artifact はそこへ保存する（`docs/output-polish.md` §7）。各 skill でも Write 前に親ディレクトリが無ければ作成する。`aggregate.json` は内部デバッグ・再生成調査のための source snapshot として任意で同梱してよいが、テンプレ差し込みや export 用の主データとして扱わない（structured fill 用 JSON は Layer 4 の責務）。
store に旧 derivation の `activities` が残っていても、projection / `get_activities` 実行時に activity derivation version 不一致で再導出される。新しい browser compression / `share_policy` / `share_guard` を反映したい時は daytrace-session を再実行する。

Phase 3 Post Draft (conditional):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/post_draft_projection.py --date today --all-sessions
```

Phase 4 Pattern Mining:

Phase 1 の JSON に `report_date` があるときは、日報の報告日と観測窓を揃えるため **`--reference-date <report_date>`** を必ず付ける（`aggregate_core.report_day_for_local_time` / `resolve_date_filters` の 06:00 境界と一致）。`report_date` が無い場合（多日範囲など）は引数を省略する。

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_prepare.py --input-source auto --store-path ~/.daytrace/daytrace.sqlite3 --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl --all-sessions --reference-date YYYY-MM-DD
```

Phase 4 Detail (conditional):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_detail.py --refs "<ref1>" "<ref2>"
```

Phase 4 Judge (conditional):

```bash
SESSION_TMP="${SESSION_TMP:-$(mktemp -d "${TMPDIR:-/tmp}/daytrace-session-XXXXXX")}"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_research_judge.py --candidate-file "$SESSION_TMP/prepare.json" --candidate-id "<id>" --detail-file "$SESSION_TMP/detail.json"
```

Phase 4 Classification（`ready` / `needs_research` の**曖昧候補のみ**に LLM overlay。オーバーレイは任意）:

- **対象の絞り込み**は `skills/skill-miner/references/classify-target-selection.md` を正とする（開発リポジトリ全体では `docs/skill-miner-classify-targets.md` と同一内容）。`rejected` は原則 overlay を作らない
- 明らかな `hook`・強い `CLAUDE.md` シグナルでヒューリスティックが閉じている候補は overlay を省略してよい
- 自動で候補一覧を出したい場合は `skill_miner_proposal.py --classification-targets-only` を先に実行して、返ってきた `classification_targets[]` の `candidate_id` だけに overlay を作る
- 契約と few-shot は `skills/skill-miner/references/classification-prompt.md` を正とする
- 1 候補 1 ファイル（例: `$SESSION_TMP/classifications/<candidate_id>.json`）を親エージェントまたは子サブエージェントが**対象候補に限って**生成する

Phase 4 Proposal:

```bash
SESSION_TMP="${SESSION_TMP:-$(mktemp -d "${TMPDIR:-/tmp}/daytrace-session-XXXXXX")}"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_proposal.py \
  --prepare-file "$SESSION_TMP/prepare.json" \
  --judge-file "$SESSION_TMP/judge.json" \
  --classification-file "$SESSION_TMP/classifications/c1.json" \
  --classification-file "$SESSION_TMP/classifications/c2.json" \
  --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl \
  --skill-creator-handoff-dir ~/.daytrace/skill-creator-handoffs \
  > "$SESSION_TMP/proposal.json"
```

`--classification-file` は存在するオーバーレイごとに繰り返す（**絞り込んだ候補だけ**に対応するファイルだけ渡す）。オーバーレイが無い候補は引数を省略してよい（Python heuristic + guardrail のみで proposal を組み立てる）。詳細な分類トレースを markdown に載せる場合のみ `--markdown-classification-detail` を付ける（既定は圧縮表示。`ready[]` の JSON は常にフル contract）。

Phase 4 Decision Writeback (conditional):

```bash
SESSION_TMP="${SESSION_TMP:-$(mktemp -d "${TMPDIR:-/tmp}/daytrace-session-XXXXXX")}"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_decision.py --proposal-file "$SESSION_TMP/proposal.json" --candidate-index 1 --decision adopt --completion-state completed --output-file "$SESSION_TMP/user-decision.json"
```

workspace 指定がある場合は全コマンドに `--workspace /absolute/path` を追加する。

Pattern Mining の persistence ルール:

- `skill_miner_prepare.py` と `skill_miner_proposal.py` には同じ `--decision-log-path` を明示的に渡す
- Phase 4 の一時ファイルは固定 `/tmp/*.json` を使わず、`mktemp -d` 等で作った session-specific temp dir（例: `$SESSION_TMP/...`）に置く
- proposal に対するユーザー選択を保存する時は `skill_miner_decision.py` が出す `$SESSION_TMP/user-decision.json` を `skill_miner_proposal.py --user-decision-file` に渡す
- `skill_miner_proposal.py` には `--skill-creator-handoff-dir` も明示的に渡す
- 既定値は `~/.daytrace/...` だが、daytrace-session は副作用を見える化するため必ず CLI 引数で path を明示する
- デモや dry-run で副作用を隔離したい場合も、`mktemp -d` で作った session 専用 path に差し替える

## Execution Flow

Phase 1 → 1.5 → 2 → **3（投稿下書き）** → **4（パターン提案）** → 5 の順に実行する（チャット上の読み順と一致させる。`docs/output-polish.md` §6）。各フェーズで状態を短い日本語で示し、追加指示なしで次に進む。

### Phase 1: Source Assessment

1. `daily_report_projection.py` を 1 回実行する
2. `sources[]` を読み、各ソースの `status` と `scope` を確認する
3. ユーザー向けにソース収集の要約を出す（`[DayTrace]` は使わない）:

```
### ソース収集の結果
- git-history (12 events) — workspace scope
- claude-history (8 events) — all-day scope
- chrome-history → 権限不足のためスキップ
- codex-history (3 events) — all-day scope
- workspace-file-activity (24 events) — workspace scope
→ 4 ソースで続行します
```

4. 判断ルール:
   - `summary.source_status_counts.success >= 1` → 続行
   - `success == 0` → 空日報を出して Phase 5 へ飛ぶ（Phase 3・4 はスキップ）
   - `share_guard.requires_confirmation == true` の時だけ、共有用生成前の確認条件を `daily-report` SKILL に従って評価する

### Phase 1.5: DayTrace ダイジェスト

Phase 1 完了直後、Phase 2 に入る前に「今日の DayTrace ダイジェスト」を 3-5 行の散文で出す。
これは全フェーズの結果を先読みするものではなく、ログから読み取れる 1 日の概観を先に見せるためのもの。

```
## 今日の DayTrace ダイジェスト
今日は N 件のソースから X 件の活動を観測しました。
{主な活動の 1-2 文要約}。
この後、日報 → 投稿下書き（条件付き）→ パターン提案の順で続きます。
```

### Phase 2: Daily Report

1. Phase 1 の中間 JSON を使って日報を生成する（`report_date` / `output_dir` があれば artifact 保存先に使う）
2. 出力ルールは `daily-report` skill の SKILL.md に従う
3. mode が明示されている場合はその mode で生成する
4. 自動判断 — 共有用の追加生成:
   - 条件: mode 未指定かつ `summary.total_groups >= 5`
   - 満たす場合: `自分用` に加えて `共有用` も自動生成する
   - 満たさない場合: `自分用` のみ
5. ユーザー向けに一行で状態を示す:

```
### 日報
自分用・共有用を生成し、`output_dir` の `report-private.md` / `report-share.md` へ保存（パスと成否をファイル単位で記載）
```

（自分用のみの例）

```
### 日報
自分用のみ生成。`report-private.md` を保存（共有用はグループ数条件未満のため省略）
```

### Phase 3: Post Draft (conditional)

1. 自動判断 — 投稿下書きの生成:
   - 条件（いずれか 1 つ以上）:
     - Phase 1 の `sources` に `git-history` と (`claude-history` or `codex-history`) が両方 success
     - `summary.total_groups >= 4`
   - 満たす場合:
     - `post_draft_projection.py` を 1 回実行する
     - `post-draft` skill の SKILL.md に従って narrative draft を生成し、`output_dir` の `post-draft.md` に保存
   - 満たさない場合: スキップ
2. ユーザー向けの状態一行:

```
### 投稿下書き
生成済み。`post-draft.md` を保存（AI + Git 共起をもとに構成）
```

スキップ時:

```
### 投稿下書き
スキップ（生成条件未満: groups: 2, AI+Git 共起: なし）
```

### Phase 4: Pattern Mining & Proposals

1. `skill_miner_prepare.py` を 1 回実行する（stdout を `$SESSION_TMP/prepare.json` に保存）
2. `candidates[]` を確認する
3. 自動判断 — 追加調査:
   - 条件: `needs_research` 候補が 1 件以上
   - 満たす場合: 各 `needs_research` 候補の `research_targets` 上位 refs で `skill_miner_detail.py` → `skill_miner_research_judge.py` を自動実行する
   - 1 候補あたり最大 5 refs、追加調査は 1 回まで
4. **Classification（prepare 後・proposal 前）**:
   - `classify-target-selection.md` の規準に従い、**曖昧な** `ready` / `needs_research` 候補にだけ `classification-prompt.md` の overlay JSON を 1 候補 1 ファイル生成する（`rejected` は原則スキップ。明らかな hook / 強い CLAUDE.md は省略可）
   - 実行主体は親エージェントを既定とし、長い場合は子サブエージェントへ**対象候補単位**で委譲してよい
   - `suggested_kind` は Python の `infer_suggested_kind()` が事前付与済みであることが多い。LLM は evidence に基づき override してよいが、明確な理由がない限り heuristic を尊重する（Pre-Classification Contract 参照）
   - `agent` は guardrail で落とされることがある。証跡が弱いときは保守的な分類・`confidence: low` を付けてよい
   - 内部メモに **classify した件数 / 対象に含めた規準** を残してよい（ユーザー向けチャットに `[DayTrace:trace]` を出さない）
5. 提案の組み立て:
    - prepare 出力・judge 出力（あれば）・**生成した classification overlay（あれば）** を `skill_miner_proposal.py` に渡す（`--classification-file` を**作ったファイルだけ**繰り返す）。stdout を shell redirect で `$SESSION_TMP/proposal.json` に保存する
    - `proposal.json` は machine-actionable JSON（contract 詳細は `skill-miner/references/proposal-json-contract.md`）
    - **compact 表を生成する前に**、`ready[]` の各候補について `skill-miner` SKILL の **Display Label Rules** に従って `display_label` を生成する。`proposal.json` の `label`（identity key）は変更しない
    - `proposal.json` に `compact_ready_rows[]` / `compact_ready_markdown` がある場合はそれを chat-side compact 表の正本として使ってよい。必要なら `display_label` だけ上書きし、`label` は触らない
    - **ユーザー向けチャット**は `skill-miner` SKILL の **compact 表**を優先し、`ready_current_repo[]` / `ready_other_repo[]` / `ready_uncertain[]` がある場合は **現在のリポジトリ向け → 別リポジトリ向け / 要確認** の順に 2 段表示する
    - 長文は `output_dir` の `proposal.md` に保存する
    - structured judgment log では `observation_contract` を正として使う
    - 近似入力判定は `observation_contract.input_fidelity == "approximate"`、adaptive window 判定は `observation_contract.adaptive_window.expanded` を参照する
    - `ready[]` の `skill_scaffold_context` / `skill_creator_handoff` / `next_step_stub` は Step 7 の適用アクションの構造化入力になる
    - `skill` 候補では `skill_creator_handoff.presentation_block`（永続化後）を **コードブロックごと**チャットに出してよい。path 単体ではなく target repo + handoff file + 手順のセットで示す（別 repo 時は現在の CWD で実行しない旨を明示）
    - `ready` が 0 件の場合: `learning_feedback` を含む enriched output を出し、その後 Phase 5 へ進む
 6. ready proposal が 1 件以上ある場合だけ、proposal の `selection_prompt` を使って 1 回だけ候補選択を受け付けてよい（optional commit step — 応答がなければ pending のまま Phase 5 へ進む）
    - ユーザーが番号を答えた場合は、その番号を `skill_miner_decision.py --candidate-index <N>` に渡す
    - `candidate-index` は 1-based。数値であり、`1 <= N <= ready_count` を検証する
    - `defer` / `reject` を選んだ場合も `skill_miner_decision.py` で `user-decision-file` を生成する
    - 選択を受け付けなかった場合も Phase 4 は正常完了とし、全候補が `user_decision=null` で decision log に記録される
 7. 自動判断 — 適用アクション（`skill-applier` の Dispatch Rules に従う）:
    - `suggested_kind == "CLAUDE.md"`: `references/claude-md-apply.md` に従い diff preview → apply
    - `suggested_kind == "skill"`: `references/skill-scaffold.md` に従い scaffold context を提示
    - `suggested_kind == "hook"` / `"agent"`: `references/hook-agent-nextstep.md` に従い設計案を提示 → 次セッションへ
    - ユーザーが「あとで」「今回は見送る」と答えた場合は `skill_miner_decision.py` で `defer` / `reject` を記録する
 8. user decision を受け取った場合:
    - `skill_miner_decision.py --output-file "$SESSION_TMP/user-decision.json"` を実行する
    - 同じ prepare / judge / **classification overlay（初回 proposal 時と同一の `--classification-file` 集合）** / decision-log-path / handoff-dir を使って `skill_miner_proposal.py --user-decision-file "$SESSION_TMP/user-decision.json"` を再実行する
    - 再実行結果を Phase 4 の最終 persistence 状態として扱う
 9. ユーザー向けの状態一行（全文は artifact へ）:

```
### パターン提案
候補 {triaged_total} 件のうち {ready_count} 件をすぐ適用できます（現在のリポジトリ向け {ready_current_repo_count} / 別リポジトリ向け {ready_other_repo_count} / 要確認 {ready_uncertain_count}）。`proposal.json` の `summary` と一致させる。compact 表と `proposal.md` を参照。
```

0 件の場合:

```
### パターン提案
観測は行ったが提案条件を満たす候補なし（観測窓 7 日）。観測サマリはチャットに短く。
```

### Phase 5: Session Summary

最後にセッション全体の成果と次の一手を散文 3-5 行でまとめる。Phase 1.5 のダイジェスト（日の概観）と内容が重複しないように、ここでは成果物と次の一手に絞る。箇条書きは使わず、読み流せる散文にする。

**§5-5（必須）:** 同じブロック内に、次をそれぞれ 1–3 行・箇条書き可で含める。

1. **Mixed-scope 注記**: 複数種のソースを横断して再構成している旨
2. **再構成元の要約**: 参照したソースの種類（Git / AI 会話ログ / 閲覧履歴 等）を短く

**チャット品質:** → `## Chat Output Policy` を参照。英語の内部推理・shell 実行ログ・エージェント定型文は出力直前に確認して除去する。

```
### セッション完了

今日は N 件のソースから活動を収集し、{自分用/共有用}日報、投稿下書き、パターン提案 Y 件まで進めました。
{提案のうち N 件はすぐに適用可能です。 / 今回は提案条件を満たす候補はありませんでした。}
{次のアクションがあれば 1 文で。}

**Mixed-scope（再構成の前提）**
- （例）1 日のログと workspace ローカルの変更ログを横断して再構成しています。

**再構成元**
- （例）Git の変更履歴, Claude の会話ログ, Codex の会話ログ, ブラウザの閲覧履歴（成功した source の表示名のみ列挙）
```

## Chat Output Policy

チャット出力は **semi-final summary** に徹する。artifact の全文をチャットに流さない。

### chat に出すもの（positive list）

1. ソース収集結果の要約（Phase 1 形式のテキスト）
2. DayTrace ダイジェスト（Phase 1.5、3-5 行の散文）
3. 各 artifact の状態一行 + 保存パス（Phase 2・3）
4. パターン提案の compact 表 + 適用アクション（Phase 4、条件付き）
5. selection prompt（Phase 4、条件付き）
6. セッション完了サマリ（Phase 5、散文 3-5 行）
7. §5-5 の mixed-scope 注記と再構成元（表示名で列挙）

### chat に出してはいけないもの（禁止リスト）

以下は artifact や内部メモに閉じ、チャットに一切出さない。

| 禁止対象 | 代替 |
|---------|------|
| 英語の内部推理・思考ログ | 日本語の状態一行に要約する |
| shell 実行ログ・コマンド出力の逐語転記 | スクリプト名を出さず、結果だけ日本語で伝える |
| `Continuing autonomously` 等のエージェント定型文 | 状態一行のみ |
| `[DayTrace]` / `[DayTrace:trace]` / `Phase N:` 形式の行 | 出さない |
| 内部スクリプト名（`skill_miner_prepare.py` 等） | 「パターン抽出」のように意味ベースの日本語に |
| artifact の全文（日報・下書き・提案本文） | 要約と保存パスのみ |
| 長い根拠一覧 | artifact 内の `### 参考: 根拠一覧` に委ねる |
| 内部状態語（`candidate_id`, `triage_status` 等） | 出さない |

### Source 名の正規化（表示名マッピング）

ユーザー向けの文中（根拠文・サマリ・再構成元の列挙）では、以下の表示名を使う。
Phase 1 収集要約の `- git-history (12 events)` のような**機械的な一覧行は例外**とし、内部名のままでよい。

| source 名（内部） | 表示名（日本語） |
|-----------------|----------------|
| `git-history` | `Git の変更履歴` |
| `claude-history` | `Claude の会話ログ` |
| `codex-history` | `Codex の会話ログ` |
| `chrome-history` | `ブラウザの閲覧履歴` |
| `workspace-file-activity` | `ワークスペースのファイル変更` |

## Output Order

ユーザー向けチャットは次の順で出す（`docs/output-polish.md` §6）。スキップしたフェーズも **見出し + 一行の状態** は出す。

1. Phase 1 のソース収集要約（詳細。圧縮しない）
2. Phase 1.5 の DayTrace ダイジェスト（3-5 行の散文）
3. Phase 2 の状態一行 + 日報（本文は artifact が正。チャットは要約とパスのみ。`daily-report` SKILL に準拠）
4. Phase 2 の共有用（条件付き。同様にファイル保存を明示）
5. Phase 3 の状態一行 + 投稿下書き（本文は `post-draft.md`。チャットは要約とパス）
6. Phase 4 の状態一行 + compact 表（`skill-miner`）+ 適用アクション（条件付き）
7. Phase 5 のセッションサマリ（散文）+ **§5-5 の mixed-scope 注記と再構成元**

`[DayTrace]` / `[DayTrace:trace]` / `Phase N:` 形式のオーケストレーション行はユーザーに出さない。
日報・投稿下書き・提案の**全文**はチャットに流さず artifact を正とする（切れ・上限対策）。

### Artifact と再構成元

Markdown artifact の本文に mixed-scope 全文を必須とはしない（§5-5）。**チャット**の Phase 5 で必須。個別 skill が artifact に注記を入れてもよい。

### Structured Judgment Fields（内部デバッグ用・禁止）

以下は仕様上の「内部トレース例」として文書に残すが、**ユーザー向けチャットに一切出さない**（コピペ禁止）。

```
[DayTrace:trace] Phase 1: source_count={N} | success={N} | skipped={N} | error={N} | degrade_level={full|limited|empty}
[DayTrace:trace] Phase 2: mode={自分用|共有用|両方} | mode_source={extracted|default|asked} | item_count={N} | primary_evidence={git|ai_history|mixed}
[DayTrace:trace] Phase 4: ready_count={N} | needs_research_count={N} | rejected_count={N} | detail_invoked={true|false} | judge_invoked={true|false}
[DayTrace:trace] Phase 3: selected_group_id={id} | topic_tier={1|2|3} | topic_reason={reason} | reader={auto|override} | skipped={true|false} | skip_reason={reason}
```

（注: 上記 Phase 番号は「実行フェーズ」に対応。Post Draft=3, Pattern Mining=4。）

### Escalation Conditions（曖昧性解消 `docs/output-polish.md` §10）

Ask は、出力品質または正確性に影響する曖昧性が残る場合**のみ**。出力種別ごと最大 2 ターン。次は従来どおり許可する:

- **共有用日報に機密情報が含まれる可能性がある場合**: `mode=共有用` かつ `sources[]` に `all-day` スコープのみ（workspace ログなし）の場合、「共有用日報に個人端末の全日ログが含まれますが、このまま進めますか？」と 1 回だけ確認
- **ready proposal の適用を進める場合**: proposal の `selection_prompt` に従う 1 回の候補選択と、適用 action の完了確認を入れてよい（強制ではない）
- **CLAUDE.md 即時反映時**: 既存の仕様通り diff preview を出して確認を待つ

**禁止**: 「技術寄り / 振り返り寄り」など抽象的な文体だけを選ばせる Ask。聞かなくても十分な品質なら聞かない。

## Sub-Skill Reference

各フェーズの出力ルールは個別 skill の SKILL.md を参照する。

- Phase 2: `skills/daily-report/SKILL.md` — Output Rules, Confidence Handling, Mixed-Scope Note Rules, Graceful Degrade
- Phase 3: `skills/post-draft/SKILL.md` — Narrative Policy, Reader Policy, Output Rules, Graceful Degrade
- Phase 4 (Mining): `skills/skill-miner/SKILL.md` — Classification Rules, Pre-Classification Contract, Oversized Cluster Guard, Proposal Format, Deep Research Rules, Triage Rules
- Phase 4 (Apply): `skills/skill-applier/SKILL.md` — CLAUDE.md Immediate Apply, Skill Scaffold Draft, Hook/Agent Next Step, Decision Writeback

本 skill は orchestration のみを担い、個別 skill の出力フォーマットや品質基準を上書きしない。

### Imported Contracts（各 skill から引き継ぐ必須ルール）

Phase 2 (daily-report):
- 出力先頭は `## 日報 YYYY-MM-DD`。活動項目 3-6 個
- 共有用はですます調で書く
- 自分用は根拠 inline、共有用は末尾の `### 参考: 根拠一覧` にまとめる
- 本文の語彙は行動レベルに変換し、実装名は根拠に退避する。根拠は短文化する
- Confidence ラベル（`high` / `medium` / `low`）は明示せず、根拠の具体性で表現する。low は inline 注記
- 低信頼度データは「一回・補助・裏方」。単独で活動項目を構成しない
- 共有用では中心テーマ 1-2 本に絞り、他は補助的な出来事として扱う
- `確認したい点` セクションは作らない
- mixed-scope: セッション chat の Phase 5（§5-5）で必須。artifact 本文への挿入は任意（`docs/output-polish.md` §5-5）

Phase 4 Mining (skill-miner):
- 分類は `CLAUDE.md` / `skill` / `hook` / `agent` の 4 つのみ。`plugin` は使わない
- `suggested_kind` は Python 事前付与が基本。`classification-prompt.md` に従う overlay で LLM が上書きしてよい（**曖昧候補のみ** overlay を生成）。override は理由記録が必須
- `oversized_cluster` 等の blocking signal が未解消なら `ready` に入れない
- `origin_hint` / `user_signal_strength` / `contamination_signals` に internal-signal guard が出ている候補は、legacy metadata 欠落 (`origin_hint=""`) を除き `ready` に上げない
- proposal の根拠は `evidence_items[]` だけで完結させ、raw history を再読込しない
- `0 件` でも enriched output（観測サマリ + 成長兆候）を返す
- contamination signal がある候補は proposal 本文に `注記:` を出し、黙って昇格させない

Phase 4 Fixation (skill-applier):
- `CLAUDE.md` → diff preview → apply 成功時のみ `completed` 記録
- `skill` → scaffold context 提示 → skill-creator handoff
- `hook` / `agent` → 設計案提示 → 次セッションへ送る
- 成功未確認の `hook` / `agent` は `adopt` を確定させず `defer` 扱い

Phase 3 (post-draft):
- 文体はですます調
- 出力は `# タイトル案` / 背景 / 今日の中心 / 気づき の構成
- タイトルは初見読者が意味を感じ取れる表現にする。内部コンポーネント名だけのタイトルは避ける
- 本文の語彙は行動レベルに変換し、実装名は narrative に必要な範囲でのみ出す
- 転換点を narrative の起伏として明示的に拾う
- 低信頼度データは「一回・補助・裏方」。単独で主題や転換点を構成しない
- 主題選定は AI+Git 共起 → AI 密度 → 最大イベント数の 3 段フォールバック
- Confidence ラベルは明示せず、弱い箇所だけ inline 注記。`確認したい点` セクションは作らない

## Error Handling

- Phase 1 で全ソース失敗 → 空日報を出して Phase 5 へ（Phase 3・4 はスキップ）
- Phase 3（Post Draft）の projection 実行失敗 → ユーザー向けにファイル単位で失敗を明示し、Phase 4（Pattern Mining）へ進む
- Phase 4 の prepare 実行失敗 → エラーを短い状態行に記録し、Phase 5 へ進む
- Phase 4 の detail/judge 実行失敗 → 当該候補を `needs_research` のまま残し、次の候補または Phase 5 へ
- いずれのフェーズ失敗もセッション全体を中断しない

状態行の例（`[DayTrace]` なし）:

```
### パターン提案
prepare でエラー（例: timeout）→ 提案フェーズはスキップしてセッションサマリへ進みます
```

## Decision Rules Summary

| 判断ポイント | 条件 | Yes | No |
|-------------|------|-----|-----|
| 続行 vs 停止 | success >= 1 | 続行 | 空日報 → Phase 5 |
| 共有用追加 | mode 未指定 & total_groups >= 5 | 両方生成 | 自分用のみ |
| 追加調査実行 | needs_research >= 1 | detail + judge 自動実行 | スキップ |
| 分類判定 | prepare の heuristic が事前付与 | **曖昧候補のみ** overlay。無い候補は heuristic + guardrail。overlay 破損時は heuristic のみ | guardrail で最終確定 |
| contamination guard | `origin_hint/user_signal_strength/contamination_signals` を確認 | internal 疑いなら `needs_research` に留める | そのまま継続 |
| 適用アクション | `CLAUDE.md` → diff preview / `skill` → scaffold | 該当アクションを表示 | hook/agent は次セッションへ |
| 投稿下書き | AI + Git 共起 or groups >= 4 | 生成 | スキップ |

## Output Skeleton

スキップした Phase も **見出し + 一行** は出す。本文は artifact を正とし、チャットは要約とパス中心。

```
### ソース収集の結果
{source lines}
→ N ソースで続行します

## 今日の DayTrace ダイジェスト
{1日の概観 3-5行}

### 日報
{保存パス・モード・一行要約}（`report-private.md` / `report-share.md`）

### 投稿下書き
{保存パス or スキップ理由}（`post-draft.md`）

### パターン提案
{compact 表 + 一行要約}。詳細は `proposal.md`
{適用アクション — 条件付き}

### セッション完了
{散文 3-5行}
**Mixed-scope（再構成の前提）** …
**再構成元** …
```

## Completion Check

以下を満たすまでセッションを完了としない。

- 1 回の依頼で Phase 1 〜 5 が完走している（実行順: 1 → 1.5 → 2 → 3 Post Draft → 4 Mining → 5）
- ask が発生する場合も `Escalation Conditions` / §10 に限定されている
- ユーザー向けチャットに `[DayTrace]` / `[DayTrace:trace]` を出していない
- チャットに英語の内部推理・shell 実行ログ・`Continuing autonomously` 等のエージェント定型文が出ていない（Chat Output Policy 参照）
- artifact の全文がチャットに流れておらず、要約と保存パスのみになっている
- ユーザー向けの文中（根拠・サマリ・再構成元）で source 名が表示名に正規化されている
- スキップした Phase も見出しと一行の状態が出ている
- ソース欠損があっても Phase 5 まで到達している
- `output_dir` が得られるときは artifact の保存パスをファイル単位で示している
- 個別 skill の出力品質基準が維持されている
- `suggested_kind` が事前付与されており、LLM override した場合は理由が内部メモまたはユーザー向け短文に残っている
- contamination signal がある候補を ready に昇格させた場合、その理由が記録されている
- Phase 4 は 0 件でも enriched output（観測サマリ + 成長兆候）がチャットに短く出ている
- Phase 4 で user decision があった場合は `skill_miner_proposal.py --user-decision-file` まで再実行されている
- Phase 5 でセッション全体のサマリと **§5-5（mixed-scope + 再構成元）** が出ている
- 判断をスキップした場合もその理由がユーザー向けに一行で分かる
