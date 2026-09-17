-- Once collection has started, these may be the sole retained observations.
-- Refuse before any DDL: downgrade requires an explicit preservation protocol.
LOCK TABLE agents_meta IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE agent_metric_observations, agent_metric_days, agent_metric_scans,
    agent_metric_file_cursors, agent_lifecycle_intervals IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM agent_metric_observations)
       OR EXISTS (SELECT 1 FROM agent_metric_days)
       OR EXISTS (SELECT 1 FROM agent_lifecycle_intervals)
       OR EXISTS (SELECT 1 FROM agent_metric_scans)
       OR EXISTS (SELECT 1 FROM agent_metric_file_cursors) THEN
        RAISE EXCEPTION 'Observed metric evidence exists; destructive downgrade refused';
    END IF;
END $$;

DROP TRIGGER IF EXISTS agents_meta_lifecycle_interval ON agents_meta;
DROP FUNCTION IF EXISTS record_agent_lifecycle_interval();
DROP TABLE IF EXISTS agent_lifecycle_intervals;
DROP TABLE IF EXISTS agent_metric_file_cursors;
DROP TABLE IF EXISTS agent_metric_scans;
DROP TABLE IF EXISTS agent_metric_days;
DROP TABLE IF EXISTS agent_metric_observations;
DROP TABLE IF EXISTS agent_metric_collection;

COMMENT ON TABLE heartbeat_pause_log IS
    'Append-only heartbeat-pause trail: one row per ava.self.pause_heartbeat call. The telemetry `heartbeat_paused` event stays the display surface.';
