# DayTrace

AI との作業ログを、ローカルに毎日ためる Claude Code plugin です。

v0.2.0 で方向を変えました。日報・投稿下書き・パターン提案の生成をやめ、まず「きちんとログを取る」ことに絞っています。振り返りの出力は、ためたログで何が読み取れるかを確かめてから決め直します。

## ためるもの

| 知りたいこと | 取り方 |
|---|---|
| 何を頼んだか | Claude の依頼文（`user_requests[]`）、Codex の依頼文（`turns[].request`）。ツールが差し込む文と引き継ぎ要約は除く |
| どれくらい時間を使ったか | Codex は依頼ごとの開始・終了（`turns[].started_at` / `completed_at`）。Claude は依頼文の時刻 |
| 何を決めたか・見送ったか | 会話ログをそのまま残す（読み取り方は今後決める） |
| どこで手戻りしたか | 会話ログをそのまま残す（読み取り方は今後決める） |

Codex の `turns[].trigger` で、手入力（`composer`）、スマホ（`remote_ios`）、自動実行（`automation_cron_scheduled`）、Codex の質問への回答（`send_user_message_async_question`）を区別できます。

## インストール

```bash
claude plugin marketplace add matz-d/daytrace-plugin
claude plugin install daytrace
```

## 使い方

Claude Code で `/daytrace-log`、または「今日のログを集めて」「昨日のログを見せて」と頼みます。

直接実行する場合:

```bash
# 集める（Git とファイル変更は --workspace のリポジトリだけ。会話ログは 1 日分すべて）
python3 scripts/collect_daily.py --workspace /path/to/repo

# 読み出す
python3 scripts/read_log.py --workspace /path/to/repo --date today --format text
```

保存先は `~/.daytrace/daytrace.sqlite3` です。未コミットファイルの変更はその時点の状態しか取れないため、毎日集める必要があります。launchd や cron で `collect_daily.py` を 1 日 1 回動かしてください。寝ていて実行を逃した日は、次の実行で昨日の分も集めます。

## データソース

ローカルにある記録だけを読みます。外部への通信はしません。

| ソース | 対象 |
|--------|------|
| `git-history` | Git コミット + 作業ツリーの状態 |
| `claude-history` | `~/.claude/projects/**/*.jsonl` |
| `codex-history` | `~/.codex/sessions/**/rollout-*.jsonl` |
| `chrome-history` | Chrome History DB（読み取り専用コピー。権限がなければスキップ） |
| `workspace-file-activity` | 未コミットファイルの変更 |

## 動作要件

- Python 3.9+（標準ライブラリのみ。追加パッケージ不要）
- Git
- macOS または Linux

## License

MIT
