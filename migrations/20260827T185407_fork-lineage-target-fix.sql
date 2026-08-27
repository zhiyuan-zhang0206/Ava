-- fork-lineage-target-fix: one-time data correction for the fork-lineage
-- ruling (2026-08-28, task #1879): a fork's lineage parent is the FORK SOURCE
-- agent, never the executor who triggered the fork. Two surfaces recorded the
-- executor as the parent:
--   1. agents_meta.spawner for forked agents (frontend tree parent fallback +
--      lineage display) — corrected to 'agent:<fork_source_agent_id>'.
--   2. the frozen events-archive fork rows — target_agent_id corrected to the
--      payload's fork_from (the archive row for #2894 was hand-fixed already;
--      this guards the rest).
-- Live-stream (Loki) fork rows are NOT reachable from SQL — the one known
-- misrecorded row (agent 3124, 2026-08-21) is corrected by
-- scripts/fix_fork_lineage_loki.py at deploy.
--
-- Re-run contract: the snapshot below captures the pre-fix state once; both
-- UPDATEs are idempotent (DISTINCT FROM guards), so a re-run is a no-op.

CREATE TABLE IF NOT EXISTS fork_lineage_fix_backfill_agents_meta AS
SELECT id, spawner
FROM agents_meta
WHERE fork_source_agent_id IS NOT NULL
  AND spawner IS DISTINCT FROM 'agent:' || fork_source_agent_id::text;

UPDATE agents_meta
   SET spawner = 'agent:' || fork_source_agent_id::text
 WHERE fork_source_agent_id IS NOT NULL
   AND spawner IS DISTINCT FROM 'agent:' || fork_source_agent_id::text;

CREATE TABLE IF NOT EXISTS fork_lineage_fix_backfill_events AS
SELECT id, target_agent_id
FROM events
WHERE event_name = 'fork'
  AND attributes->>'fork_from' IS NOT NULL
  AND target_agent_id IS DISTINCT FROM (attributes->>'fork_from')::bigint;

UPDATE events
   SET target_agent_id = (attributes->>'fork_from')::bigint
 WHERE event_name = 'fork'
   AND attributes->>'fork_from' IS NOT NULL
   AND target_agent_id IS DISTINCT FROM (attributes->>'fork_from')::bigint;
