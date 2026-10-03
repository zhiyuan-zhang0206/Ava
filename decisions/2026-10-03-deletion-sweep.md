# The 2026-10-03 deletion sweep: dead CLI flags, dead read endpoints, dead compatibility

## Context

Five domain inventories (CLI, config/env/roster/schedules, DB, docs/okf/skills, SDK+HTTP)
listed zero-caller surfaces, parallel entry points and transition leftovers, each finding
carrying caller greps against origin/main, a production-evidence window, and the commit that
introduced it. Execution splits into three tracks: this change (code + docs), a migration
track for the DB objects, and an operator track for machine `.env` lines and backup tables.
The inventories' own boundary applied: "no production use" is not "dead code" — anything with
a future/decisions or backup-recovery role was graded down to a decision, not deleted.

## Decision

1. CLI surfaces with no reachable behavior are deleted, not shimmed:
   - `restart --force-reap` was exactly `--mode force` (`stop.py` folded both into one
     `force=`); it dated from the 2026-09-07 pause/stop split, and its parameter chain goes
     with it.
   - `stop --stop-browser` had `default=True` and no writer — passing it changed nothing.
   - the `boot` entry in `_LITE_VERBS` was unreachable (`main()` dispatches `boot` before the
     lite opt-in runs).
   - the `mcp ls` / `plugins ls` aliases had no reference (the `agents context` alias, which
     documents use, stays).
   - `cluster health-probe`'s `--agent-min` / `--crash-loop-max-restarts` /
     `--crash-loop-window-minutes` and `health-probe-register --interval` had no setter; the
     defaults are the contract (the `AVA_HEALTH_PROBE_AGENT_MIN` config key stays), and the
     `--no-crash-loop-check` / `--no-schema-check` operator flags stay.
2. Dead read endpoints are deleted with their readers and tests:
   - `GET /api/agents/{id}/activity` + `select_activity_trail`: the writer (`ava.self.log`) went
     in 2026-08-02 and the reader had no consumer; the `agent_activity` table drop is a separate
     migration. This overturns the 2026-08-02-notice-sdk-slimming note that the activity
     endpoints "remain (history)" — history nobody reads is not a read surface.
   - `GET /api/agents/{id}/completion-notice-policy`: the policy is written through config and
     resolved by the delivery boundary; the exposing GET had no reader.
   - `GET /api/notices/escalations`: introduced as an "operator review queue" but never wired to
     one; escalation notices still reach the human queue on their own.
   - `GET /api/notices/resolved`: the unified `GET /api/notices` feed carries the resolved page
     for the one UI consumer; the standalone endpoint had none.
3. The Loki read side (`event_stream_selector`, `archive_stream_selector`,
   `split_index_label_window`, `escape_logql_label`, `ledger_gap_plan`, `retention_floor`, the
   era/slice/plan types) and `INDEX_LABEL_CUTOVER_AT` go: the last Loki event readers were
   removed by the 2026-10-03 telemetry-readers-on-postgres move. What stays is deploy-config
   pinning (`validate_loki_deploy_config` and its chain, `ARCHIVE_FREEZE_AT` for events
   maintenance).
4. The mcp-daemon pre-rename compatibility (`ava._mcps_daemon`) is deleted: its own delete
   condition is satisfied — a four-machine process census (ubuntu / macmini / company-mini /
   company-air) shows zero pre-rename processes and one `python -m ava.mcps._daemon` per host.
5. `AVA_RUNNER_MODE` is deleted from its last settings (the visual-baselines workflow env and
   the lazy-import tests' clean-env strips): nothing reads it. The config registry's negative
   assertion stays as the retirement pin.
6. Stale docs are corrected in the same change: the stranded-hold-recovery tombstone node is
   deleted with its one incoming wikilink; the host-deploy-state Notes no longer wait for a
   cleanup that shipped; renamed modules are spelled as they are (`group_closure`, the
   observation package); the tests doc's `tests/plugins/` contents and a retired test name are
   corrected; six mis-pathed wikilinks are fixed; `lint_ava_okf` now exempts inline code spans
   and fenced blocks from the wikilink scan (matching `check_doc_references`), so the syntax
   samples in the content-lint docs stop reporting.
7. Retained deliberately: `event-resolutions` (the dismissal is netted by the alert card), auth
   sessions, mcp clients, `GET /api/agents/{id}/events`, `XAI_API_KEY` (a local plugin uses
   it), `agents cancel/compact`, the `notices` / `memory search` CLI reads, and the
   `pause_owner` naming. `understanding_nodes_reuse` is kept for one more observation week.

## Alternatives rejected

- **Keep `--force-reap` as a hidden alias of `--mode force`.** A second name for one boolean is
  a compatibility surface with no user.
- **Delete the activity endpoint but keep the reader.** The reader's only caller was the
  deleted handler; a DB-backed reader with no caller is exactly the dead code this sweep
  removes.
- **Rewrite the stranded-hold-recovery node as prose in `state/`.** Its content is a removal
  record ("these things no longer exist"); the decisions/ axis carries removal facts, and the
  state node described nothing the system still has.
- **Rename `pause_owner` / strip the word "pause".** The word names live machinery (the
  maintenance hold, the `paused` posture, heartbeat pause); only the `ava pause` verb was
  deleted (2026-10-03).

## Consequences

- Scripts that spelled a deleted flag fail at argument parsing (none exist in the repository),
  and `--stop-browser` users lose a hidden no-op — a stop still closes the browser by default.
- `GET /api/agents/{id}/activity` consumers outside the repository would now 404; the recorded
  window showed none.
- The `agent_activity` table is unreferenced until the separate migration track drops it.
