# Classification Rules v2

`skill-miner` の一次分類は `CLAUDE.md / skill / hook / agent` の 4 つだけ使う。
`plugin` は v2 では使わない。

## `CLAUDE.md`

向いているもの:

- repo ローカルの原則として毎回読ませたい
- 手順よりも「必ず守るルール」の固定化が目的
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

- `提案（固定化を推奨）`: そのまま分類してよい
- `有望候補（もう少し観測が必要）`: 巨大クラスタや混在疑いがあり、detail で split 判定したい
- `観測ノート`: 単発または一般化の根拠不足

正式提案数は `0-5 件` を正常系として扱う。
