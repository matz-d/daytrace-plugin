# Skill Miner: classify（overlay）対象の絞り込み

Phase 3 の **classification overlay**（`classification-prompt.md` に従った 1 候補 1 JSON）は、LLM 呼び出しコストとユーザー認知負荷を抑えるため、**全候補ではなく「曖昧な候補だけ」**に限定する。

Python が「曖昧度スコア」を自動計算していない前提で、**オーケストレーション（daytrace-session / skill-miner の手順）**が候補リストを絞る。`skill_miner_proposal.py` は従来どおり overlay の有無にフォールバックするだけでよい。

（daytrace 開発リポジトリでは `docs/skill-miner-classify-targets.md` と同一内容。）

## 原則

| 方針 | 内容 |
|------|------|
| `rejected` | **原則 classify しない**（overlay を作らない）。観測ノート向けで、LLM 分類の価値が低い。 |
| 内部 contract | `classification_trace` / `classification_guardrail_signals` / `ready[]` の各フィールドは **proposal JSON 上は常に保持**。markdown の通常表示だけ圧縮する。 |

## 曖昧候補（overlay を検討する）

次の **いずれか**に当てはまる `ready` または `needs_research` 候補を優先する。

1. **skill ↔ agent が揺れやすい**  
   - ヒューリスティックが `skill` だが `intent_trace` やラベルに継続的役割・レビュー立場が強い、または逆に `agent` だが手順化できる疑いがある。

2. **skill ↔ CLAUDE.md が揺れやすい**  
   - 宣言的ルールと手順が混在し、`artifact_hints` / `constraints` / `acceptance_criteria` のどちらが主か判断が分かれる。

3. **ヒューリスティックの確信が弱い**  
   - `confidence` が `medium` / `weak`、または `suggested_kind` が空・デフォルト寄りのフォールバックに近い。

4. **`needs_research` からの昇格・分割後**  
   - `skill_miner_research_judge.py` が `promote_ready` 等で境界が変わった候補は、最終 `suggested_kind` を一度 LLM で揃えた方が説明責任が立つ。

## 原則 classify しない（overlay を省略してよい）

次に当てはまる場合は **overlay を書かず** `skill_miner_proposal.py` のヒューリスティック + guardrail のみでよい。

1. **明らかな `hook`**  
   - 例: `tests-before-close` + `run_tests` 先行など、`classification.md` の hook 狭義ゲートに乗るパターンで、他分類との競合が小さい。

2. **強い `CLAUDE.md` シグナル**  
   - `artifact_hints` に `claude-md`、または CLAUDE.md 系 `rule_hints` が明確で、ヒューリスティックも `CLAUDE.md` と整合している。

3. **`rejected`**  
   - 上表のとおり対象外。

4. **強い heuristic 一致で境界が閉じている**  
   - `confidence` が `strong` かつ、上記 1〜2 の「揺れやすい軸」に該当しない。

## 説明責任（「なぜこの候補だけ？」）

セッション内の判断ログ（例: `[DayTrace] パターン検出: ...`）に、**件数**だけでなく **絞り込み規準の一言**を載せる。

- 例: `classify 対象 2/6（境界: skill/agent・skill/CLAUDE.md、弱確信のみ）`

## 関連

- 手順の全体: `skills/daytrace-session/SKILL.md` Phase 3
- overlay contract: `classification-prompt.md`
- 分類境界: `classification.md`
