# Sample Outputs

narrative draft 前提の出力例と fixture review 手順をまとめる。
目的は wording を固定することではなく、同じ aggregate fixture に対して主題選定、narrative continuity、reader override の効き方を人間がレビューできるようにすること。

## Review Viewpoints

同一 fixture を読むたびに、次の点を確認する。

- 主題が破綻していないか
- narrative として一本通っているか
- reader override で説明の粒度が変わるか
- mixed-scope の注記が coverage を誤認させないか

## Sample 1: Default Reader

想定:

- `reader` override なし
- デフォルト読者は `同じ技術スタックを使う開発者`
- 技術的な判断、詰まり、学びをそのまま書く

```markdown
# aggregate に scope を追加して、date-first な出力契約を崩さずに広げた日

この下書きは、1日のログと workspace ローカルの変更ログをもとに再構成しています。

## 背景
その日の DayTrace 作業は、`daily-report` と `post-draft` を date-first 前提へ寄せるための下地作りが中心でした。中でも大きかったのは、aggregator が返す `sources[]` に source ごとの `scope` を持たせて、全日ログと workspace 限定ログの混在を後段で説明できるようにしたことです。

## 今日の中心
中心になったのは `aggregate.py` と `sources.json` の更新でした。source registry に `scope_mode` を追加し、`summarize_source_result()` が `scope` を返すようにしたことで、downstream skill が mixed-scope を機械可読な形で扱えるようになりました。並行して `test_aggregate.py` に `--date today --all-sessions` と `--workspace /path` のケースを足し、`all-day` と `workspace` の両方が回帰なく出ることを確認しています。

難しかったのは、全 source を全日化する方向へ踏み込まずに、現行の hybrid 挙動だけを正しく説明できる contract に落とすことでした。`supports_all_sessions` から都度推論する形だと後段 skill が挙動を説明しづらくなるため、source registry に `scope_mode` を明示し、その値をそのまま出力へ流す方針に寄せました。

## 気づき
この変更で効いたのは、aggregate の top-level shape をほぼ変えずに、後段 skill が必要としていた説明責務だけを足せたことでした。特に `groups` の narrative 的な解釈は LLM 側に残しつつ、scope のような決定論的メタデータだけを先に固定すると、出力 skill の設計がかなり進めやすくなります。次は `post-draft` 側で `sources[].scope` を読み、冒頭の mixed-scope 注記ルールを narrative に組み込む予定です。
```

## Sample 2: `--reader "社内の非エンジニア"`

想定:

- `--reader "社内の非エンジニア"` を明示
- 技術用語は必要最小限にし、背景、進め方、成果を優先する
- 同じ fixture でも説明の粒度と比重を変える

```markdown
# 1日の作業記録を、外に説明しやすい形へ整理し直した

## 何に取り組んだか
この日は、開発作業の記録をあとから説明しやすくするための土台を整えました。

この下書きは、1日のログと workspace ローカルの変更ログをもとに再構成しています。

## 今日の中心
1 日全体の動きと、今開いている作業場所だけで確認できる変更とが混ざるため、その違いを出力時に明示できるようにしたのが主な進展でした。まず、集約結果に「その情報が 1 日全体のものか、現在の作業場所に限られるものか」を示す項目を追加しました。これにより、あとから日報や記事の下書きを作る際に、どこまでがその日の全体像で、どこからが repo ローカルな根拠かを説明しやすくなりました。あわせて、この違いが正しく出るかを確認するテストも足しています。

## 気づき
今回の整理で、出力側の文章が「1 日全体の話」と「今の作業場所に基づく話」を混同しにくくなりました。単に情報を増やすのではなく、説明の前提をそろえることで、あとから読む人にも意図が伝わりやすくなることが分かりました。
```

## Fixture Review Procedure

同じ aggregate fixture を使って、少なくとも次の 2 パターンを見比べる。

1. `reader` override なしで narrative を生成する
2. `--reader "社内の非エンジニア"` 付きで narrative を生成する

見るべき点:

- 同じ中心 group を起点にしていても、reader override に応じて背景説明の厚みが変わるか
- default reader では実装判断や学びが前に出ているか
- 非技術者向け override では成果、進め方、影響が先に読めるか
- mixed-scope 注記が長すぎず、かつ coverage を誤認させないか

追加で余力があれば、次の補助レビューも行う。

- `--topic "..."` を明示した場合に、3 段 fallback より override が優先されて読めるか
- source が 1-2 本だけの fixture でも narrative の骨格が維持されるか
- source が 0 本の fixture で空 narrative が破綻せず返るか
- `scope` が単一な fixture で mixed-scope 注記を無理に出していないか

## Why Fixture Review, Not Unit Tests

- 主題選定と narrative continuity は prompt policy の責務であり、決定論的 helper の pass/fail に落とし込まない
- wording の揺れそのものは不具合ではない
- 代わりに、同一 fixture を使った複数 sample を人間がレビューし、主題、流れ、reader override の差分を確認する
