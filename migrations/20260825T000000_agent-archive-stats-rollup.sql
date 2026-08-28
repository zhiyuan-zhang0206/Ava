-- agent-archive-stats-rollup: materialize the inspector's immutable archive
-- reads once per agent. The events table froze at the Loki cutover, so these
-- values stay correct only while that archive remains frozen.

-- The squashed baseline already contains this table. Keep the incremental
-- migration convergent when it is applied after that baseline.
CREATE TABLE IF NOT EXISTS agent_archive_stats (
    agent_id          BIGINT PRIMARY KEY REFERENCES agents(id),
    turn_distribution JSONB NOT NULL DEFAULT '[]'::jsonb,
    active_seconds    DOUBLE PRECISION NOT NULL DEFAULT 0,
    exec_seconds      DOUBLE PRECISION NOT NULL DEFAULT 0,
    lifecycle         JSONB NOT NULL DEFAULT '[]'::jsonb,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE agent_archive_stats IS
    'Materialized whole-life inspector values from the pre-cutover events archive (task #1281: the raw archive lives in the Loki archive stream).';
COMMENT ON COLUMN agent_archive_stats.turn_distribution IS
    'Ascending JSON pairs [duration_seconds, count] for archived turn_end events.';
COMMENT ON COLUMN agent_archive_stats.active_seconds IS
    'Archived node_exit duration sum excluding claim nodes.';
COMMENT ON COLUMN agent_archive_stats.exec_seconds IS
    'Archived node_exit duration sum for exec nodes.';
COMMENT ON COLUMN agent_archive_stats.lifecycle IS
    'Ascending JSON pairs [UTC timestamp, event name] for archived lifecycle replay.';
COMMENT ON COLUMN agent_archive_stats.computed_at IS
    'Backfill time; this materialization is valid only while the events archive was frozen.';
-- The archive read is guarded: the frozen PG `events` archive was dropped by
-- migration 20260829T030000_drop-events-archive (task #1823), so a fresh-DB
-- replay (baseline without the table) must skip the backfill — the table's
-- CREATE/COMMENT above stay unconditional.
DO $$
BEGIN
    IF to_regclass('public.events') IS NOT NULL THEN

WITH archive_agents AS (
    SELECT DISTINCT agent_id
    FROM events
    WHERE agent_id IS NOT NULL
),
turn_distributions AS (
    SELECT agent_id,
        jsonb_agg(jsonb_build_array(value, count) ORDER BY value) AS turn_distribution
    FROM (
        SELECT agent_id,
            (attributes ->> 'duration_seconds')::float8 AS value,
            count(*) AS count
        FROM events
        WHERE event_name ~ '^turn_end$'
          AND category = ANY(ARRAY['telemetry', 'log'])
          AND ts < (SELECT max(ts) FROM events)
        GROUP BY agent_id, value
    ) grouped
    GROUP BY agent_id
),
node_durations AS (
    SELECT agent_id,
        COALESCE(
            sum((attributes ->> 'duration_seconds')::float8)
                FILTER (WHERE COALESCE(attributes ->> 'node', '') <> 'claim'),
            0
        ) AS active_seconds,
        COALESCE(
            sum((attributes ->> 'duration_seconds')::float8)
                FILTER (WHERE attributes ->> 'node' = 'exec'),
            0
        ) AS exec_seconds
    FROM events
    WHERE event_name ~ '^node_exit$'
      AND ts < (SELECT max(ts) FROM events)
    GROUP BY agent_id
),
lifecycle_events AS (
    SELECT agent_id,
        jsonb_agg(
            jsonb_build_array(
                to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                event_name
            )
            ORDER BY ts
        ) AS lifecycle
    FROM events
    WHERE event_name ~ '^(agent_spawned|agent_resurrected|agent_terminated)$'
      AND ts < (SELECT max(ts) FROM events)
    GROUP BY agent_id
)
INSERT INTO agent_archive_stats (
    agent_id, turn_distribution, active_seconds, exec_seconds, lifecycle
)
SELECT archive_agents.agent_id,
    COALESCE(turn_distributions.turn_distribution, '[]'::jsonb),
    COALESCE(node_durations.active_seconds, 0),
    COALESCE(node_durations.exec_seconds, 0),
    COALESCE(lifecycle_events.lifecycle, '[]'::jsonb)
FROM archive_agents
LEFT JOIN turn_distributions USING (agent_id)
LEFT JOIN node_durations USING (agent_id)
LEFT JOIN lifecycle_events USING (agent_id)
ON CONFLICT (agent_id) DO UPDATE SET
    turn_distribution = EXCLUDED.turn_distribution,
    active_seconds = EXCLUDED.active_seconds,
    exec_seconds = EXCLUDED.exec_seconds,
    lifecycle = EXCLUDED.lifecycle,
    computed_at = EXCLUDED.computed_at;

    END IF;
END $$;
