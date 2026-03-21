# Hook / Agent Guided Creation Contract

`hook` または `agent` の候補が選択された場合、設計案を提示し、ユーザー承認後に実際のファイルを生成する。

## Hook 設計案 → 生成

`next_step_stub` から抽出する:

- **トリガーイベント:** PreToolUse | PostToolUse | Stop | ...
- **対象ツール:** tool_name リスト
- **アクション:** 実行内容の 1 文説明
- **ガード条件:** 実行しない条件の 1 文説明

提示フロー:

1. 設計案をユーザーに見せる
2. 「この hook を設定しますか？」と確認
3. 承認後、`references/hook-creation-guide.md` のフォーマットに従い:
   - `.claude/hooks/{slug}.sh` スクリプトを生成
   - `.claude/settings.json` に hook 定義を追加
4. 生成結果を報告し、動作確認を案内

## Agent 設計案 → 生成

`next_step_stub` から抽出する:

- **役割:** 1 文での役割定義
- **行動原則:** rule_hints ベースの振る舞いルール
- **想定トリガー:** いつこの agent を使うか

提示フロー:

1. 設計案をユーザーに見せる
2. 「このエージェントを作成しますか？」と確認
3. 承認後、`references/agent-creation-guide.md` のフォーマットに従い:
   - `.claude/agents/{slug}.md` を生成
4. 生成結果を報告し、`/agents` での確認を案内

## ユーザーが承認しなかった場合

- 「あとで」「今回は見送る」→ `defer` を記録
- 「不要」「いらない」→ `reject` を記録
- session を閉じる → `pending` 経由で `defer` 扱い

## daytrace-session での扱い

- 設計案を提示し、承認を求める
- 承認されたら生成を実行し、`--decision adopt --completion-state completed` を記録
- 承認されなかった場合は `defer` / `reject` を記録
