# DayTrace Source CLI Contract

`scripts/` contains all CLI scripts for DayTrace, organized into two groups:

**共通 CLI** — 全 skill が使う:

| Script | 役割 |
|--------|------|
| `aggregate.py` | 5 source を統合し中間 JSON を返すオーケストレーター |
| `daily_report_projection.py` | store-backed `activities` から daily-report 用の aggregate 互換 JSON を返す adapter |
| `post_draft_projection.py` | store-backed `activities` と cached `patterns` から post-draft 用の aggregate 互換 JSON を返す adapter |
| `common.py` | 共有ユーティリティ（JSON I/O, エラー処理, CLI 引数） |
| `git_history.py` | Git commit 履歴の source CLI |
| `claude_history.py` | Claude 会話履歴の source CLI |
| `codex_history.py` | Codex 会話履歴の source CLI |
| `chrome_history.py` | Chrome 閲覧履歴の source CLI |
| `workspace_file_activity.py` | ファイル変更検出の source CLI |
| `sources.json` | source レジストリ（preflight, timeout, confidence_category） |

**skill-miner 専用 CLI** — `/skill-miner` skill だけが使う:

| Script | 役割 |
|--------|------|
| `skill_miner_prepare.py` | 全セッションを圧縮 candidate view に変換 |
| `skill_miner_detail.py` | 選択候補の session_ref から raw detail を再取得 |
| `skill_miner_research_judge.py` | 追加調査後の structured conclusion |
| `skill_miner_proposal.py` | prepare + judge → 最終 proposal 組み立て |
| `skill_miner_common.py` | skill-miner 共有ユーティリティ |

## Common output contract

Each source CLI must print one JSON object to stdout.

Success shape:

```json
{
  "status": "success",
  "source": "git-history",
  "events": [
    {
      "source": "git-history",
      "timestamp": "2026-03-09T14:30:00+09:00",
      "type": "commit",
      "summary": "Implement source CLI",
      "details": {},
      "confidence": "high"
    }
  ]
}
```

Skipped shape:

```json
{
  "status": "skipped",
  "source": "chrome-history",
  "reason": "not_found",
  "events": []
}
```

Error shape:

```json
{
  "status": "error",
  "source": "codex-history",
  "message": "history.jsonl is unreadable",
  "events": []
}
```

## Required event fields

- `source`
- `timestamp`
- `type`
- `summary`
- `details`
- `confidence`

`details` is required but source-specific.

`git_history.py` may also emit a `worktree_status` event for windows that include the local current day. Its `details` can include `branch`, tracked `staged_files` / `unstaged_files`, counts, and lightweight path summaries such as `path_kinds`, `dominant_kind`, `languages`, and `top_dirs`.

AI history sources (`claude_history.py`, `codex_history.py`) additionally use these conventions in `details` when canonical skill-miner packets are available:

- `ai_observation`: canonical packet payload for the event or summarized logical session
- `ai_observation_packets`: list form of the same canonical packets when one event summarizes multiple logical packets
- `logical_packets`: source-native logical packet breakdown used to reconstruct the summarized event
- canonical packets may include richer telemetry such as `tool_trace`, `tool_argument_patterns`, `tool_call_examples`, `intent_tool_alignment`, and `workflow_signals`
- tool call detail objects may include explicit execution metadata such as `result_status`, `exit_code`, and `error_excerpt` when rollout-native result data is available; otherwise the field set stays minimal and higher-level logic falls back to heuristic inference

## Source registry fields

`sources.json` supports these additional declarative fields:

- `prerequisites`: preflight checks such as `git_repo`, `path_exists`, `all_paths_exist`, `glob_exists`, `chrome_history_db`
- `confidence_category`: source role used by grouping confidence rules, such as `git`, `ai_history`, `browser`, `file_activity`
- `scope_mode`: source-level scope semantics for mixed-scope aggregation. Use `all-day` for sources that represent the whole day and `workspace` for sources limited to the requested/current workspace

## Manifest draft and identity

The pre-AR2 manifest draft is documented in `source-manifest-draft.md`.

Current loader rules:

- built-in `sources.json` and future single-object drop-in manifests share the same logical shape
- `source_registry.load_registry()` is the unified entrypoint for built-in + user drop-in discovery
- user drop-ins are discovered from `~/.config/daytrace/sources.d/*.json`
- each source gets a stable `source_identity` where `source_id == name`
- each source gets a `manifest_fingerprint` derived from the logical manifest fields only
- runtime orchestration fields such as `required`, `timeout_sec`, and `platforms` are intentionally excluded from the fingerprint
- duplicate source names are rejected so built-in and future user sources can share one registry namespace

Validation policy:

- reject missing required fields and type mismatches
- allow unknown extra keys so future registry extensions do not break built-in manifests
- keep prerequisite validation structural in the loader and defer subtype-specific checks to preflight evaluation
- invalid registry entries are reported as machine-readable `registry_errors` in CLI error JSON

## SQLite store

`AR2` introduces a rebuildable SQLite store for `source_runs` and `observations`.

- default store path: `~/.daytrace/daytrace.sqlite3`
- override path: `aggregate.py --store-path /path/to/daytrace.sqlite3`
- disable persistence for one run: `aggregate.py --no-store`
- run context and fingerprint rules are documented in `store-run-context-note.md`

`AR4` adds projection adapters on top of the store:

- `daily_report_projection.py` reuses persisted `activities` and falls back to one `aggregate.py` hydration run only when the requested slice is missing
- `post_draft_projection.py` does the same and also attaches cached `patterns` when a matching skill-miner window exists

## Shared CLI conventions

- `--since` and `--until` accept ISO 8601 datetime or `YYYY-MM-DD`
- `--date` accepts `today`, `yesterday`, or `YYYY-MM-DD` as a shorthand for single-day aggregation
- `--group-window` overrides the default 15 minute grouping window
- `--workspace` defaults to the current working directory where relevant
- `--all-sessions` disables workspace filtering for Claude/Codex history
- `--store-path` overrides the SQLite store location
- `--no-store` skips store ingestion for that run
- `--user-sources-dir` overrides the drop-in manifest directory for registry testing or custom installs (collection-only; auto-mode completeness validation always uses the default directory — use `--sources-file` for custom manifest validation)
- `--limit` caps returned events for manual inspection

## Aggregator output

`aggregate.py` emits a reusable intermediate JSON with these top-level keys:

- `sources`: normalized per-source execution results
- `timeline`: merged event list sorted by timestamp
- `groups`: nearby events grouped with `evidence` and aggregated `confidence` (see Group Contract below)
- `summary`: source status counts, total event count, total group count, and `no_sources_available`

Each entry in `sources[]` includes:

- `name`: source name
- `status`: `success`, `skipped`, or `error`
- `scope`: copied from `sources.json.scope_mode` so downstream skills can tell whether that source represents `all-day` or `workspace` evidence
- `events_count`: normalized event count
- optional `reason`, `message`, `command`, `duration_sec`

### Group Contract

Each entry in `groups[]` includes:

- `id`: stable group identifier (e.g. `group-001`)
- `start_timestamp`, `end_timestamp`: ISO 8601 boundaries
- `summary`: human-readable summary
- `confidence`: aggregated `high` / `medium` / `low` (from source category rules)
- `confidence_breakdown`: `{category: event_count}` — per-category event distribution (e.g. `{"git": 2, "browser": 1}`)
- `event_confidence_breakdown`: `{confidence: event_count}` — per-event confidence distribution before group aggregation
- `confidence_basis`: records both the category-derived confidence and the strongest observed event confidence used to derive the final group confidence
- `sources`: sorted list of contributing source names
- `confidence_categories`: sorted set of categories
- `scope_breakdown`: sorted list of scope_modes present (e.g. `["all-day", "workspace"]`)
- `mixed_scope`: `true` if both `all-day` and `workspace` sources contribute
- `source_count`, `event_count`: counts
- `evidence`: representative events selected by salience (git > ai_history > browser > file_activity), capped at `evidence_limit`
- `evidence_overflow_count`: number of events beyond the evidence limit
- `events`: full event list (same objects as in `timeline[]`)

Grouping parameters:

- `group_window_minutes` (default 15): max gap between consecutive events
- `max_span_minutes` (default 60): max total span of a single group; prevents rolling-chain accumulation
- `evidence_limit` (default 5): max number of representative events in `evidence[]`; additional events are counted in `evidence_overflow_count`

Aggregator behavior:

- forwards `--workspace` to source CLIs and also runs them with that directory as `cwd`
- prints a one-line preflight summary to `stderr` before collection starts
- uses `sources.json` metadata to evaluate preflight availability and confidence categories without source-name conditionals
- preserves mixed-scope behavior explicitly: `all-day` sources can describe the full day while `workspace` sources stay repo-local, and `sources[].scope` makes that visible to consumers

## Skill Miner CLIs

`skill-miner` uses two standalone CLIs that do not go through `aggregate.py`.
Deep research adds helper CLIs for post-detail judgment and final proposal formatting.

### `skill_miner_prepare.py`

Purpose:

- reads raw Claude/Codex JSONL directly
- defaults to `--days 7`
- keeps the configured day window even with `--all-sessions`
- treats `--all-sessions` as a workspace-filter override, not an unlimited history mode
- starts workspace mode at 7 days and expands to 30 days only when packet/candidate volume is too small
- splits Claude history into logical sessions
- emits compressed `candidates` and `unclustered` packets for proposal phase
- can emit `intent_analysis` for B0 observation with `--dump-intents`

Top-level shape:

```json
{
  "status": "success",
  "source": "skill-miner-prepare",
  "candidates": [],
  "unclustered": [],
  "sources": [],
  "summary": {},
  "config": {},
  "intent_analysis": {
    "summary": {},
    "items": []
  }
}
```

Important fields:

- `config.days`: default `7`
- `config.effective_days`: actual observation window after adaptive expansion
- `config.all_sessions`: disables workspace filtering but keeps the configured day window
- `config.input_source`: `raw` or `store`
- `config.input_fidelity`: `original`, `canonical`, or `approximate`
- `config.observation_mode`: `workspace` or `all-sessions`
- `config.date_window_start`: ISO 8601 threshold used for the effective window
- `config.adaptive_window`: workspace-only expansion metadata, including thresholds, initial counts, and whether 30-day fallback was used
- `config.adaptive_window.expanded`: canonical flag for whether workspace mode expanded from the initial window to 30 days
- `summary`: packet counts, candidate counts, and blocking stats only
- `candidates[].session_refs`: stable references for detail lookup
- `candidates[].support`: packet counts and ranking evidence
- `candidates[].confidence`, `proposal_ready`, `triage_status`: proposal quality and triage outcome
- `candidates[].quality_flags`, `evidence_summary`: why a candidate is strong, weak, or held back
- `candidates[].evidence_items`: up to 3 proposal-ready evidence entries with `session_ref`, `timestamp`, `source`, `summary`
- `candidates[].research_targets`: up to 5 suggested refs for deep research on `needs_research` candidates
- `candidates[].research_brief`: suggested questions and decision rules for deep research
- `unclustered[]`: packets that did not form a repeated cluster
- `intent_analysis.summary`: `generic_rate`, `synonym_split_rate`, `specificity_distribution`
- `intent_analysis.items`: anonymized `primary_intent` samples for B0 inspection

Contract notes:

- `summary` in `evidence_items[]` prefers the session's `primary_intent` (the same normalized intent sampled in `intent_analysis.items`); when empty it falls back to an anonymized representative snippet from the conversation
- `prepare` is the only phase that reads raw history for evidence chain construction
- `--input-source store` reads persisted `claude-history` / `codex-history` observations instead of raw history
- new store slices persist canonical skill-miner packet payloads inside source observation details; canonical reuse now requires packet schema v2 (`packet_version=2`) plus the required v2 fields
- packet payloads distinguish `user_rule_hints` (single-message user directives usable for clustering) from `user_repeated_rules` (strict repeated directives kept as higher-confidence evidence)
- `task_shape`, `artifact_hints`, and `representative_snippets` are derived from cleaned user messages first; assistant text is only used as a fallback when no usable user text was captured
- `user_rule_hints` are directive-only; explanatory mentions of labels such as `findings-first` or `file-line-refs` are ignored unless the user is actually instructing the agent
- store-backed prepare reuses only valid v2 canonical packets; stale or invalid packets fall back to highlight-based reconstruction in forced store mode and force raw fallback in auto mode
- if a store slice was hydrated before canonical packet payloads were upgraded to v2, re-running aggregate for that window is required to recover raw/store parity
- `--input-source auto` reuses the store only when the matching slice is complete for the current source manifest; missing, partial, degraded, stale, or unvalidated slices fall back to raw history
- `--sources-file` lets auto mode validate the current manifest against a specific source registry instead of the built-in default
- `--compare-legacy` adds a lightweight comparison summary between the selected path and the raw-history path
- normal broad-scope execution is `--all-sessions --days 7`; use a larger explicit `--days` only when a longer observation window is intentionally needed
- `--decision-log-path` lets prepare read prior decision state for carry-forward / suppression; orchestration should pass the same path to proposal so write/read stay aligned
- no state file is used; execution mode is determined only by CLI flags

`candidates[].evidence_items[]` example:

```json
[
  {
    "session_ref": "codex:abc123:1710000000",
    "timestamp": "2026-03-10T09:00:00+09:00",
    "source": "codex-history",
    "summary": "SKILL.md の構造確認を行い、提案理由を整理"
  }
]
```

### `skill_miner_detail.py`

Purpose:

- accepts one or more `session_ref` values from prepare output
- returns user/assistant conversation detail for selected packets only

Top-level shape:

```json
{
  "status": "success",
  "source": "skill-miner-detail",
  "details": [],
  "errors": []
}
```

Important fields:

- `details[].messages`: pure user/assistant conversation log
- `details[].tool_calls`: aggregated command/tool usage when available

### `skill_miner_research_judge.py`

Purpose:

- accepts one candidate from prepare output and one detail payload
- returns a structured conclusion for deep research

Top-level shape:

```json
{
  "status": "success",
  "source": "skill-miner-research-judge",
  "candidate_id": "codex-abc123",
  "judgment": {}
}
```

Important fields:

- `judgment.recommendation`: `promote_ready`, `split_candidate`, or `reject_candidate`
- `judgment.proposed_triage_status`: suggested triage status after research
- `judgment.reasons`: short explanation list for the decision
- `judgment.split_suggestions`: candidate split axes when the verdict is `split_candidate`

### `skill_miner_proposal.py`

Purpose:

- accepts prepare output and optional research judgments
- optionally accepts classification overlays for LLM-first kind selection experiments
- skill handoff bundles use `handoff_schema_version: 2`, stable filenames `handoff-{candidate_id}.json` (latest-wins per candidate), and `presentation_block` for target-repo UX
- returns final `ready` / `needs_research` / `rejected` proposal sections and markdown
- renders the evidence chain directly from `candidates[].evidence_items[]`
- does not reload raw history

Top-level shape:

```json
{
  "status": "success",
  "source": "skill-miner-proposal",
  "ready": [],
  "needs_research": [],
  "rejected": [],
  "selection_prompt": null,
  "markdown": ""
}
```

When `--classification-targets-only` is set:

```json
{
  "status": "success",
  "source": "skill-miner-proposal",
  "mode": "classification_targets",
  "summary": {
    "target_count": 0,
    "target_candidate_ids": []
  },
  "classification_targets": []
}
```

Important fields:

- `ready`: proposal-ready candidates
- `needs_research`: candidates still held back after prepare and optional research judgment
- `rejected`: candidates and unclustered references that should not be proposed
- `markdown`: preformatted proposal sections for the LLM/user-facing output
- `decision_log_stub`: per-candidate persistence rows that bridge this run to the next `prepare`
- `user_decision_overlay`: how many normalized user decisions from `--user-decision-file` were matched and applied before persistence
- `persistence.decision_log`: append result for the shared JSONL decision log
- `persistence.skill_creator_handoff`: persisted handoff bundle metadata for `skill` proposals
- `ready[].classification_trace`: optional classification path for `llm` / `guardrail_override` results
- `classification_targets[]`: candidates that should receive a classification overlay after prepare + optional judge merge
- `classification_targets[].candidate`: prompt-ready snapshot limited to the `classification-prompt.md` input contract keys (`candidate_id` through `research_brief`)
- `--decision-log-path`: optional JSONL output path for `decision_log_stub`; pass the same path used by prepare if you want next-run carry-forward behavior to close the loop
- `--skill-creator-handoff-dir`: optional output directory for persisted skill scaffold / handoff bundles
- `--classification-file`: optional JSON overlay with `candidate_id` and `classification.llm_suggested_kind`
- `--classification-targets-only`: emit only `classification_targets[]` instead of the final proposal sections
- `--user-decision-file`: optional normalized decision payload; when provided, proposal overlays `adopt` / `defer` / `reject` before persisting the next decision-log row set

Contract notes:

- the CLI has defaults under `~/.daytrace`, but orchestrators should pass persistence paths explicitly so side effects stay intentional
- `prepare` readback and `proposal` persistence only form one learning loop when they share the same decision-log path
- `classification_targets[].candidate` is a prompt-input snapshot, not the full internal merged candidate object
- the proposal JSON is written to stdout; orchestration should redirect it to a session-specific temp file when a later step needs to read it again

### `skill_miner_decision.py`

Purpose:

- accepts one selected proposal candidate and the user's action
- emits a normalized `--user-decision-file` payload for `skill_miner_proposal.py`
- keeps incomplete adoption attempts in carry-forward instead of suppressing them too early

Top-level shape:

```json
{
  "status": "success",
  "source": "skill-miner-decision",
  "selected_candidate": {},
  "normalization": {},
  "decision": {},
  "decisions": []
}
```

Important fields:

- `selected_candidate.section`: where the chosen candidate came from (`ready`, `needs_research`, `rejected`)
- `selected_candidate.index`: 1-based index when selected via `--candidate-index`
- `normalization.persisted_user_decision`: stored decision after normalization
- `normalization.carry_forward`: whether the candidate should reappear next run
- `decision`: single normalized entry
- `decisions[]`: list form accepted by `skill_miner_proposal.py --user-decision-file`

Contract notes:

- `--candidate-index` is 1-based and only targets the `ready` list
- `--decision adopt --completion-state completed` persists `user_decision="adopt"` with `carry_forward=false`
- `--decision adopt --completion-state pending` is normalized to `user_decision="defer"` with `carry_forward=true`
- `defer` and `reject` always persist with `carry_forward=true`

### `session_ref` contract

- Claude: `claude:/absolute/path/to/file.jsonl:<epoch>`
- Codex: `codex:<session_id>:<epoch>`

These refs are the only supported bridge between prepare and detail.
