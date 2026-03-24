# P プランのステータス判定ガイド

output-polish.md §12 の各 P 項目が「実装済み」かどうかを判定するための基準。
対象ファイルを Read して、「実装済みの証拠」が含まれるかを確認する。

---

## P1: Artifact storage policy の実装

対象: `skills/daily-report/SKILL.md`, `skills/post-draft/SKILL.md`, `skills/daytrace-session/SKILL.md`

実装済みの証拠:
- `output_dir` が非 null の時にファイルを書き出す記述がある
- `mkdir` または「親ディレクトリが無ければ作成」の記述がある
- `report-private.md` / `report-share.md` の保存ルールが明示されている

---

## P2: 保存結果メッセージのファイル単位化

対象: `skills/daytrace-session/SKILL.md`

実装済みの証拠:
- ファイル単位で保存成功/失敗を明示する記述がある
- 「report-private.md を保存」のような具体例がある

---

## P3: Chat-side Output Policy の実装

対象: `skills/daytrace-session/SKILL.md`

実装済みの証拠:
- `## Chat Output Policy` セクションがある
- chat に出すもの / 出さないものの区別が定義されている
- `[DayTrace]` 等のメタデータを出さないルールがある
- §5-5 の mixed-scope 注記と再構成元の要約が chat で必須と書かれている

---

## P4: Canonical timeline ルールの追記

対象: `skills/daily-report/SKILL.md`, `skills/post-draft/SKILL.md`

実装済みの証拠:
- 時系列順（古い→新しい）の活動並べ方ルールがある
- `output-polish.md §5-1` への参照がある、または同等のルールが直接記載されている

---

## P5: 事実/推測/禁止語ルールの追記

対象: `skills/daily-report/SKILL.md`, `skills/post-draft/SKILL.md`

実装済みの証拠:
- `確認したい点` セクションを作らないと明記されている
- confidence が低い時の inline 注記ルールがある（`注記:` 形式等）
- 内部状態語（`candidate_id`, `triage_status` 等）を出さないルールがある

---

## P6: 出力品質ガードの SKILL.md 追記（低信頼度データポリシー含む）

対象: `skills/daily-report/SKILL.md`, `skills/post-draft/SKILL.md`

実装済みの証拠:
- 低信頼度データ（Chrome 履歴・workspace-file-activity 単独）の使用ポリシーが明示されている
- 「一回・補助・裏方」または同等の原則が書かれている
- 低信頼度データ単独で活動項目を構成しないルールがある

---

## P7: 見出し構造の SKILL.md 準拠徹底（§9 の見出し仕様を SKILL.md に反映）

対象: `skills/daily-report/SKILL.md`, `skills/post-draft/SKILL.md`

### §9-1 (report-private) チェック

実装済みの証拠:
- サンプルまたは推奨構成に以下が含まれている:
  ```
  ## 日報 YYYY-MM-DD
  ### 今日の流れ
  ### 未完了の手がかり
  ```

### §9-2 (report-share) チェック

実装済みの証拠（すべて満たすこと）:
- `### 今日の概要` が見出し構造に含まれている
- `### 実装`, `### 未完了の手がかり`, `### 参考: 根拠一覧` が含まれている
- 「今日の活動」ではなく「今日の概要」を使うと明記されている
- カテゴリ分割（実装 / 調査 / 設計・判断）を省略しないと明記されている
- 根拠は「参考: 根拠一覧」にまとめると明記されている

要確認:
- 共有用日報のタイトルが `## 日報 YYYY-MM-DD` のみで `（共有用）` サフィックスが無い場合は §9-2 との差異として記録する（必須要件ではないが、output-polish.md では明示されている）

### §9-3 (post-draft) チェック

実装済みの証拠:
- 標準構成（背景 / 今日の中心 / 気づき）が明記されている
- 非技術者向け・調査中心の別構成が定義されている
- `output-polish.md §9-3` への参照がある、または直接内容が記載されている

---

## P8: 根拠 source 名の正規化

対象: `skills/daytrace-session/SKILL.md`

実装済みの証拠:
- source 名の正規化マッピングテーブルがある
  - `git-history` → `Git の変更履歴`
  - `claude-history` → `Claude の会話ログ`
  - `codex-history` → `Codex の会話ログ`
  - 等

---

## P9: proposal 候補説明の差別化

対象: `skills/skill-miner/SKILL.md`

実装済みの証拠:
- 候補の説明を「各候補に固有の効果」として書くルールがある
- compact 表に「効果」と「アクション導線」列が定義されている
- 候補ごとに異なる説明を書くことが明示されている（汎用文言の使い回し禁止等）

---

## P10: 曖昧性解消 Ask の発火条件実装

対象: `skills/post-draft/SKILL.md`, `skills/daily-report/SKILL.md`

実装済みの証拠:
- Ask の発火条件が明示されている（「曖昧なままだと品質が落ちる場合のみ」等）
- 「最大 2 ターン」または同等の制限がある
- 抽象的な文体選択（技術寄り/振り返り寄り等）の Ask を禁止している

---

## P11: aggregate.json の扱い整理

対象: `skills/daytrace-session/SKILL.md`（または scripts の README）

実装済みの証拠:
- `aggregate.json` が「デバッグ・再利用用の任意同梱」として定義されている
- テンプレ埋め込み用の主データとして扱わないことが明記されている
