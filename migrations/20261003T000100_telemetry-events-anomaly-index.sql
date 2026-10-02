-- Warning, error and critical rows are a small share of telemetry_events, and the stats
-- dashboard and the event-class resolution pass count exactly those over windows of minutes
-- to a week. A partial index that carries the grouped columns answers those counts without
-- reading the info and debug rows that make up most of the table. The predicate is spelled
-- as a literal in every reader so the planner can prove it matches.
CREATE INDEX IF NOT EXISTS telemetry_events_anomaly_ts
    ON telemetry_events (ts)
    INCLUDE (cluster, category, level, event_name, source, process)
    WHERE level IN ('warning', 'error', 'critical');
