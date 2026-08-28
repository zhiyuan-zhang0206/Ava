-- Mergeable integer-second turn-duration distribution. The day rollup writes
-- this from Loki; this one-time UPDATE backfills only existing archive-era
-- ledger rows and deliberately leaves known ledger gaps absent.
ALTER TABLE agent_metrics_daily
    ADD COLUMN IF NOT EXISTS turn_dur_hist JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN agent_metrics_daily.turn_dur_hist IS
    'Integer-second floor(duration_seconds) bucket-to-count map; mergeable across days and backfilled for archive-era ledger rows.';
-- The archive read is guarded: the frozen PG `events` archive was dropped by
-- migration 20260829T030000_drop-events-archive (task #1823), so a fresh-DB
-- replay (baseline without the table) must skip the backfill.
DO $$
BEGIN
    IF to_regclass('public.events') IS NOT NULL THEN

WITH frozen AS (
    SELECT max(ts) AS ts FROM events
),
hist AS (
    SELECT agent_id, day, jsonb_object_agg(bucket::text, cnt) AS turn_dur_hist
    FROM (
        SELECT
            agent_id,
            (ts AT TIME ZONE 'UTC')::date AS day,
            floor((attributes ->> 'duration_seconds')::float8)::bigint AS bucket,
            count(*) AS cnt
        FROM events
        WHERE event_name ~ '^turn_end$'
          AND category = ANY(ARRAY['telemetry', 'log'])
          AND ts < (SELECT ts FROM frozen)
        GROUP BY agent_id, day, bucket
    ) grouped
    GROUP BY agent_id, day
)
UPDATE agent_metrics_daily AS metrics
SET turn_dur_hist = hist.turn_dur_hist
FROM hist
WHERE metrics.agent_id = hist.agent_id AND metrics.day = hist.day;

    END IF;
END $$;
