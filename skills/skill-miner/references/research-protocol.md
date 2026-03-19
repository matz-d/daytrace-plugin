# Deep Research Protocol

`needs_research` 候補に対してだけ、限定的な追加調査を行ってよい。

## ルール

- 1 candidate あたり最大 5 refs まで
- 追加調査は 1 回まで
- `research_targets` があればそれを優先して使う
- `research_brief.questions` と `research_brief.decision_rules` をそのまま調査メモの骨子に使う
- ランダム抽出ではなく、代表例に近い ref / near-match に近い ref / 異質そうな ref を混ぜる
- detail を大量取得しない
- 追加調査しても粒度が粗い場合は `観測ノート` に落とす

## 追加調査後

- `skill_miner_research_judge.py` を 1 回だけ実行して structured conclusion を得る
- `promote_ready`
  - `提案（固定化を推奨）` へ移す
  - `oversized_cluster` / `weak_semantic_cohesion` / `split_recommended` / `near_match_dense` を解消した場合だけ許可する
  - proposal 側には「研究で解消した注意信号」を残す
- `split_candidate`
  - `有望候補（もう少し観測が必要）` に残し、必要なら「分割軸」を書く
- `reject_candidate`
  - `観測ノート` に移す

## 追加調査で確認すべきこと

- 本当に 1 つの automation candidate か
- コードレビュー、調査、ログ整理のような別作業が混ざっていないか
- 分割するならどの軸が自然か
- 今回の proposal phase で正式提案すべきか、保留すべきか
