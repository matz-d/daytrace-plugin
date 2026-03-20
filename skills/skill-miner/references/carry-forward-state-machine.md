# Carry-Forward State Machine

候補の lifecycle を形式化し、次回 prepare / proposal での挙動を決定論的にする。

## 状態遷移表

```
              ┌─────────────────────────────────────────────┐
              │                                             │
  [new_packet]─→ unclustered ──→ rejected (insufficient)   │
              │     │                                       │
              │     ▼                                       │
              │  clustered ──→ ready (proposal_ready=true)  │
              │     │              │                        │
              │     │              ▼                        │
              │     │         user_decision?                │
              │     │          ├─ adopt ──→ adopted         │
              │     │          ├─ defer ──→ deferred ──────→│ (次回 prepare)
              │     │          └─ reject ─→ user_rejected ─→│ (次回 prepare)
              │     │                                       │
              │     ▼                                       │
              │  needs_research ──→ promote_ready ──→ ready │
              │     │              ├─ split_candidate ──→ needs_research
              │     │              └─ reject_candidate ──→ rejected
              │     ▼                                       │
              │  rejected (weak/singleton) ────────────────→│ (次回 prepare)
              └─────────────────────────────────────────────┘
```

## 状態定義

| 状態 | 意味 | carry_forward | 次回出現条件 |
|------|------|---------------|-------------|
| `unclustered` | 単独パケット、クラスタに未所属 | — | 次回 prepare で再クラスタされれば出現 |
| `ready` | 提案可能。`proposal_ready=true` | `true` | user_decision が設定されるまで |
| `needs_research` | 追加調査が必要 | `true` | 調査結果または次回 prepare で解消 |
| `rejected` | 品質不足で見送り | `true` | パターン変化で再浮上可能（下記参照） |
| `adopted` | ユーザーが adopt 選択済み（確定） | `false` | CLAUDE.md: Suggested Rules と照合し重複 skip。skill/hook/agent: 生成成功（`done`）確認後のみ次回 suppress。成功未確認・中断時は `deferred` 相当で再出現。将来 store の adopted フラグで代替 |
| `deferred` | ユーザーが defer 選択済み | `true` | 常に再出現。`observation_count` が前回より増えていれば confidence 上昇 |
| `user_rejected` | ユーザーが reject 選択済み | `true` | 再浮上条件を満たした場合のみ（下記参照） |

## `needs_research` 遷移アクション定義

`needs_research` からの遷移は、prepare/proposal の判定ロジックによる**自動遷移**（ユーザー手動ではない）として扱う。

- `promote_ready`
  - 発火条件: 追加調査後に `proposal_ready=true` を満たす（intent が単一方向に収束し、support が最小件数を満たし、重複/ノイズ判定を通過）。
  - 遷移先: `ready`
- `split_candidate`
  - 発火条件: 1 候補内に複数 intent が混在し、分割後の各サブ候補で intent 純度が改善する見込みがある。
  - 遷移先: 分割したサブ候補を `needs_research` として再評価（親候補は分割済みとしてクローズ）。
- `reject_candidate`
  - 発火条件: 調査・分割を行っても最小品質基準（support/一貫性/再現性）に到達しない。
  - 遷移先: `rejected`

### `split_candidate` の反復上限と打ち切り

無限ループ防止のため、以下のガードを適用する。

1. `split_iteration_count` が `max_split_iterations`（既定 2）に到達したら、以降は split 不可。
2. split 後に改善がない（intent 純度が閾値未満、または有効サブ候補数が増えない）場合は split 不可。
3. split 不可時は、`proposal_ready=true` を満たせば `promote_ready`、満たせなければ `reject_candidate` に強制遷移。

## 再浮上条件（resurface rules）

### 指標と閾値（共通）

- `intent_trace` の比較は **Jaccard 類似度**で統一する。
- `intent_similarity_threshold` 既定値は `0.7`。
- 旧表記の「Jaccard 距離 > 0.3」は `1 - similarity > 0.3` であり、`similarity < 0.7` と同義。

### `rejected`（品質不足で見送り）の再浮上

`rejected` の候補が次回 prepare で再度候補化された場合、以下のいずれかを満たせば再浮上:

1. **pattern_changed**: 前回 `intent_trace` との Jaccard 類似度 `< intent_similarity_threshold`。
2. **support_grew**: 前回の `support.packets` から今回が 2 倍以上。
3. **cluster_strengthened**: `unclustered/singleton` 由来から、複数 packet を持つ cluster に昇格。
4. いずれも満たさない → suppress（再浮上させない）。

### `user_rejected`（ユーザー reject 済み）の再浮上

`user_rejected` は `rejected` より慎重に扱い、次のいずれかで再浮上:

1. **pattern_changed**: 前回 `intent_trace` との Jaccard 類似度 `< intent_similarity_threshold`。
2. **support_grew**: 前回 reject 時の `support.packets` より今回が 2 倍以上。
3. **time_elapsed**: `user_decision_timestamp` から 30 日以上経過。
4. いずれも満たさない → suppress（`carry_forward=false` と同等に扱う）。

## adopt 後の重複検出

| suggested_kind | 検出方法 | skip 条件 |
|----------------|----------|-----------|
| `CLAUDE.md` | `cwd/CLAUDE.md` の `## DayTrace Suggested Rules` を読む | 既存ルールと intent_trace の Jaccard 類似度 `>= intent_similarity_threshold`（既定 0.7） |
| `skill` | 生成セッションで `done`（成功）を確認（将来: store の adopted フラグ） | `done` 確認済み、または store `adopted=true` |
| `hook` | 同上 | 同上 |
| `agent` | 同上 | 同上 |

移行メモ（decision log → store adopted）:

- 初期 migration は `user_decision="adopt" && carry_forward=false` を adopted として取り込む
- 取り込み時に `adopted_at`（既存 timestamp を流用）と `adopted_via`（`skill` / `hook` / `agent` / `CLAUDE.md`）を保存する
- migration 後の主判定は store `adopted` を正とし、`carry_forward` は互換読み取り専用（legacy）として扱う

## `decision_key` と `content_key`（二次マッチ）

`decision_key` は `suggested_kind` を含むため、LLM overlay や heuristic の更新で分類だけが変わるとキーが一致しなくなり、従来の decision log 行との carry-forward が切れる。

対策として `content_key` を別途持ち、`skill_miner_prepare.py` の readback は次の順で照合する。

1. **一次**: `decision_key` が一致する行を採用（従来どおり）
2. **二次**: 一次で見つからないとき、`content_key` が一致し、かつログ上の `suggested_kind` と今回候補の `suggested_kind` が異なる場合のみ、該当行を採用する。このとき候補に `classification_migrated: true` を付与する

二次マッチは「分類だけが変わった」ケースに限定する。`content_key` が一致しても kind が同じなら二次では採用しない（一次で `decision_key` が一致しているはず）。

## observation_count の追跡

decision_log_stub に `observation_count` フィールドを追加:

```json
{
  "observation_count": 3,
  "prior_observation_count": 2,
  "observation_delta": 1
}
```

- `observation_count`: 今回の prepare で計算された support.packets
- `prior_observation_count`: 前回 decision_log の observation_count（初回は 0）
- `observation_delta`: 差分。defer 候補の confidence 変化を判断する材料
