# CLAUDE.md Immediate Apply Spec

`CLAUDE.md` 分類だけは low-risk immediate apply path を仕様として持つ。
この skill ではコード実装を前提にしないが、以下の contract を守る。

## Pre-Apply Checks（apply 前の必須チェック）

apply 前に以下を順番に実行し、いずれかに引っかかった場合は即座に停止する。

### 1. セキュリティチェック

候補テキストに以下のパターンが含まれる場合は **apply 拒否**し理由を返す:

- API キー・シークレット（`sk-`, `api_key`, `API_KEY`, `secret` 等のキーワード＋値の組み合わせ）
- 認証情報（`password=`, `passwd=`, `token=`, `Bearer ` 等）
- DB 接続文字列（`postgres://`, `mysql://`, `mongodb://` 等）
- AWS / GCP / Azure の認証情報パターン

### 2. 簡潔性チェック

以下のいずれかに該当する場合は **skip**し理由を返す:

- 既存の CLAUDE.md 内の別ルールと意味的に等価（言い換えや重複）
- 既存ルールに明示的に包含される（より広いルールが既に存在する）
- 単体では意味が完結しない断片（例: 「常に確認する」だけでは何を確認するか不明）

## Apply Rules

1. 対象は `cwd/CLAUDE.md` だけ（root / parent / home 配置への対応は将来拡張候補）
2. **Pre-Apply Checks をパスした場合のみ** apply を進める
3. `cwd/CLAUDE.md` が無い場合は **New File Scaffold** で diff preview を作る
4. 既存 CLAUDE.md がある場合は **Section Placement** で追記先を決定する
5. 既存文言の書き換えや並び替えはしない
6. 重複候補は skip して理由を返す（Pre-Apply Checks で catch されていない完全一致）
7. 衝突候補は diff preview だけ出して終了する
8. `skill` / `hook` / `agent` は immediate apply しない

## Section Placement

追記先セクションの決定ルール（上から順に評価）:

1. **候補のルール性質を判定**する:
   - コーディング規約・型ヒント・フォーマット → `## Coding Standards`（または同義セクション）
   - テスト・CI・品質 → `## Testing` / `## Workflow`（または同義セクション）
   - プロジェクト固有のルール・禁止事項 → `## DayTrace Suggested Rules`
   - セキュリティ・危険操作の制限 → `## DayTrace Suggested Rules`

2. **既存セクションとのマッチング**:
   - 既存 CLAUDE.md に性質が一致するセクションがあれば、そのセクション末尾に追記する
   - 一致するセクションが無ければ、`## DayTrace Suggested Rules` セクション末尾に追記する
   - `## DayTrace Suggested Rules` が無ければ新規作成する（ファイル末尾に追加）

## New File Scaffold

`cwd/CLAUDE.md` が存在しない場合、公式推奨の **最小限スキャフォールド**で初期化する。

最小限の原則: 一度に全セクションを作らず、今回の候補に必要なセクションだけを含める。
ユーザーが段階的に Project overview / Directory map / Workflow 等を追加していく（DayTrace は追記のみ担い、既存コンテンツの再構成はしない）。

```diff
--- /dev/null
+++ cwd/CLAUDE.md
@@
+# {repo-name}
+
+## DayTrace Suggested Rules
+
+- {candidate_rule}
```

## Diff Preview Example（既存ファイルへの追記）

```diff
--- cwd/CLAUDE.md
+++ cwd/CLAUDE.md
@@ ## DayTrace Suggested Rules @@
+
+- Use pytest for verification.
```

## daytrace-session での扱い

- Pre-Apply Checks の結果（pass / security-reject / conciseness-skip）をチャットに表示する
- diff preview を表示し、apply 成功後にだけ `--decision adopt --completion-state completed` を記録する
- conciseness-skip の場合は `defer` として記録する
- security-reject の場合は `reject` として記録する
- apply をユーザーが手動スキップした場合も `defer` として記録する
