-- The metrics digest, the ops monitor and the rollup count a window's rows per agent and per
-- event name. An index on ts that carries both columns answers those counts from the index alone,
-- without reading the table's wide rows; it also serves every plain ts range scan, so it replaces
-- the bare ts index.
CREATE INDEX IF NOT EXISTS telemetry_events_ts_agent_name
    ON telemetry_events (ts)
    INCLUDE (agent_id, event_name);
DROP INDEX IF EXISTS telemetry_events_ts;
