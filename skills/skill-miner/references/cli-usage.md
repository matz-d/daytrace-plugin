# CLI 詳細仕様 & Prepare Output Reading Guide

## CLI コマンド詳細

スクリプトは plugin 直下の `scripts/` ディレクトリにある。
`SKILL.md` のあるディレクトリから `../..` を辿った先を `${CLAUDE_PLUGIN_ROOT}` として扱う。

### skill_miner_prepare.py

提案フェーズ用。全セッションを圧縮 candidate view で横断分析する。

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_prepare.py --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl
```

広域観測:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_prepare.py --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl --all-sessions
```

補足:

- デフォルト観測窓は `--days 7`
- `--all-sessions` は workspace 制限を外すだけで、日数窓は維持する
- `workspace` モード（`--all-sessions` を付けない通常実行。`--workspace` 未指定時は `cwd` を使う）だけ、packet / candidate が少なすぎる場合に 30 日へ自動拡張する
- B0 観測（`primary_intent` / clustering / quality gate のどこを優先的に改善すべきかを見るための実データ観測）では、full-history 相当を見たい場合に十分長い `--days` を明示する
- B0 の判断枠と実行手順の詳細は `references/b0-observation.md` を参照する

例:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_prepare.py --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl --all-sessions --days 3650 --dump-intents
```

### skill_miner_detail.py

選択後の detail 再取得。

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_detail.py --refs "<session_ref_1>" "<session_ref_2>"
```

### skill_miner_research_judge.py

追加調査後の結論判定。

```bash
SESSION_TMP="${SESSION_TMP:-$(mktemp -d "${TMPDIR:-/tmp}/daytrace-session-XXXXXX")}"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_research_judge.py --candidate-file "$SESSION_TMP/prepare.json" --candidate-id "<candidate_id>" --detail-file "$SESSION_TMP/detail.json"
```

### skill_miner_proposal.py

最終 proposal 組み立て。

```bash
SESSION_TMP="${SESSION_TMP:-$(mktemp -d "${TMPDIR:-/tmp}/daytrace-session-XXXXXX")}"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_proposal.py --prepare-file "$SESSION_TMP/prepare.json" --judge-file "$SESSION_TMP/judge.json" --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl --skill-creator-handoff-dir ~/.daytrace/skill-creator-handoffs > "$SESSION_TMP/proposal.json"
```

### skill_miner_decision.py

proposal 選択結果を `--user-decision-file` 互換 JSON に正規化する helper。`proposal.json` は `skill_miner_proposal.py` の stdout を redirect して作る。`candidate-index` は 1-based（最初の候補は `1`）。

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_decision.py --proposal-file "$SESSION_TMP/proposal.json" --candidate-index 1 --decision adopt --completion-state completed --output-file "$SESSION_TMP/user-decision.json"
```

補足:

- `skill_miner_prepare.py` と `skill_miner_proposal.py` は同じ `--decision-log-path` を共有する
- 一時 JSON は固定 `/tmp/*.json` を避け、`mktemp -d` で作った session-specific temp dir に置く
- `skill_miner_decision.py` が出す JSON は `skill_miner_proposal.py --user-decision-file` にそのまま渡せる
- handoff bundle は `--skill-creator-handoff-dir` に保存される
- デモ時に副作用を隔離したい場合も、`mktemp -d` で作った session-specific temp dir を使う

### 実コマンド例

repo root をカレントディレクトリとした場合:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_prepare.py --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_prepare.py --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl --all-sessions
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_prepare.py --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl --all-sessions --days 3650 --dump-intents
SESSION_TMP="${SESSION_TMP:-$(mktemp -d "${TMPDIR:-/tmp}/daytrace-session-XXXXXX")}"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_detail.py --refs "codex:abc123:1710000000"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_research_judge.py --candidate-file "$SESSION_TMP/prepare.json" --candidate-id "codex-abc123" --detail-file "$SESSION_TMP/detail.json"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_proposal.py --prepare-file "$SESSION_TMP/prepare.json" --judge-file "$SESSION_TMP/judge.json" --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl --skill-creator-handoff-dir ~/.daytrace/skill-creator-handoffs > "$SESSION_TMP/proposal.json"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_decision.py --proposal-file "$SESSION_TMP/proposal.json" --candidate-index 1 --decision adopt --completion-state completed --output-file "$SESSION_TMP/user-decision.json"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_proposal.py --prepare-file "$SESSION_TMP/prepare.json" --judge-file "$SESSION_TMP/judge.json" --decision-log-path ~/.daytrace/skill-miner-decisions.jsonl --skill-creator-handoff-dir ~/.daytrace/skill-creator-handoffs --user-decision-file "$SESSION_TMP/user-decision.json" > "$SESSION_TMP/proposal-final.json"
```

## Prepare Output Reading Guide

`skill_miner_prepare.py` の主な読みどころ:

- `candidates`
  - ranked cluster 一覧
- `candidates[].support`
  - 出現回数、source 多様性、直近性
- `candidates[].confidence`
  - 候補の強さ。`strong` / `medium` / `weak` / `insufficient`
- `candidates[].proposal_ready`
  - そのまま提案可能か
- `candidates[].triage_status`
  - `ready` / `needs_research` / `rejected`
- `candidates[].quality_flags`
  - 巨大クラスタや汎用クラスタなどの注意信号
- `candidates[].evidence_summary`
  - 根拠の短い要約
- `candidates[].representative_examples`
  - 候補の代表例
- `candidates[].session_refs`
  - 選択後 detail 取得に使う参照キー
- `candidates[].research_targets`
  - `needs_research` 候補で優先的に detail 取得する ref と理由
- `candidates[].research_brief`
  - 追加調査で何を確認し、どの基準で `ready` / `split` / `rejected` を判断するか
- `unclustered`
  - cluster に乗らなかった孤立 packet。原則として提案しない
- `summary`
  - packet 数、candidate 数、blocking の規模
- `config.effective_days`
  - 実際に使われた観測窓
- `config.adaptive_window`
  - しきい値、初期 packet / candidate 数、拡張理由
  - `config.adaptive_window.expanded` を adaptive window 判定の canonical key として読む
- `skill_miner_proposal.py` の出力
  - triage 済み candidate を人間向け proposal section に整形したもの

注意:

- `representative_examples` と `primary_intent` は圧縮済み
- path は `[WORKSPACE]` にマスクされる
- URL はドメインのみ残る
