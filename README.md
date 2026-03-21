# DayTrace

> **AIエージェント ハッカソン 2026 提出作品**  
> テーマ: **「一度命じたら、あとは任せろ」**

**一度頼めば、観測から提案まで自律完走。**  
DayTrace は、ローカル証跡を集めて 1 日を再構成し、反復パターンを抽出し、次の改善候補まで返す Claude Code plugin です。

![DayTrace overview](docs/daytrace-hero.svg?v=2)

## 何ができるか

`/daytrace-session` と一度頼むと、DayTrace は次を順に実行します。

1. Git / Claude / Codex / Chrome / file activity から、その日の証跡を収集
2. 日報を生成
3. AI 履歴から反復パターンを抽出し、適用候補を提案
4. 必要条件を満たす日は、投稿下書きまで生成

再構成された 4 種類の成果物が返ります。

- **自分用日報**: 後で振り返れる形に再構成
- **共有用日報**: 第三者に見せやすい進捗報告
- **パターン提案**: `CLAUDE.md` / `skill` / `hook` / `agent` への適用候補
- **投稿下書き**: その日の中心テーマを 1 本の narrative として整理

## ハッカソン審査基準へのアプローチ

### 自律性

DayTrace の自律性は、単に「質問しない」ことではなく、**最後まで進めること** にあります。

- 一度頼むと、収集から日報・提案・下書きまで自律完走
- source が欠けても止まらず、利用可能な証跡だけで継続
- 人の判断を仰ぐのは、共有範囲の確認や適用の承認など、影響の大きい決定だけ

### クオリティ

同じローカル証跡を、用途に応じて 2 つのルートで使い分けます。

- **date-first**: 日報 / 投稿下書き向け
- **scope-first**: スキル抽出向け

各提案には根拠（evidence）と確信度（confidence）を付け、LLM 出力の信頼性を担保しつつ人が読める形に整えます。

### インパクト

毎日の振り返りが、開発環境の改善サイクルに直結します。

- 提案は `CLAUDE.md` / `skill` / `hook` / `agent` への適用候補として返る
- ユーザーが見送った提案も decision log に残り、証跡が蓄積すれば次回あらためて再浮上する
- 使い続けるほど、反復作業が自動化され、開発環境が自分に合った形に育っていく

## 試し方

### 1. インストール

```bash
claude plugin add github:matz-d/daytrace-plugin
```

設定は不要です。外部へのデータ送信は一切ありません（ローカル完結）。

### 2. 実行

```bash
/daytrace-session
```

あるいは自然言語で、

- `今日の振り返りをお願い`
- `1日のまとめをして`
- `今日の活動を整理して`

のように頼むだけでも開始できます。

### 3. 実行すると返るもの

1. DayTrace ダイジェスト
2. 日報
3. パターン提案
4. 投稿下書き
5. セッション要約

## どう動くか

```mermaid
flowchart LR
  subgraph Sources["5 Local Sources"]
    direction TB
    S1[Git]
    S2[Claude]
    S3[Codex]
    S4[Chrome]
    S5[File Activity]
  end

  P1["Phase 1\nCollect"]
  DG["Phase 1.5\nDigest"]
  P2["Phase 2 · date-first\nReport\n<i>daily-report</i>"]
  P3["Phase 3 · scope-first\nMining → Fixation\n<i>skill-miner · skill-applier</i>"]
  P4["Phase 4 · date-first\nDraft\n<i>post-draft</i>"]
  P5["Phase 5\nSession Summary"]

  Sources --> P1 --> DG --> P2 --> P3 --> P4 --> P5

  P2 -. "日報" .-> OUT(("Outputs"))
  P3 -. "提案 →\nCLAUDE.md / skill\nhook / agent" .-> OUT
  P4 -. "投稿下書き" .-> OUT
```

- **date-first**: 1 日を軸に活動を再構成する（日報・投稿下書き）
- **scope-first**: 観測窓（7〜30 日）で反復パターンを抽出する（パターン提案）
- ソースが欠けても止まらず、取得できたデータだけで最後まで進む（Graceful Degrade）

### 5つのスキル

| スキル | 主軸 | 役割 |
|--------|------|------|
| `/daytrace-session` | orchestration | 一言で全フェーズを自律完走する統合入口 |
| `/daily-report` | date-first | その日の活動を日報ドラフトに再構成 |
| `/post-draft` | date-first | 1 日の中心テーマを narrative draft に再構成 |
| `/skill-miner` | scope-first | AI 履歴から反復パターンを抽出し適用候補を提案 |
| `/skill-applier` | apply | 提案を `CLAUDE.md` / `skill` / `hook` / `agent` に適用 |

## データソース

収集対象は **ローカルデータのみ** です。

| ソース | 対象 | スコープ |
|--------|------|----------|
| `git-history` | Git コミット + worktree snapshot | workspace |
| `claude-history` | `~/.claude/projects/**/*.jsonl` | all-day |
| `codex-history` | `~/.codex/history.jsonl` | all-day |
| `chrome-history` | Chrome History DB の読み取り専用コピー | all-day |
| `workspace-file-activity` | untracked ファイル変更 | workspace |

## 動作要件

- Python 3.8+
- Git
- macOS または Linux

追加パッケージ不要（Python 標準ライブラリのみ）。

## License

MIT
