# Cross-Repo Handoff (schema v2)

`skill_creator_handoff` に付与される **別リポジトリ適用** のためのメタデータと、永続 bundle の形を定義する。

## 検出（`build_cross_repo_handoff_metadata`）

`skill_miner_prepare.py` がクラスタごとに付ける `dominant_workspace` / `workspace_paths` と、prepare の `config.workspace`（`--workspace`）を比較する。

| 条件 | `cross_repo` | `handoff_scope` | `detection_signals` 例 |
|------|--------------|-----------------|-------------------------|
| `dominant_workspace` を解決したパス ≠ `config.workspace` を解決したパス | `true` | `other_repo` | `dominant_workspace_mismatch` |
| `workspace_paths` のいずれかが `config.workspace` 配下にない | `true` | `other_repo` | `packet_workspace_outside_config_workspace` |
| `config.workspace` が無い（全日程観測など）かつ `dominant_workspace` あり | `false` | `current_repo` | `prepare_workspace_unset` |
| 上記以外で cross と判定されない | `false` | `current_repo` | （空または `multi_workspace_cluster`） |

`cross_repo_confidence` は `high` / `medium` / `low`。guardrail 分岐には使わず、UX 注記用。

## `skill_creator_handoff` 追加フィールド（v2）

| フィールド | 型 | 説明 |
|------------|-----|------|
| `handoff_schema_version` | `2` | スキーマ版 |
| `cross_repo` | bool | 別 repo 向けと判断したか |
| `target_workspace_hint` | string \| null | 開くべきリポジトリの絶対パス目安 |
| `current_workspace` | string \| null | 観測時 `--workspace` の解決パス |
| `handoff_scope` | `current_repo` \| `other_repo` | 適用スコープ |
| `execution_instruction` | string | 改行区切りの手順（日本語） |
| `workspace_resolution_note` | string | ユーザー向け短文 |
| `cross_repo_confidence` | string | `high` \| `medium` \| `low` |
| `detection_signals` | string[] | 判定の内訳 |
| `target_repo_display_name` | string \| null | `Path(target).name` 等 |
| `target_path_examples` | string[] | `workspace_paths` 先頭数件 |
| `presentation_block` | string | 永続化**後**に埋まる fenced テキスト（`context_file` パス含む） |

既存の `tool`, `entrypoint`, `prompt`, `instructions`, `suggested_invocation` は従来どおり。

## 永続 bundle（`--skill-creator-handoff-dir`）

- ファイル名: `handoff-{candidate_id_slug}.json`（**candidate 単位で latest-wins 上書き**）
- ルートに `handoff_schema_version: 2`
- `handoff` オブジェクトに上記 v2 フィールド + `context_file` / `context_format`

## Proposal 表示

- `種類` の直後に `適用先: 現在のリポジトリ` または `適用先: 別リポジトリ（…）`
- `workspace 注記:` に `workspace_resolution_note`（200 文字以内）

## 後方互換

v1 bundle（タイムスタンプ付きファイル名、`handoff_schema_version` なし）は読み手が無視可能。新規生成は v2 のみ。
