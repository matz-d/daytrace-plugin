---
name: daytrace-log
description: >
  Claude Code / Codex の会話ログ、Git、ファイル変更を DayTrace の保存先に集め、時系列で読み出す。
  「今日のログを集めて」「ログを取って」「昨日のログを見せて」「何を頼んだか一覧にして」と言われた時に使う。
  日報・要約・提案は作らない。
user-invocable: true
---

# DayTrace Log

ログを集めて、そのまま読み出すだけの skill。日報・投稿下書き・パターン提案は作らない。

## 集める

対象リポジトリ（Git とファイル変更を取る場所）を `--workspace` で指定する。指定がなければ現在のディレクトリ。

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/collect_daily.py --workspace /absolute/path/to/repo
```

- 今日の分を集める。昨日の分が保存されていなければ、昨日も先に集める
- 保存先は `~/.daytrace/daytrace.sqlite3`
- Claude / Codex の会話ログは、どのリポジトリで動かしたものも 1 日分すべて集める

`${CLAUDE_PLUGIN_ROOT}` はこの `SKILL.md` から `../..` を辿った先。

## 読み出す

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/read_log.py --workspace /absolute/path/to/repo --date today --format text
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/read_log.py --workspace /absolute/path/to/repo --since 2026-11-16 --until 2026-11-22
```

JSON（既定）では、イベントごとに次が入る。

- Claude: `type == "session_summary"` の `details.user_requests[]` — ユーザーが打った依頼文と時刻（サブエージェント宛ての文、引き継ぎ要約、システムの差し込み文は除く）
- Codex: `type == "session_meta"` の `details.turns[]` — 依頼文、きっかけ（`composer` は手入力、`remote_ios` はスマホ、`automation_cron_scheduled` は自動実行、`send_user_message_async_question` は Codex の質問への回答）、開始・終了時刻
- Git: コミットと作業ツリーの状態
- ファイル変更: 未コミットファイルの更新時刻

## 返し方

- 集めた結果は、ソースごとの件数と、スキップしたソースとその理由だけを返す
- 読み出しを頼まれたら、時刻順の一覧をそのまま返す。解釈や要約は頼まれた時だけ行う
- 会話ログには顧客名や個人の発言が含まれる。読み出した内容を外部へ送らない
