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
3. 承認後、**Workspace チェック**（後述）を行い、パスに応じて生成する
4. 生成結果を報告し、動作確認を案内

## Agent 設計案 → 生成

`next_step_stub` から抽出する:

- **役割:** 1 文での役割定義
- **行動原則:** rule_hints ベースの振る舞いルール
- **想定トリガー:** いつこの agent を使うか

提示フロー:

1. 設計案をユーザーに見せる
2. 「このエージェントを作成しますか？」と確認
3. 承認後、**Workspace チェック**（後述）を行い、パスに応じて生成する
4. 生成結果を報告し、`/agents` での確認を案内

## Workspace チェックと生成パス

承認後、候補の観測元 workspace（`proposal` の `config.workspace`）と現在の CWD を比較する。

### 同一 repo（workspace が CWD と一致、または workspace 未設定）

Claude が Write / Bash ツールで**直接生成**する:

**hook の場合（`references/hook-creation-guide.md` の生成手順に従う）:**

1. `Bash` で `.claude/hooks/` ディレクトリを作成（`mkdir -p`）
2. `Write` で `.claude/hooks/{slug}.sh` を生成
3. `Bash` で実行権限を付与（`chmod +x`）
4. `Read` で `.claude/settings.json` を読み込み（なければ空オブジェクト扱い）
5. hooks エントリを構築して `Write` でマージ結果を書き戻す

**agent の場合（`references/agent-creation-guide.md` の生成手順に従う）:**

1. `Bash` で `.claude/agents/` ディレクトリを作成（`mkdir -p`）
2. `Write` で `.claude/agents/{slug}.md` を生成

### cross-repo（workspace が CWD と異なる）

直接生成はしない。代わりに **handoff JSON** を Write で生成し、ユーザーに案内する。

handoff JSON の内容:

```json
{
  "record_type": "hook_agent_handoff",
  "kind": "hook" | "agent",
  "label": "<候補のラベル>",
  "target_workspace": "<観測元 workspace>",
  "next_step_stub": { ... },
  "instructions": "対象リポジトリを開き、next_step_stub の内容に従って .claude/hooks/ または .claude/agents/ にファイルを生成してください。"
}
```

保存先: `~/.daytrace/hook-agent-handoffs/handoff-{slug}.json`

案内文（ユーザーへ）:

```text
この候補は別リポジトリ向けです。
target repo: <target_workspace>
handoff file: ~/.daytrace/hook-agent-handoffs/handoff-{slug}.json

対象リポジトリを開いてから、handoff の内容をもとに hook / agent を設定してください。
```

## ユーザーが承認しなかった場合

- 「あとで」「今回は見送る」→ `defer` を記録
- 「不要」「いらない」→ `reject` を記録
- session を閉じる → `pending` 経由で `defer` 扱い

## daytrace-session での扱い

- 設計案を提示し、承認を求める
- 承認されたら生成を実行し、`--decision adopt --completion-state completed` を記録
- 承認されなかった場合は `defer` / `reject` を記録
