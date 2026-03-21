# Hook Creation Guide

skill-applier が hook 候補を実際の Claude Code hook として生成する際のフォーマット仕様。

## 生成先

プロジェクトの `.claude/settings.json` に hook 定義を追加する。

ファイルが存在しない場合は新規作成する。既存の場合は `hooks` キーにマージする。

## settings.json の Hook 構造

```json
{
  "hooks": {
    "EventType": [
      {
        "matcher": "pattern",
        "hooks": [
          {
            "type": "command",
            "command": ".claude/hooks/script-name.sh",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

## サポートされるイベント

| イベント | タイミング | ブロック可能 | 主な用途 |
|---------|----------|------------|---------|
| `PreToolUse` | ツール実行前 | Yes | コマンド検証、危険操作ブロック |
| `PostToolUse` | ツール実行後 | No | lint、フォーマット、通知 |
| `Stop` | Claude 応答完了時 | Yes | テスト実行、品質チェック |
| `UserPromptSubmit` | プロンプト送信前 | Yes | 入力検証 |
| `SubagentStop` | サブエージェント完了時 | No | 結果検証 |

## Hook タイプ

### command hook（推奨）

シェルスクリプトを実行する。stdin に JSON イベントデータが渡される。

```json
{
  "type": "command",
  "command": ".claude/hooks/hook-name.sh",
  "timeout": 30
}
```

スクリプトの終了コード:
- `0`: 成功（stdout の JSON を解釈）
- `2`: ブロックエラー（stderr を Claude に表示）
- その他: 非ブロックエラー

### prompt hook

LLM に単発評価させる。スクリプト不要で手軽だが、毎回 LLM コールが発生する。

```json
{
  "type": "prompt",
  "prompt": "Review this command for safety: $ARGUMENTS",
  "timeout": 30
}
```

置換変数: `$ARGUMENTS`, `$TOOL_NAME`, `$COMMAND`, `$FILE_PATH`

## Matcher

正規表現パターンでイベントをフィルタする:

```json
"matcher": "Bash"
"matcher": "Bash|Write|Edit"
"matcher": ".*"
```

## next_step_stub からの生成ルール

`next_step_stub` の情報を以下のように settings.json に変換する:

| next_step_stub フィールド | settings.json マッピング |
|------------------------|----------------------|
| `trigger_event` | イベントキー（`Stop`, `PostToolUse` 等） |
| `target_tools` | `matcher`（`\|` 区切りで結合） |
| `action_summary` | スクリプト内コメント or prompt テキスト |
| `guard_condition` | スクリプト内の条件分岐 |

## 生成手順

1. `next_step_stub` から trigger_event, target_tools, action_summary, guard_condition を取得
2. ユーザーに設計案を提示し、承認を求める
3. 承認後:
   a. `.claude/hooks/` ディレクトリが無ければ作成
   b. hook スクリプト `.claude/hooks/{候補名のスラッグ}.sh` を生成（実行権限付与）
   c. `.claude/settings.json` に hook 定義を追加（既存設定とマージ）
4. 動作確認の案内を出す

## スクリプトテンプレート（Stop イベント・テスト実行）

```bash
#!/bin/bash
# Hook: {label}
# {action_summary}

# Guard: {guard_condition}

# Read event JSON from stdin
INPUT=$(cat)

# Run tests
python3 -m pytest tests/ -q 2>&1
TEST_EXIT=$?

if [ $TEST_EXIT -ne 0 ]; then
  echo "Tests failed. Please fix before finishing." >&2
  exit 2  # Block
fi

exit 0  # Allow
```

## スクリプトテンプレート（PreToolUse イベント・検証）

```bash
#!/bin/bash
# Hook: {label}
# {action_summary}

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null)
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null)

# Guard: {guard_condition}
# Add validation logic here

exit 0  # Allow by default
```

## 注意事項

- 既存の settings.json を壊さない（hooks キーのマージのみ）
- スクリプトには必ず実行権限を付与する（`chmod +x`）
- timeout はデフォルト 30 秒、テスト実行系は 120 秒を推奨
- ユーザーの明示承認なしに settings.json を変更しない
