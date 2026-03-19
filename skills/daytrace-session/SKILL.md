---
name: daytrace-session
description: >
  「今日の振り返りをお願い」の一言で、ローカルログの収集・日報生成・反復パターン提案・投稿下書きまで
  自律的に完走する統合セッション。振り返りをまとめて、全部やって、1日のまとめ、と言われた時に使う。
user-invocable: true
---

# DayTrace Session

1 回の依頼で「収集 → 日報 → パターン提案 → 追加調査 → 投稿下書き」まで自律的に完走するオーケストレーション skill。

## Goal

- 1 回の依頼で全フェーズを追加指示なしで完走する
- 各ステップで自己判断の理由を `[DayTrace]` プレフィックス付きで報告する
- ソース欠損やデータ不足でも止まらず、できる範囲で最後まで進む
- 最後に実施内容のサマリを返す
- ready proposal がある時は、Phase 3 の固定化アクションまで閉じる

やらないこと:

- 個別 skill の出力フォーマットや品質基準を上書きすること
- `Escalation Conditions` 以外で途中 ask を増やすこと
- フェーズ間で人の確認を待つこと（proposal 選択 / 固定化確認を除く）

## Inputs

- 対象日: 指定がなければ `today`
- workspace: 任意。補助フィルタ
- mode: `自分用` or `共有用`。未指定なら `自分用` + 条件付き共有用自動生成

## Entry Contract

- ask は入口 0 回を基本とする
- 「今日の振り返りをお願い」「1日のまとめ」「全部やっておいて」「今日の活動を整理して」などから日付を抽出する
- mode / workspace / topic / reader は自然言語から抽出できればそれを使い、取れなければデフォルト
- `Escalation Conditions` の例外を除き、途中で追加 ask しない
- Phase 3 の候補選択（`selection_prompt`）はセッション後半の optional commit step であり、ask ではない。選択がなければ pending のまま正常終了する

## Scripts

スクリプトはこの `SKILL.md` と同じ plugin 内の `scripts/` にある。
このディレクトリから `../..` を辿った先を `${CLAUDE_PLUGIN_ROOT}` として扱う。

Phase 1 Data Collection:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/daily_report_projection.py --date today --all-sessions
```

Phase 3 Pattern Mining:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_prepare.py --input-source auto --store-path ~/.daytrace/daytrace.sqlite3 --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl --all-sessions
```

Phase 3 Detail (conditional):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_detail.py --refs "<ref1>" "<ref2>"
```

Phase 3 Judge (conditional):

```bash
SESSION_TMP="${SESSION_TMP:-$(mktemp -d "${TMPDIR:-/tmp}/daytrace-session-XXXXXX")}"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_research_judge.py --candidate-file "$SESSION_TMP/prepare.json" --candidate-id "<id>" --detail-file "$SESSION_TMP/detail.json"
```

Phase 3 Proposal:

```bash
SESSION_TMP="${SESSION_TMP:-$(mktemp -d "${TMPDIR:-/tmp}/daytrace-session-XXXXXX")}"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_proposal.py --prepare-file "$SESSION_TMP/prepare.json" --judge-file "$SESSION_TMP/judge.json" --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl --skill-creator-handoff-dir ~/.daytrace/skill-creator-handoffs > "$SESSION_TMP/proposal.json"
```

Phase 3 Decision Writeback (conditional):

```bash
SESSION_TMP="${SESSION_TMP:-$(mktemp -d "${TMPDIR:-/tmp}/daytrace-session-XXXXXX")}"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_decision.py --proposal-file "$SESSION_TMP/proposal.json" --candidate-index 1 --decision adopt --completion-state completed --output-file "$SESSION_TMP/user-decision.json"
```

Phase 4 Post Draft (conditional):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/post_draft_projection.py --date today --all-sessions
```

workspace 指定がある場合は全コマンドに `--workspace /absolute/path` を追加する。

Pattern Mining の persistence ルール:

- `skill_miner_prepare.py` と `skill_miner_proposal.py` には同じ `--decision-log-path` を明示的に渡す
- Phase 3 の一時ファイルは固定 `/tmp/*.json` を使わず、`mktemp -d` 等で作った session-specific temp dir（例: `$SESSION_TMP/...`）に置く
- proposal に対するユーザー選択を保存する時は `skill_miner_decision.py` が出す `$SESSION_TMP/user-decision.json` を `skill_miner_proposal.py --user-decision-file` に渡す
- `skill_miner_proposal.py` には `--skill-creator-handoff-dir` も明示的に渡す
- 既定値は `~/.daytrace/...` だが、daytrace-session は副作用を見える化するため必ず CLI 引数で path を明示する
- デモや dry-run で副作用を隔離したい場合も、`mktemp -d` で作った session 専用 path に差し替える

## Execution Flow

5 つのフェーズを順に実行する。各フェーズで判断ログを出力し、追加指示なしで次に進む。

### Phase 1: Source Assessment

1. `daily_report_projection.py` を 1 回実行する
2. `sources[]` を読み、各ソースの `status` と `scope` を確認する
3. 判断ログを出力する:

```
[DayTrace] ログを収集しました
  git-history (12 events) — workspace scope
  claude-history (8 events) — all-day scope
  chrome-history → 権限不足のためスキップ
  codex-history (3 events) — all-day scope
  workspace-file-activity (24 events) — workspace scope
  → 4 ソースで続行します
```

4. 判断ルール:
   - `summary.source_status_counts.success >= 1` → 続行
   - `success == 0` → 空日報を出して Phase 5 へ飛ぶ

### Phase 1.5: DayTrace ダイジェスト

Phase 1 完了直後、Phase 2 に入る前に「今日の DayTrace ダイジェスト」を 3-5 行の散文で出す。
これは全フェーズの結果を先読みするものではなく、ログから読み取れる 1 日の概観を先に見せるためのもの。

```
## 今日の DayTrace ダイジェスト
今日は N 件のソースから X 件の活動を観測しました。
{主な活動の 1-2 文要約}。
パターン候補と投稿下書きの結果はこの後に続きます。
```

### Phase 2: Daily Report

1. Phase 1 の中間 JSON を使って日報を生成する
2. 出力ルールは `daily-report` skill の SKILL.md に従う
3. mode が明示されている場合はその mode で生成する
4. 自動判断 — 共有用の追加生成:
   - 条件: mode 未指定かつ `summary.total_groups >= 5`
   - 満たす場合: `自分用` に加えて `共有用` も自動生成する
   - 満たさない場合: `自分用` のみ
5. 判断ログは 1 行に圧縮する:

```
[DayTrace] 日報を生成しました（自分用 + 共有用）
```

または:

```
[DayTrace] 日報を生成しました（自分用のみ、グループ 3 件）
```

### Phase 3: Pattern Mining & Proposals

1. `skill_miner_prepare.py` を 1 回実行する
2. `candidates[]` を確認する
3. 自動判断 — 追加調査:
   - 条件: `needs_research` 候補が 1 件以上
   - 満たす場合: 各 `needs_research` 候補の `research_targets` 上位 refs で `skill_miner_detail.py` → `skill_miner_research_judge.py` を自動実行する
   - 1 候補あたり最大 5 refs、追加調査は 1 回まで
4. 分類判定:
   - `suggested_kind` は Python の `infer_suggested_kind()` が事前付与済み
   - LLM は明確な理由がある場合のみ override する（Pre-Classification Contract 参照）
   - `agent` は Python も条件付きで推定するが、条件が厳しく付与は稀。LLM は evidence を確認して override してよい（Pre-Classification Contract 参照）
5. 提案の組み立て:
    - prepare 出力と judge 出力（あれば）を `skill_miner_proposal.py` に渡し、stdout を shell redirect で `$SESSION_TMP/proposal.json` に保存する
    - `proposal.json` は machine-actionable JSON（contract 詳細は `skill-miner/references/proposal-json-contract.md`）
    - ユーザー向けには `markdown` フィールドを出力する
    - structured judgment log では `observation_contract` を正として使う
    - 近似入力判定は `observation_contract.input_fidelity == "approximate"`、adaptive window 判定は `observation_contract.adaptive_window.expanded` を参照する
    - `ready[]` の `skill_scaffold_context` / `skill_creator_handoff` / `next_step_stub` は Step 7 の固定化アクションの構造化入力になる
    - `ready` が 0 件の場合: `learning_feedback` を含む enriched output が自動生成され、そのまま Phase 4 へ進む
 6. ready proposal が 1 件以上ある場合だけ、proposal の `selection_prompt` を使って 1 回だけ候補選択を受け付けてよい（optional commit step — 応答がなければ pending のまま Phase 4 へ進む）
    - ユーザーが番号を答えた場合は、その番号を `skill_miner_decision.py --candidate-index <N>` に渡す
    - `candidate-index` は 1-based。数値であり、`1 <= N <= ready_count` を検証する
    - `defer` / `reject` を選んだ場合も `skill_miner_decision.py` で `user-decision-file` を生成する
    - 選択を受け付けなかった場合も Phase 3 は正常完了とし、全候補が `user_decision=null` で decision log に記録される
 7. 自動判断 — 固定化アクション（`skill-applier` の Dispatch Rules に従う）:
    - `suggested_kind == "CLAUDE.md"`: `references/claude-md-apply.md` に従い diff preview → apply
    - `suggested_kind == "skill"`: `references/skill-scaffold.md` に従い scaffold context を提示
    - `suggested_kind == "hook"` / `"agent"`: `references/hook-agent-nextstep.md` に従い設計案を提示 → 次セッションへ
    - ユーザーが「あとで」「今回は見送る」と答えた場合は `skill_miner_decision.py` で `defer` / `reject` を記録する
 8. user decision を受け取った場合:
    - `skill_miner_decision.py --output-file "$SESSION_TMP/user-decision.json"` を実行する
    - 同じ prepare / judge / decision-log-path / handoff-dir を使って `skill_miner_proposal.py --user-decision-file "$SESSION_TMP/user-decision.json"` を再実行する
    - 再実行結果を Phase 3 の最終 persistence 状態として扱う
 9. 判断ログは 1 行に圧縮する:

```
[DayTrace] パターン検出: 候補 6 件中 2 件を提案（CLAUDE.md ×1, skill ×1）、1 件は有望候補、追加調査 1 件実施済み
```

0 件の場合:

```
[DayTrace] パターン検出: N セッションを観測、M 件のクラスタを検出したが提案条件を満たす候補なし（観測窓 7 日）
```

### Phase 4: Post Draft (conditional)

1. 自動判断 — 投稿下書きの生成:
   - 条件（いずれか 1 つ以上）:
     - Phase 1 の `sources` に `git-history` と (`claude-history` or `codex-history`) が両方 success
     - `summary.total_groups >= 4`
   - 満たす場合:
     - `post_draft_projection.py` を 1 回実行する
     - `post-draft` skill の SKILL.md に従って narrative draft を生成する
   - 満たさない場合: スキップ
2. 判断ログは 1 行に圧縮する:

```
[DayTrace] 投稿下書きを生成しました（AI + Git 共起パターンをもとに構成）
```

スキップ時:

```
[DayTrace] 投稿下書き: 生成条件を満たさないためスキップ (groups: 2, AI+Git 共起: なし)
```

### Phase 5: Session Summary

最後に全フェーズの実施結果を 3-5 行の散文でまとめる。チェックリストではなく、DayTrace がこのセッションで何をしたかの要約として書く。

```
[DayTrace] セッション完了
今日は N 件のソースから日報を生成し、パターン候補 X 件のうち Y 件を提案しました。
CLAUDE.md への適用候補が 1 件あり、diff preview を表示済みです。
skill 候補が 1 件あり、scaffold context を提示済みです。
投稿下書きは AI + Git の共起パターンをもとに 1 本生成しています。
```

## Output Order

各フェーズの出力は以下の順序で連続して出力する。
この順序は必須であり、スキップされたフェーズも判断ログだけは必ず出力する。

1. Phase 1 の判断ログ（ソース判定の詳細。自律性を見せる最重要ポイントなので圧縮しない）
2. Phase 1.5 の DayTrace ダイジェスト（3-5 行の散文で 1 日の概観を先に見せる）
3. Phase 2 の判断ログ（1 行）+ 日報出力（`daily-report` SKILL.md の Output Rules に準拠）
4. Phase 2 の共有用日報（条件付き）
5. Phase 3 の判断ログ（1 行）+ 提案出力（`skill-miner` SKILL.md の Proposal Format に準拠。0 件でも enriched output を表示）
6. Phase 3 の固定化アクション出力（条件付き: CLAUDE.md diff preview / skill scaffold context）
7. Phase 4 の判断ログ（1 行）+ 下書き出力（`post-draft` SKILL.md の Output Rules に準拠）
8. Phase 5 のセッションサマリ（散文）

判断ログは `[DayTrace]` プレフィックスで統一する。
Phase 1 のログだけ詳細に出し、Phase 2-4 のログは 1 行に圧縮する。
日報・提案・下書きの本文はそのまま読めるように、判断ログと明確に区切る。
`[DayTrace:trace]` タグの structured fields はユーザー向け出力に含めない。

### Structured Judgment Fields（内部デバッグ用）

以下の structured fields はデバッグ・再現性の確保のために定義する。
**これらはユーザー向け出力には含めない。** ユーザーに見せる判断ログは各 Phase セクションで定義された人間可読な 1 行メッセージ（例: `[DayTrace] 日報を生成しました（自分用 + 共有用）`）のみ。

structured fields は内部トレースとしてのみ使用する:

```
[DayTrace:trace] Phase 1: source_count={N} | success={N} | skipped={N} | error={N} | degrade_level={full|limited|empty}
[DayTrace:trace] Phase 2: mode={自分用|共有用|両方} | mode_source={extracted|default|asked} | item_count={N} | primary_evidence={git|ai_history|mixed}
[DayTrace:trace] Phase 3: ready_count={N} | needs_research_count={N} | rejected_count={N} | detail_invoked={true|false} | judge_invoked={true|false}
[DayTrace:trace] Phase 4: selected_group_id={id} | topic_tier={1|2|3} | topic_reason={reason} | reader={auto|override} | skipped={true|false} | skip_reason={reason}
```

`[DayTrace:trace]` タグ付きの行をユーザー向け出力ストリームに混入させてはならない。

### Escalation Conditions

以下の場合のみ確認を入れてよい:

- **共有用日報に機密情報が含まれる可能性がある場合**: `mode=共有用` かつ `sources[]` に `all-day` スコープのみ（workspace ログなし）の場合、「共有用日報に個人端末の全日ログが含まれますが、このまま進めますか？」と 1 回だけ確認
- **ready proposal の固定化を進める場合**: proposal の `selection_prompt` に従う 1 回の候補選択と、固定化 action の完了確認を入れてよい
- **CLAUDE.md 即時反映時**: 既存の仕様通り diff preview を出して確認を待つ
- それ以外は ask せずに degrade して進む

## Sub-Skill Reference

各フェーズの出力ルールは個別 skill の SKILL.md を参照する。

- Phase 2: `skills/daily-report/SKILL.md` — Output Rules, Confidence Handling, Mixed-Scope Note Rules, Graceful Degrade
- Phase 3 (Mining): `skills/skill-miner/SKILL.md` — Classification Rules, Pre-Classification Contract, Oversized Cluster Guard, Proposal Format, Deep Research Rules, Triage Rules
- Phase 3 (Fixation): `skills/skill-applier/SKILL.md` — CLAUDE.md Immediate Apply, Skill Scaffold Draft, Hook/Agent Next Step, Decision Writeback
- Phase 4: `skills/post-draft/SKILL.md` — Narrative Policy, Reader Policy, Output Rules, Graceful Degrade

本 skill は orchestration のみを担い、個別 skill の出力フォーマットや品質基準を上書きしない。

### Imported Contracts（各 skill から引き継ぐ必須ルール）

Phase 2 (daily-report):
- 出力先頭は `## 日報 YYYY-MM-DD`。活動項目 3-6 個。各項目に `根拠:` を付ける
- 本文の語彙は行動レベルに変換し、実装名は根拠に退避する。根拠は短文化する
- Confidence ラベル（`high` / `medium` / `low`）は明示せず、根拠の具体性で表現する。low は inline 注記
- 低信頼度データは「一回・補助・裏方」。単独で活動項目を構成しない
- 共有用では中心テーマ 1-2 本に絞り、他は補助的な出来事として扱う
- `確認したい点` セクションは作らない
- mixed-scope 時は冒頭 1 行で scope を伝え、詳細はフッターに置く

Phase 3 Mining (skill-miner):
- 分類は `CLAUDE.md` / `skill` / `hook` / `agent` の 4 つのみ。`plugin` は使わない
- `suggested_kind` は Python 事前付与。LLM override は理由記録が必須
- `oversized_cluster` 等の blocking signal が未解消なら `ready` に入れない
- `origin_hint` / `user_signal_strength` / `contamination_signals` に internal-signal guard が出ている候補は、legacy metadata 欠落 (`origin_hint=""`) を除き `ready` に上げない
- proposal の根拠は `evidence_items[]` だけで完結させ、raw history を再読込しない
- `0 件` でも enriched output（観測サマリ + 成長兆候）を返す
- contamination signal がある候補は proposal 本文に `注記:` を出し、黙って昇格させない

Phase 3 Fixation (skill-applier):
- `CLAUDE.md` → diff preview → apply 成功時のみ `completed` 記録
- `skill` → scaffold context 提示 → skill-creator handoff
- `hook` / `agent` → 設計案提示 → 次セッションへ送る
- 成功未確認の `hook` / `agent` は `adopt` を確定させず `defer` 扱い

Phase 4 (post-draft):
- 出力は `# タイトル案` / 背景 / 今日の中心 / 気づき の構成
- タイトルは初見読者が意味を感じ取れる表現にする。内部コンポーネント名だけのタイトルは避ける
- 本文の語彙は行動レベルに変換し、実装名は narrative に必要な範囲でのみ出す
- 転換点を narrative の起伏として明示的に拾う
- 低信頼度データは「一回・補助・裏方」。単独で主題や転換点を構成しない
- 主題選定は AI+Git 共起 → AI 密度 → 最大イベント数の 3 段フォールバック
- Confidence ラベルは明示せず、弱い箇所だけ inline 注記。`確認したい点` セクションは作らない

## Error Handling

- Phase 1 で全ソース失敗 → 空日報を出して Phase 5 へ
- Phase 3 の prepare 実行失敗 → エラーを判断ログに記録し、Phase 4 へ進む
- Phase 3 の detail/judge 実行失敗 → 当該候補を `needs_research` のまま残し、次の候補または Phase 4 へ
- Phase 4 の projection 実行失敗 → エラーを判断ログに記録し、Phase 5 へ進む
- いずれのフェーズ失敗もセッション全体を中断しない

判断ログ例:

```
[DayTrace] パターン検出でエラーが発生しました: skill_miner_prepare.py timeout → スキップして投稿下書きへ進みます
```

## Decision Rules Summary

| 判断ポイント | 条件 | Yes | No |
|-------------|------|-----|-----|
| 続行 vs 停止 | success >= 1 | 続行 | 空日報 → Phase 5 |
| 共有用追加 | mode 未指定 & total_groups >= 5 | 両方生成 | 自分用のみ |
| 追加調査実行 | needs_research >= 1 | detail + judge 自動実行 | スキップ |
| 分類判定 | suggested_kind は Python が事前付与 | LLM は override 理由がある場合のみ変更 | そのまま使用 |
| contamination guard | `origin_hint/user_signal_strength/contamination_signals` を確認 | internal 疑いなら `needs_research` に留める | そのまま継続 |
| 固定化アクション | `CLAUDE.md` → diff preview / `skill` → scaffold | 該当アクションを表示 | hook/agent は次セッションへ |
| 投稿下書き | AI + Git 共起 or groups >= 4 | 生成 | スキップ |

## Output Skeleton

全 Phase の出力は以下のスケルトンに従う。スキップした Phase も判断ログだけは必ず出力する。

```
[DayTrace] ログを収集しました
  {source lines}
  → N ソースで続行します

## 今日の DayTrace ダイジェスト
{1日の概観 3-5行}

[DayTrace] 日報を生成しました（{mode}）
{日報本文}

[DayTrace] パターン検出: {summary line}
{proposal markdown — 0件でも enriched output を含む}
{固定化アクション出力 — 条件付き}

[DayTrace] 投稿下書き: {summary line}
{下書き本文 or スキップ理由}

[DayTrace] セッション完了
{セッションサマリ 3-5行}
```

## Completion Check

以下を満たすまでセッションを完了としない。

- 1 回の依頼で Phase 1 〜 5 が完走している
- ask が発生する場合も `Escalation Conditions` 内に限定されている
- 各判断ポイントで `[DayTrace]` 付きの判断ログが出力されている
- スキップした Phase も判断ログが出力されている
- ソース欠損があっても Phase 5 まで到達している
- 個別 skill の出力品質基準が維持されている
- `suggested_kind` が事前付与されており、LLM override した場合は理由が判断ログに記録されている
- contamination signal がある候補を ready に昇格させた場合、その理由が判断ログに記録されている
- Phase 3 は 0 件でも enriched output（観測サマリ + 成長兆候）が出力されている
- Phase 3 で user decision があった場合は `skill_miner_proposal.py --user-decision-file` まで再実行されている
- Phase 5 でセッション全体のサマリが出ている
- 判断をスキップした場合もその理由が記録されている
