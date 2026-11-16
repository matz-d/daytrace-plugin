# DayTrace Source CLI Contract

`scripts/` contains the DayTrace log collectors. Nothing here writes reports, drafts, or proposals.

| Script | 役割 |
|--------|------|
| `collect_daily.py` | 今日（と、未保存なら昨日）のログを集めて store に保存する。launchd / cron 向けの入口 |
| `read_log.py` | store から指定日・期間のイベントを時系列で読み出す |
| `aggregate.py` | 各 source を実行して統合 JSON を返し、store に保存するオーケストレーター |
| `aggregate_core.py` | 日付の解決・source 実行・時系列とグループの組み立て |
| `store.py` / `derived_store.py` | SQLite store の書き込み / 読み出し |
| `request_text.py` | 依頼文から、ツールが差し込んだ文（system-reminder、引き継ぎ要約など）を除く |
| `common.py` | 共有ユーティリティ（JSON I/O, 日時, エラー処理） |
| `session_packets.py` | Claude / Codex の会話を作業のまとまり（logical packet）に分け、抜粋とツール呼び出しを整える |
| `git_history.py` | Git commit 履歴の source CLI |
| `claude_history.py` | Claude 会話履歴の source CLI。`session_summary` の `details.user_requests[]` に依頼文を残す |
| `codex_history.py` | Codex 会話履歴の source CLI。`session_meta` の `details.turns[]` に依頼文・きっかけ・開始/終了を残す |
| `chrome_history.py` | Chrome 閲覧履歴の source CLI |
| `workspace_file_activity.py` | 未コミットファイル変更の source CLI |
| `sources.json` | source レジストリ（preflight, timeout, confidence_category） |

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

Timestamps in `observations.occurred_at` are fixed-width UTC (`YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, schema v4) so that SQL string comparison matches time order. `read_log.py` reads only runs made with `--date`.

## Shared CLI conventions

- `--since` and `--until` accept ISO 8601 datetime or `YYYY-MM-DD`
- `--date` accepts `today`, `yesterday`, or `YYYY-MM-DD` as a shorthand for single-day aggregation. A day is the local calendar day (00:00-23:59); there is no 06:00 boundary
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

Terminology note:

- `sources[].scope` = source-level scope label copied from registry `scope_mode`
- `groups[].scope_breakdown` / `groups[].mixed_scope` = group-level result after aggregating multiple source scopes

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
