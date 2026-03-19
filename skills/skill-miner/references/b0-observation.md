# B0 Observation Guide

`skill-miner` の B0 は、`primary_intent` / clustering / quality gate のどこを優先的に改善すべきかを見るための実データ観測タスクであり、固定の結論を保存するためのものではない。
その時点の履歴を観測し、`B: feature extraction` / `C: clustering` / `D: quality gate` のどこを優先すべきかを決めるための判断フレームである。

このファイルには固定の考え方だけを書く。
実測値、代表サンプル、今回の優先順位は都度の report に書く。

## Purpose

- `primary_intent` が壊れているのか
- cluster / similarity が merge しすぎているのか
- quality gate が厳しすぎるのか

を切り分ける。

## Command Template

通常運用とは分けて、B0 では明示的に長い観測窓を指定する。

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill_miner_prepare.py --all-sessions --days 3650 --top-n 5 --max-unclustered 5 --dump-intents
```

補足:

- 通常運用の `skill-miner` は `--days 7` がデフォルトで、workspace モードだけ必要時に 30 日へ自動拡張する
- ここでいう workspace モードは、`--all-sessions` を付けない通常実行を指す。`--workspace` 未指定時は `cwd`、指定時はその path を観測対象に使う
- `--all-sessions` 単体は workspace 制限を外すだけで、無制限観測ではない
- B0 の優先順位判断は、原則として `--all-sessions + 明示的な長窓` の観測を基準にする
- `--days 7` や adaptive 30 日は通常運用の挙動確認には使ってよいが、B0 の主判断には使わない
- 例外的に full-history 相当が取れない場合は、その制約を report に明記する

## Required Metrics

最低限見る指標は 3 つ。

- `generic_rate`
  - generic な `primary_intent` の割合
- `synonym_split_rate`
  - 同義の intent が別表現に割れていそうな割合
- `specificity_distribution`
  - `high / medium / low` の分布

補助観測:

- oversized cluster の有無
- `generic_tools`, `generic_task_shape`, `weak_semantic_cohesion` の出方
- proposal-ready candidate が giant cluster に吸われていないか

## Decision Rules

### B を優先する条件

次のどれかに当てはまるなら、まず `B: feature extraction` を優先する。

- `generic_rate > 0.60`
- low specificity が多く、`primary_intent` 自体が曖昧
- 同義語割れが目立ち、同じ目的が別ラベルに散っている
- `task_shape` や clustering を変えても、元の intent が汎用文のまま

典型症状:

- `確認する`, `調べる`, `進める` のような intent が多い
- 実際には別目的なのに `search_code` や `inspect_files` 以前に intent が潰れている

### C を優先する条件

次のどれかに当てはまるなら、まず `C: clustering / similarity` を優先する。

- `primary_intent` は具体的だが giant cluster が残る
- `generic_rate` は低いのに oversized cluster が多い
- 異なる artifact / repeated rule / task objective が 1 candidate にまとまる
- `write_markdown`, `review_changes` のような generic shape に異質タスクが吸われる

典型症状:

- specific intent は取れているのに `proposal_ready` が増えない
- `tool:rg` や generic task shape 起点で大きな cluster ができる
- representative examples が cluster 全体を説明しきれない

### D を優先する条件

次のどれかに当てはまるなら、まず `D: quality gate rebalance` を優先する。

- cluster 自体は妥当に見えるのに `proposal_ready` へ上がらない
- `needs_research` から `ready` に戻る候補がほとんどない
- oversized ではない候補まで保守的に reject される
- split / promote / reject の境界が厳しすぎる

典型症状:

- detail を読むと十分一貫しているのに `reject_candidate` になる
- `0件` が多い主因が cluster の粗さではなく gate の厳しさに見える

## Default Priority Rule

迷った場合の優先順位は次の通り。

1. `C: clustering / similarity`
2. `D: quality gate rebalance`
3. `B: feature extraction / intent normalization`

ただし、これは default に過ぎない。
`generic_rate` が高いなど B 優先の条件が明確なら、そちらを優先する。

## Report Contract

B0 の実行後は、別の report に次を残す。

- 実行日
- 実行コマンド
- 観測対象の scope
  - full-history 相当か、制約付きか
  - 通常運用の 7 日 / adaptive 30 日とは別条件で観測したか
- 3 指標の実測値
- oversized cluster などの補助所見
- 今回の priority decision
- 次に着手する順序

重要:

- report は今回の観測結果
- この reference は観測の判断枠

を分離して扱う。

## Re-run Triggers

次のどれかに当てはまる場合は B0 を再観測する。

- `primary_intent` 抽出ロジックを変えた
- `TASK_SHAPE_PATTERNS` や synonyms を変えた
- `stable_block_keys` や `similarity_score` を変えた
- 実ユーザーや対象履歴の性質が大きく変わった
- `skill-miner` の提案品質が再び崩れた

## Non Goals

B0 では以下を固定しない。

- 特定ユーザーの実測値
- 一度出た priority decision の永続化
- future-proof な絶対閾値の保証

その都度の履歴を見て、同じ判断枠で決め直す。
