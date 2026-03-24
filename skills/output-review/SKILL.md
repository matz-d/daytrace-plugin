---
name: output-review
description: >
  DayTrace の出力結果（日報・ダイジェスト・投稿下書き・パターン提案）をレビューし、
  output-polish.md の基準で UX ギャップを検出して、P 番号プランの未実装項目を
  SKILL.md に反映する。「出力をレビューして」「UX を改善したい」「配布先ユーザー視点で
  フィードバックして」「P7/P番号の着手をお願い」「SKILL.md を出力仕様に合わせたい」
  「出力品質を確認して」と言われた時に必ずこのスキルを使う。
  output-polish.md の P プランを SKILL.md に段階的に適用したい場合も同様。
user-invocable: true
---

# Output Review

DayTrace の出力品質を output-polish.md 基準でレビューし、P 番号プランの未実装項目を特定して SKILL.md に反映する。

## Goal

- DayTrace 出力サンプルを §A（違反パターン）と照合して UX ギャップを列挙する
- output-polish.md §12 の全 P 項目を読み、各 SKILL.md の実装状態を判定する
- 未実装/要確認の項目を優先度付きで提案する
- ユーザー確認後、対象 SKILL.md に diff-preview → Edit を適用する
- daytrace-session への波及がある変更は必ず別途確認を入れる

やらないこと:
- DayTrace のログ収集や日報生成（daytrace-session が担う）
- Python スクリプトの修正
- ユーザー確認なしに SKILL.md を変更する

## Inputs

- **DayTrace 出力サンプル**（任意）: チャットにペーストされたテキスト、または `output_dir` のファイルパス
- **対象 P 番号**（任意）: 「P7 の着手」「P6 から」のように指定。未指定なら全項目を確認する

## Entry Contract

- ask なしで Phase 1 から自律実行する
- 出力サンプルが未提供の場合は Phase 1 をスキップし Phase 2 から開始する
- P 番号が明示されている場合は、その項目に絞って Phase 2–4 を実行してよい
- SKILL.md が肥大化する場合は references/ に詳細を分離し、ポインタだけ本文に残す

## Phase 1: 出力レビュー（サンプルがある場合のみ）

提供された出力を読み、次の違反パターンを検出する（詳細は `references/violation-patterns.md`）:

| カテゴリ | 検出対象 |
|---------|---------|
| A-1: メタデータ漏洩 | `[DayTrace] Phase N:` 等のオーケストレーション行 |
| A-2: テキスト切れ | 文の途中切断・項目番号の飛び |
| A-3: 英語漏洩 | 英語の内部処理語が日本語本文に混入 |
| A-4: 見出し仕様違反 | §9 の構造と不一致（「今日の活動」等） |
| A-5: 禁止語露出 | 「寄り道」「今日の重心」等 |
| A-6: 出力順序の問題 | 日報→投稿下書き→提案の順と異なる |

検出結果フォーマット:

```text
【検出した問題】
- [A-1] メタデータ漏洩: `[DayTrace] Phase 2: mode=両方 ...` が混入
- [A-4] 見出し仕様: 共有用の見出しが「今日の活動」（正: 「今日の概要」）
→ N 件検出 / または問題なし
```

Phase 1 の結果は Phase 2 の優先度判断に使う（検出した違反に対応する P 項目を優先して確認する）。

## Phase 2: P プランのステータス確認

`${CLAUDE_PLUGIN_ROOT}/docs/output-polish.md` の §12（実装優先順）を読む。
次に、各 P 項目の対象 SKILL.md ファイルを Read して実装状態を判定する。
**判定基準は `references/p-plan-check.md` を参照すること**（ファイルが実際に含む内容で判断する）。

ステータステーブルを出力する:

```text
| P#  | 内容                              | 状態        | 対象ファイル               |
|-----|-----------------------------------|-------------|--------------------------|
| P1  | Artifact storage policy           | ✅ 実装済み  | -                        |
| P7  | 見出し構造 §9 準拠徹底               | ❓ 要確認    | daily-report, post-draft |
| P9  | proposal 候補説明の差別化            | ❌ 未実装    | skill-miner              |
```

状態の定義:
- `✅ 実装済み`: 判定基準を満たす記述が SKILL.md に存在する
- `❓ 要確認`: 部分的に存在するが、仕様との完全一致を確認できない
- `❌ 未実装`: 判定基準を満たす記述が存在しない

## Phase 3: 実装提案

未実装（❌）・要確認（❓）の項目を優先度順に提案する。Phase 1 で検出した違反に対応する項目は優先度を上げる。

提案フォーマット:

```text
【次の実装候補】
1. P7: 見出し構造の §9 準拠徹底
   変更対象:
   - skills/daily-report/SKILL.md → 共有用日報の見出しを §9-2 に合わせる
   - skills/post-draft/SKILL.md → 見出し構造セクションを §9-3 と照合
   「P7 を適用して」と言うと diff-preview を出します。
```

## Phase 4: 実装（ユーザー指示後）

1. 対象 SKILL.md を Read する
2. `references/p-plan-check.md` の「実装内容」を参照して変更箇所を特定する
3. diff-preview を出す（before/after を明示）
4. 確認を得てから Edit を実行する
5. daytrace-session への波及がある変更は別の diff-preview → 確認 → Edit の順で行う
6. 完了後に「実装したこと」と「残りの未実装項目数」を 1-2 行で簡潔にまとめる

SKILL.md の変更は最小限にとどめ、肥大化する場合は references/ に詳細を分離する。

## Escalation Conditions

次の場合のみ確認を入れる:

- 変更が daytrace-session の Execution Flow または Execution Rules に影響する場合
- P 項目の実装状態が SKILL.md を読んでも判定不能な場合: 「確認できない」と明示して次項目へ進む

## References

詳細な判定基準と違反パターンの説明:
- `references/p-plan-check.md` — 各 P 項目の実装済み判定基準と実装内容の要点
- `references/violation-patterns.md` — §A の違反パターン詳細説明
