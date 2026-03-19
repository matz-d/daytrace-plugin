# CLAUDE.md Immediate Apply Spec

`CLAUDE.md` 分類だけは low-risk immediate apply path を仕様として持つ。
この skill ではコード実装を前提にしないが、以下の contract を守る。

## Apply Rules

1. 対象は `cwd/CLAUDE.md` だけ
2. `cwd/CLAUDE.md` が無い場合は、新規作成として diff preview を作る
3. 追記先は `## DayTrace Suggested Rules` セクション末尾
4. セクションが無ければ新規作成する
5. 既存文言の書き換えや並び替えはしない
6. 重複候補は skip して理由を返す
7. 衝突候補は diff preview だけ出して終了する
8. `skill` / `hook` / `agent` は immediate apply しない

## Diff Preview Example

```diff
--- /dev/null
+++ cwd/CLAUDE.md
@@
+## DayTrace Suggested Rules
+
+- Use pytest for verification.
```

## daytrace-session での扱い

- diff preview を表示し、apply 成功後にだけ `--decision adopt --completion-state completed` を記録する
- apply をスキップした場合は `defer` として記録する
