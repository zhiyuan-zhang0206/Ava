-- Reverse of the drop-events-archive migration: recreate the frozen
-- `events` archive structure (empty — the data lives in the Loki archive
-- stream and the cold pg_dump archive). Matches db/schema.sql's baseline
-- shape: partitioned by month with a DEFAULT catch-all and the query-pattern
-- indexes (partitioned indexes propagate to partitions created later by the
-- events-maintenance daemon).
CREATE TABLE events (
    id               BIGSERIAL,
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    trace_id         TEXT,
    span_id          TEXT,
    agent_id         BIGINT REFERENCES agents(id),
    machine          TEXT NOT NULL,
    process          TEXT NOT NULL,
    category         TEXT NOT NULL,
    event_name       TEXT NOT NULL,
    level            TEXT NOT NULL,
    source           TEXT NOT NULL,
    target_agent_id  BIGINT REFERENCES agents(id),
    attributes       JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE events_default PARTITION OF events DEFAULT;

CREATE INDEX idx_events_agent_ts ON events (agent_id, ts DESC);
CREATE INDEX idx_events_event_name_ts ON events (event_name, ts DESC);
CREATE INDEX idx_events_category_ts ON events (category, ts DESC);
CREATE INDEX idx_events_trace_id ON events (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX idx_events_machine_ts ON events (machine, ts DESC);
CREATE INDEX idx_events_level_ts ON events (ts DESC) WHERE level IN ('warning', 'error', 'critical');
CREATE INDEX idx_events_target_agent_id ON events (target_agent_id);
