# Violation Patterns（§A）

output-polish.md Appendix A で定義された違反パターンの詳細。
Phase 1 で DayTrace 出力サンプルを照合する際に参照する。

---

## A-1: メタデータ漏洩（§6 違反）

検出対象:
- `[DayTrace] Phase N:` で始まるオーケストレーション行
- `[DayTrace] 日報を生成しました` 等の内部状態文字列
- `[DayTrace:trace]` で始まる行
- `Phase 2: mode=両方 | mode_source=default | item_count=29 | ...` 形式の structured log

対応 P 項目: **P3**（Chat-side Output Policy の実装）

---

## A-2: テキスト切れ（§8-1 該当）

検出対象:
- 文の途中で切断（「〜を整備し」「〜に向け」のような未完成の文末）
- 活動項目の番号が飛んでいる（1, 2, 3, 5 のように 4 が欠落）
- セクションの見出しはあるが本文が空または不完全

対応 P 項目: **P1**（Artifact storage policy — chat ではなくファイルに書き出すことで回避）

---

## A-3: 英語漏洩（§5-3 違反）

検出対象:
- 英語の内部処理語が日本語本文に混入
  - 例: `classification overlay CLI and load_classification_overlays`
  - 例: `content_key + readback`
  - 例: `Phase 2 content_key + readback; bump daytrace-plugin`
- エージェント定型文の混入（`Continuing autonomously` 等）
- 英語スクリプト名・関数名が根拠欄にそのまま記載されている

対応 P 項目: **P5**（事実/推測/禁止語ルール）

---

## A-4: 見出し仕様違反（§9 違反）

共有用日報での検出対象:
- 見出しが「今日の活動」になっている（正: 「今日の概要」）
- カテゴリ分割（実装 / 調査 / 設計・判断）が省略されている
- 「参考: 根拠一覧」セクションが欠落している
- 根拠が本文に混入している（共有用は本文に根拠を書かない）

投稿下書きでの検出対象:
- タイトルが内輪寄り（DayTrace コンポーネント名のみ等）
- 「背景 / 今日の中心 / 気づき」の基本構造が欠落している

対応 P 項目: **P7**（見出し構造の §9 準拠徹底）

---

## A-5: 禁止語・内部用語の露出（§5-2 違反）

検出対象（禁止語）:
- 「寄り道」
- 「今日の重心」（置換: 「今日の中心作業」等）
- 「実装密度の高い1日」
- 「〜でしょう」
- 「ハッカソン提出を控え」

検出対象（内部語）:
- `candidate_id`, `triage_status`, `trace` 等の内部状態語
- `skill_miner_prepare.py` 等のスクリプト名が日報本文に直接出ている
- `ISSUE-xxx.md`, `P1-P10` 等の内部管理識別子が本文に出ている

対応 P 項目: **P5**（事実/推測/禁止語ルール）

---

## A-6: chat 出力順序の問題（§6 参照）

検出対象:
- skill-miner の提案が日報と投稿下書きの間に挟まっている
  - 問題の順序: 日報 → skill-miner → 投稿下書き
  - 推奨順序: 日報 → 投稿下書き → skill-miner 提案
- Phase 番号がユーザー向け出力に出ている

対応 P 項目: **P3**（Chat-side Output Policy — `## Output Order` セクション）
