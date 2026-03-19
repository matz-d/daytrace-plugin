# DayTrace

ローカル証跡から **観測 → 抽出 → 適用** を自律的に回す Claude Code plugin。

5 つのスキルが連携するパイプラインで、
**Date-first / Scope-first の直交射影** によりデータの潰れを防ぎつつ、
**Carry-Forward State Machine** がユーザーの reject すら次回の学習に変える。
外部通信ゼロ・Python stdlib のみで完結する自己改善エージェントアーキテクチャ。

## インストール

```bash
claude plugin add github:matz-d/daytrace-plugin
```

設定不要。ソースが足りない環境でも利用可能なログだけで縮退動作します。

## パイプライン

```
observe ──→ project ──→ extract ──→ propose ──→ apply
  │            │            │           │          │
  │        date-first   scope-first  carry-     CLAUDE.md
  │        ┌─────┐     ┌─────┐     forward    skill / hook
  │        │daily │     │skill│     state      agent
  │        │report│     │miner│     machine
  │        │post  │     └─────┘
  │        │draft │
  │        └─────┘
  │
5 local sources
(git, claude, codex, chrome, file-activity)
```

### スキル

| スキル | 射影 | 役割 |
|--------|------|------|
| `/daytrace-session` | — | 一言で全フェーズを自律完走する統合オーケストレーター |
| `/daily-report` | date-first | その日の活動を自分用/共有用の日報ドラフトに再構成 |
| `/post-draft` | date-first | 一次情報から読者向け narrative draft を生成 |
| `/skill-miner` | scope-first | AI 履歴から反復パターンを抽出し固定化を提案 |
| `/skill-applier` | — | 提案を CLAUDE.md / skill / hook / agent に固定化 |

**date-first** は「いつ」が主軸。対象日の全ソースを横断し、workspace は補助フィルタ。
**scope-first** は「どこまで見るか」が主軸。workspace 7 日窓 → all-sessions へ段階拡張。

## データソース

ローカルデータのみ。**外部へのデータ送信は一切行いません。**

| ソース | 対象 | スコープ |
|--------|------|----------|
| git-history | Git コミット + worktree snapshot | workspace |
| claude-history | `~/.claude/projects/**/*.jsonl` | all-day |
| codex-history | `~/.codex/history.jsonl` | all-day |
| chrome-history | Chrome History DB（読み取り専用コピー） | all-day |
| workspace-file-activity | untracked ファイル変更 | workspace |

## 動作要件

- Python 3.x（標準ライブラリのみ。外部パッケージ不要）
- Git / macOS or Linux

## License

MIT
