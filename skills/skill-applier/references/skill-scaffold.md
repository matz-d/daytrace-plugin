# Skill Scaffold Draft Spec

`suggested_kind=skill` の candidate が選択された場合、DayTrace は skill scaffold context を構造化して提示する。
実際の skill 生成は `skill-creator` skill に委ねる。

## DayTrace 側の責務

1. candidate から `skill_scaffold_context` を構造化する（`build_skill_scaffold_context()` が返す）
2. context には `skill_name`, `goal`, `task_shapes`, `artifact_hints`, `rule_hints`, `execution_hints`, `representative_examples`, `evidence_summaries`, `observation_count` を含む
3. scaffold context を `skill-creator` への引き継ぎプロンプトとして出力する

## Output Template

```markdown
### Skill Scaffold Draft: {skill_name}

この候補は {observation_count}回の反復パターンから抽出されました。

**Goal:** {goal}
**成果物:** {artifact_hints}
**適用ルール:** {rule_hints}

**代表的な使用例:**
- {example_1}
- {example_2}

> `/skill-creator` で本格的な SKILL.md を生成できます。
  上記の context を skill-creator に渡してください。
```

## skill-creator への Handoff

- DayTrace は scaffold context を proposal markdown では構造化テキストとして提示し、skill-creator を自動起動しない
- `skill_miner_proposal.py --skill-creator-handoff-dir <dir>` を付けた場合は、ready な `skill` candidate ごとに JSON handoff bundle を 1 ファイル保存する
- 保存される bundle には `handoff_schema_version`（2）, `record_type`, `recorded_at`, `candidate_id`, `label`, `suggested_kind`, `context`, `handoff` が入る
- `handoff` 内の `presentation_block`（コードフェンス付き）をそのままユーザーに見せてよい（target repo / handoff file / 手順の 3 点セット）
- ファイル名は `handoff-{candidate_id}.json` で **同一 candidate の再実行は上書き**（latest-wins）
- cross-repo の判定とフィールド一覧は `skills/skill-miner/references/cross-repo-handoff.md`
- persisted handoff path は `skill_creator_handoff.context_file` として返り、監査や手渡し再利用に使える
- ユーザーが `/skill-creator` を呼ぶ際に context を参照して渡す
- proposal markdown の末尾に以下のガイドを表示する:

```markdown
> この候補を skill 化するには:
  `/skill-creator {skill_name} をスキルにしてください` と伝えてください。
  上記の Goal / 成果物 / 適用ルール / 代表例が引き継がれます。
```

- skill-creator は自然言語入力を受け付けるため、構造化 JSON の受け渡しは不要
- DayTrace の scaffold_draft / persisted handoff bundle は skill-creator にとっての参考情報であり、binding ではない

## DayTrace がやらないこと

- SKILL.md ファイルの直接生成
- skill-creator の自動起動
- skill のデプロイや有効化
- scaffold context の skill-creator 側フォーマットへの変換

## daytrace-session での扱い

- scaffold context を提示し、`done` が確認できた時だけ `--completion-state completed` を使う
- 確認できない場合は `pending` のまま session を閉じる

### `done` 確認フロー（`skill` 適用）

`--completion-state completed` の使用条件は以下で固定する。

1. **確認主体（誰が）**
   - daytrace-session を実行しているオーケストレーター（assistant）が確認を実施する。
2. **確認タイミング（どこで）**
   - scaffold context と skill-creator handoff ガイドを提示し終えた直後に、1 回だけ確認する。
3. **確認方法（どうやって）**
   - ユーザーからの明示的な完了意思（例: `done`, `完了`, `これでOK`, `この内容で進めてよい`）を受け取る。
   - 自動推定（出力済みだから完了とみなす等）は禁止する。
4. **確定条件（何をもって confirmed とするか）**
   - 上記の明示応答がある場合のみ `confirmed` とし、`--decision adopt --completion-state completed` を記録する。
   - 明示応答がない、離脱した、保留を示した場合は `confirmed` にしない。`--completion-state pending`（または `defer` / `reject`）を記録する。
