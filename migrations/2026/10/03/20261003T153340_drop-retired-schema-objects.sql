-- Contract the DB objects the 2026-10-03 deletion sweep found with no writer
-- and no reader left on origin/main (expand-contract: the code removals shipped
-- first).
--
--   * agents_meta.last_resurrect_at / last_wedged_check_at / last_claim_loop_at —
--     the per-agent backoff clocks of the CrashResurrect / Wedged / claim-loop
--     progress controllers; their writers went with the da08172d2 (#1924)
--     controller teardown. Crash-resurrect backoff now lives in
--     delivery_watchdog_attempts.last_attempt_at;
--   * agent_metric_file_cursors.excluded_archive_rows — the old telemetry-replay
--     INSERT's skip counter; the replay rewrite (33689f111) reads and writes only
--     (source_key, identity, position). Apply only on a release that contains
--     that rewrite;
--   * agent_activity — write-dead since ava.self.log() was removed (2026-08-02);
--     its only remaining reader, GET /api/agents/{id}/activity, is removed in the
--     same wave. The operator dumps the table before this migration runs, and it
--     is applied only together with (or after) that release; the rows are not
--     carried anywhere;
--   * agent_impersonation_entries_created — no query filters or sorts
--     agent_impersonation_entries by created_at; readers go through the
--     (lease_id, seq) primary key / (lease_id, event_key) unique index;
--   * agent_impersonations_retention — served the bounded-retention DELETE
--     removed in bd533e6e4 (#2421); idx_scan = 0 and no query shape uses it.
--
-- understanding_nodes_reuse is deliberately kept (observation window).

ALTER TABLE agents_meta DROP COLUMN IF EXISTS last_resurrect_at;
ALTER TABLE agents_meta DROP COLUMN IF EXISTS last_wedged_check_at;
ALTER TABLE agents_meta DROP COLUMN IF EXISTS last_claim_loop_at;
ALTER TABLE agent_metric_file_cursors DROP COLUMN IF EXISTS excluded_archive_rows;
DROP TABLE IF EXISTS agent_activity;
DROP INDEX IF EXISTS agent_impersonation_entries_created;
DROP INDEX IF EXISTS agent_impersonations_retention;
