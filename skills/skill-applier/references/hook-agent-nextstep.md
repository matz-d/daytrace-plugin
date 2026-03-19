# Hook / Agent Next Step Contract

`hook` または `agent` の候補が選択された場合、設計案を提示して次セッションへ送る。
DayTrace は設計案の提示のみを担い、settings.json 書き込みや agent 定義ファイル生成は行わない。

## Hook 設計案

`tool_signature` + `rule_hints` から抽出する:

- **トリガーイベント:** PreToolUse | PostToolUse | Stop | ...
- **対象ツール:** tool_name リスト
- **アクション:** 実行内容の 1 文説明
- **ガード条件:** 実行しない条件の 1 文説明
- ガイド: `「{candidate_label} を hook にしてください」と次セッションで指示`

## Agent 設計案

`representative_examples` + `rule_hints` から抽出する:

- **役割:** 1 文での役割定義
- **行動原則:** rule_hints ベースの振る舞いルール
- **想定トリガー:** いつこの agent を使うか
- **参考パターン:** representative_examples から 1-2 件
- ガイド: `「{candidate_label} を agent にしてください」と次セッションで指示`

## daytrace-session での扱い

- 設計案を提示し、次セッションへ送る旨を伝える
- 成功未確認のまま session を閉じる場合は `adopt` を確定させず、`pending` 経由で `defer` 扱いにする
