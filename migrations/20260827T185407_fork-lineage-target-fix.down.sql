-- Restore the exact pre-fix values from the correction snapshots, then drop
-- the snapshots. The Loki live-stream correction (scripts/fix_fork_lineage_loki.py)
-- is not reversible here — re-running it with the corrected row's data undoes it.

UPDATE agents_meta m
   SET spawner = s.spawner
  FROM fork_lineage_fix_backfill_agents_meta s
 WHERE m.id = s.id;

UPDATE events e
   SET target_agent_id = s.target_agent_id
  FROM fork_lineage_fix_backfill_events s
 WHERE e.id = s.id
   AND e.event_name = 'fork';

DROP TABLE IF EXISTS fork_lineage_fix_backfill_agents_meta;
DROP TABLE IF EXISTS fork_lineage_fix_backfill_events;
