# Agent Creation Guide

skill-applier が agent 候補を実際の Claude Code agent として生成する際のフォーマット仕様。

## 生成先

プロジェクトの `.claude/agents/` ディレクトリに `.md` ファイルを作成する。

ディレクトリが存在しない場合は作成する。ファイル名は `{候補名のスラッグ}.md`。

## Agent ファイル構造

Agent は YAML フロントマター付き Markdown ファイル。

```markdown
---
name: agent-slug-name
description: いつこの agent を使うべきかの説明（トリガーフレーズを含める）
tools: Read, Grep, Glob, Bash
model: sonnet
---

システムプロンプト（agent の振る舞いを定義）
```

## YAML フロントマターフィールド

| フィールド | 必須 | 説明 |
|-----------|------|------|
| `name` | Yes | 一意識別子。小文字+ハイフンのみ |
| `description` | Yes | Claude がこの agent に委譲すべきタイミングの説明 |
| `tools` | No | 許可ツールのカンマ区切りリスト |
| `model` | No | `sonnet`, `opus`, `haiku`, or full model ID |
| `maxTurns` | No | 最大ターン数 |
| `permissionMode` | No | `default`, `acceptEdits`, `dontAsk`, `plan` |

### tools の指定

```yaml
tools: Read, Grep, Glob           # 読み取り専用
tools: Read, Grep, Glob, Bash     # 実行も可能
tools: Read, Edit, Write, Bash    # 書き込みも可能
```

### model の選択

- `sonnet`: 高速・コスト効率（多くの場合はこれで十分）
- `opus`: 高精度・複雑なタスク向け
- `haiku`: 最速・軽量タスク向け

## next_step_stub からの生成ルール

| next_step_stub フィールド | agent ファイルマッピング |
|------------------------|---------------------|
| `role_summary` | description の元ネタ + システムプロンプトの冒頭 |
| `behavior_rules` | システムプロンプト内のルールセクション |
| `trigger` | description のトリガー説明 |

## 生成手順

1. `next_step_stub` から role_summary, behavior_rules, trigger を取得
2. ユーザーに設計案を提示し、承認を求める
3. 承認後:
   a. `.claude/agents/` ディレクトリが無ければ作成
   b. `.claude/agents/{候補名のスラッグ}.md` を生成
4. 動作確認の案内を出す（`/agents` で一覧確認）

## 生成テンプレート

```markdown
---
name: {slug}
description: >
  {role_summary}。{trigger}に使う。
tools: Read, Grep, Glob, Bash
model: sonnet
---

あなたは {role_summary} を担当する専門エージェントです。

## 役割

{role_summary}

## 行動原則

{behavior_rules を箇条書きで展開}

## プロセス

1. タスクの内容を把握する
2. 関連するコードやファイルを調査する
3. 行動原則に従って作業を実行する
4. 結果を構造化して報告する
```

## 注意事項

- 同名の agent が既に存在する場合は上書き確認を行う
- description にはトリガーフレーズを含める（Claude が自動委譲を判断するため）
- tools は必要最小限にする（Write/Edit は本当に必要な場合のみ）
- ユーザーの明示承認なしに agent ファイルを生成しない
- 生成後、`/agents` コマンドで確認可能な旨を案内する
