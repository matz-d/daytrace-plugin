# Formatter Contract

DayTrace の artifact 生成において、**機械的変換（Python）** と **意味変換（LLM）** の責務境界を定義します。

## 基本原則

> Python は事実の守番人。LLM は表現の翻訳者。

Python が行う変換は **決定論的・可逆的・監査可能** なものに限定します。  
出力の意味や文体に関わる判断は LLM（SKILL.md ルール）に委ねます。

## 入出力 Shape

```python
FormatterInput(
    raw_text: str,
    mode: str,           # "report-private" | "report-share" | "post-draft" | "proposal"
    scope_mode: str,     # "single" | "mixed"
    sources: list[str],  # e.g. ["git-history", "claude-history"]
    session_date: str | None,
)

FormatterResult(
    text: str,
    warnings: list[str],  # 検知された問題（warn-only）
    patches: list[Patch], # 置換の監査ログ
)
```

## Python 側：機械的変換

| 変換 | 適用対象 | 動作 |
|------|----------|------|
| `path_sanitize` | 全 artifact | `/Users/…/` の絶対パスを `[PATH]` に**置換** |
| `normalize_source_names` | 全 artifact | `git-history` → `Git の変更履歴` など mapping に従い**置換** |
| `check_forbidden_words` | 全 artifact | 禁止語を検知して `warnings` に記録（**置換しない**）|
| `check_english_leakage` | 全 artifact | 内部英語フレーズを検知して `warnings` に記録（**置換しない**）|
| `inject_mixed_scope_note` | scope_mode="mixed" | 先頭に混在スコープ注記を**挿入** |
| `inject_footer` | mode 別 | 末尾に再構成元フッターを**挿入**（下記参照）|

### Footer 粒度（mode 別）

| mode | footer 内容 |
|------|------------|
| `report-share` | `_この日報は DayTrace により自動生成されました。_`（sources は非表示）|
| `report-private` | 観測日 + `再構成元: Git の変更履歴 / …`（sources 全件）|
| `post-draft` | 観測日 + 再構成元（sources 全件）|
| `proposal` | sources が空の場合は footer 省略 |

### Source 名正規化テーブル

| raw 識別子 | 表示名 |
|------------|--------|
| `git-history` | Git の変更履歴 |
| `claude-history` | Claude の会話ログ |
| `codex-history` | Codex の会話ログ |
| `chrome-history` | ブラウザの閲覧ログ |
| `workspace-file-activity` | workspace のファイル作業痕跡 |

### 禁止語リスト

**内部状態語（出力に出してはいけない）:**
- `candidate_id`, `triage_status`, `internal state`, `internal trace`
- `classification_trace`, `suggested_kind`
- `Continuing autonomously`

**プロダクトコピー禁止語（置換が必要）:**
- `寄り道` → 「別の作業をした時間」など
- `今日の重心` → 「今日の中心作業」など
- `実装密度の高い1日` → 成果・変更を直接書く
- `ハッカソン提出を控え` → 文脈を直接書く

禁止語は Python が **warn-only** で検知し、置換は LLM が行います。

## LLM 側：意味変換

以下は Python formatter の責務外です。SKILL.md の出力ルールに従って LLM が処理します。

| 変換 | 説明 |
|------|------|
| 行動レベルの語彙変換 | ファイル名・実装名を「何をした」に変換 |
| トーン調整 | share（対外）vs private（自分用）のです・ます調整 |
| 事実 / 推測の書き分け | source 裏付けの有無に応じた表現調整 |
| 禁止語の自然な言い換え | Python の warn を受けて適切な表現に置換 |
| 背景説明の圧縮 | post-draft など文体要件に応じた長さ調整 |

## 適用位置

formatter は **artifact 保存直前**に適用します。  
chat への要約（Layer 2）には原則として適用しません（chat 要約は LLM が SKILL.md ルールに従って直接生成）。

```
raw_text  →  ArtifactFormatter.apply()  →  FormatterResult.text  →  保存
                                        └→  FormatterResult.warnings  →  SKILL.md completion check 参照用
```

## エラー / デグレード時の扱い

| 状況 | 動作 |
|------|------|
| formatter 例外 | `raw_text` をそのまま保存し、warnings に `formatter_error: …` を積む |
| path_sanitize で置換漏れ | warnings: `path_not_sanitized: …` |
| 禁止語検知 | warnings に記録。LLM が次回生成時に修正 |

## 将来の拡張ポイント

- `未完全文 / 切れ文の検知`（文末が句読点で終わっていない場合の warn）
- `低信頼度 source の明示`（chrome-history のみで断定している文の検知）
- report-private / report-share 間の footer 粒度の細分化
