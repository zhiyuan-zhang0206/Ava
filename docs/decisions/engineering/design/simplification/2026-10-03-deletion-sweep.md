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

## Addendum (2026-10-04): the work-failed ingest webhook

The user ruled (2026-10-04, through the CC -> #405 -> task #4962 line): `POST /api/work-failed`
is deleted. The SDK+HTTP inventory (C6) found no producer in the repository, on any machine's
`~/.ava`, or in the production window, and the ruling chose deletion over keeping a dead
ingest. One change removes the router and its schemas, the route's tests and doc page, the app
wiring (import, `include_router`, the pause-exempt set), the contract entry, the package
docstrings that listed it, and the route reference in the routers doc.

The TTL reaper's remote loop was the one live internal consumer: it retried stale
`work_failed_events` deliveries through
`gateway.routers.work_failed.reconcile_stale_work_failures` — the import the
2026-10-02-ttl-reaper-is-its-own-service note called out. With ingest gone nothing can write a
row, so the redelivery phase is deleted with the function (no other caller exists), along with
`work_failed_retry_grace_seconds` (the reconciler was its only reader). The `remote` loop keeps
only its shell-reclaim phase.

It was also the service's last non-test reference to the gateway, and removing it moved the
patch-target placement home of `services/ttl_reaper/tests/test_loops.py`: the file's one
gateway-side assertion (the gateway owns no reaper) moved to its own
`gateway/tests/test_ttl_reaper_not_in_gateway.py`, mirroring the completion-digest pin.

The `work_failed_events` table is not touched here. It now has no reader or writer; it is
registered for the DB track (dump, then drop). When dropping it, also drop its entry from
`tests/fixtures/provisioning.py`'s `_PER_TEST_TRUNCATE_TABLES` — removing the entry earlier
would leave the still-existing table uncovered by the truncate-isolation lint.

Living docs are corrected in the same change (the runbook's ttl-reaper row, the ttl_reaper
service doc, the api-tokens surface table); dated records (the 2026-10-02 decision, migration
history) stay as written. The deleted test file's two `.test_durations` entries are left to
the nightly refresh.
