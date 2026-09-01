-- Drop the one-time correction snapshots after their rollback window.
--
-- They only preserved pre-correction values for a temporary rollback path;
-- retaining them after that window enlarges the data plane without serving a
-- runtime read path. IF EXISTS keeps a fresh baseline, which already omits the
-- retired tables, replayable.

DROP TABLE IF EXISTS fork_lineage_fix_backfill_agents_meta;
DROP TABLE IF EXISTS fork_lineage_fix_backfill_events;
DROP TABLE IF EXISTS ledger_unpriced_backfill_20260824;
