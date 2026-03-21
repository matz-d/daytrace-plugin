# Classification Rules v2

`skill-miner` の一次分類は `CLAUDE.md / skill / hook / agent` の 4 つだけ使う。
`plugin` は v2 では使わない。

## `CLAUDE.md`

向いているもの:

- repo ローカルの原則として毎回読ませたい
- 手順よりも「必ず守るルール」の定着が目的
- 同じ前提説明や禁止事項を繰り返している

境界ケース:

- 同じ作法を repo ルールにしたいなら `CLAUDE.md`
- ただし複数段の作業手順まで含むなら `skill` を優先する

例:

- findings-first で報告する
- テスト結果を閉じに必ず書く
- 特定 repo では絶対に `git reset --hard` しない

## `skill`

向いているもの:

- 明確な入力、出力、手順がある
- 複数ステップを順番に踏むことで価値が出る
- 人が呼び出して使うフローとして再利用したい

境界ケース:

- 同じ目的で毎回 3 手以上の処理を踏むなら `skill`
- `CLAUDE.md` の原則だけでは再現できない具体手順があるなら `skill`

例:

- PR review の findings-first レポート生成
- 週次のレポート下書き作成
- 手順つきの migration 作業補助

## `hook`

向いているもの:

- 判断不要の機械処理
- あるタイミングで自動実行すると価値がある
- 成否や副作用が比較的読みやすい

境界ケース:

- 人が都度判断する必要があるなら `skill` か `agent`
- 単なる repo ルールなら `CLAUDE.md`

例:

- save 時の整形
- commit 前の lint / validation
- log / telemetry の自動採取

## `agent`

向いているもの:

- 単一手順ではなく継続的な役割が中心
- 複数タスクを横断する行動原則や優先順位が重要
- 出力物より振る舞いの一貫性が価値になる

境界ケース:

- 「何をするか」より「どう振る舞うか」が主題なら `agent`
- 実行トリガーごとに定型フローへ落とせるなら `skill` または `hook`

例:

- 常にレビュー観点を保つ reviewer agent
- 継続的な triage / routing を担う agent
- 監視対象を横断して動く observation agent

## Triage Reminder

- `提案（アクション候補）`: そのまま分類してよい
- `有望候補（もう少し観測が必要）`: 巨大クラスタや混在疑いがあり、detail で split 判定したい
- `観測ノート`: 単発または一般化の根拠不足

正式提案数は `0-5 件` を正常系として扱う。

## Guardrail 補助信号（Phase 3 / script 側）

Python `skill_miner_proposal.py` は LLM overlay の後に **決定論的 guardrail** をかける。分類境界の補助として次を使う（詳細は `skill_miner_common.py`）。

- **`agent`**: 従来どおり `total_packets >= 4`、かつ `summarize_findings` / `search_code` / `inspect_files` 等の agent 向き task_shape、または **非 CLAUDE 系の rule_hint** があれば通過。追加で **`label` + `intent_trace` の 2 行以上**が役割語（reviewer / triage / レビュー担当 等）に一致すれば **role consistency** として通過しうる。
- **`CLAUDE.md`**: `claude-md` artifact または CLAUDE 系 `rule_hints` が無くても、**制約・受け入れ条件**から推定した **宣言的ウェイト**が十分に高く、かつ **宣言的 /（宣言的+ワークフロー）比率**が閾値以上なら `CLAUDE.md` を許可しうる（「ルール中心だが artifact にまだ載っていない」ケース向け）。
- **`hook`**: `run_tests` が先頭 task_shape かつ `tests-before-close` ルールがあり、観測回数が十分な場合、**lint/test ツール署名が無くても** 狭いゲートで hook を許可しうる（`tests-before-close` が hook 用 rule 集合にも含まれるため、評価順が重要）。

overlay の **`confidence` は guardrail 分岐に使わない**（proposal JSON の `classification_guardrail_signals` と併せて観測する）。

## Orchestration: いつ overlay を書くか

LLM は **候補ごとに必ず**呼ぶのではなく、`classify-target-selection.md` の規準で「曖昧候補」に絞ったうえで overlay を生成する。`rejected` や明らかな hook / 強い CLAUDE.md に一致する候補では overlay を省略し、script 側のヒューリスティック + guardrail に任せる。
