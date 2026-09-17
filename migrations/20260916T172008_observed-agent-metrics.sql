-- Compact observed measurements, never a complete billing ledger. The telemetry
-- queue can shed records before any sink; freshness does not prove completeness.
CREATE TABLE agent_metric_collection (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO agent_metric_collection (singleton) VALUES (TRUE);

CREATE TABLE agent_metric_observations (
    event_id NUMERIC(20, 0) PRIMARY KEY CHECK (event_id >= 0),
    agent_id BIGINT NOT NULL REFERENCES agents(id),
    occurred_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    kind TEXT NOT NULL CHECK (kind IN ('usage', 'turn', 'exec', 'activity')),
    model TEXT,
    usage_calls BIGINT NOT NULL DEFAULT 0 CHECK (usage_calls >= 0),
    unpriced_calls BIGINT NOT NULL DEFAULT 0 CHECK (unpriced_calls >= 0),
    tokens_in BIGINT NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out BIGINT NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    tokens_cached BIGINT NOT NULL DEFAULT 0 CHECK (tokens_cached >= 0),
    tokens_reasoning BIGINT NOT NULL DEFAULT 0 CHECK (tokens_reasoning >= 0),
    turn_total BIGINT NOT NULL DEFAULT 0 CHECK (turn_total >= 0),
    turn_ok BIGINT NOT NULL DEFAULT 0 CHECK (turn_ok >= 0),
    exec_ok BIGINT NOT NULL DEFAULT 0 CHECK (exec_ok >= 0),
    exec_failed BIGINT NOT NULL DEFAULT 0 CHECK (exec_failed >= 0),
    cost_usd NUMERIC NOT NULL DEFAULT 0 CHECK (cost_usd >= 0 AND cost_usd <> 'NaN'::numeric),
    turn_duration_seconds DOUBLE PRECISION CHECK (turn_duration_seconds >= 0 AND turn_duration_seconds < 'Infinity'::float8),
    active_seconds DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (active_seconds >= 0 AND active_seconds < 'Infinity'::float8),
    exec_seconds DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (exec_seconds >= 0 AND exec_seconds < 'Infinity'::float8)
);
CREATE INDEX agent_metric_observations_window_idx
    ON agent_metric_observations (agent_id, occurred_at);

CREATE TABLE agent_metric_days (
    agent_id BIGINT NOT NULL REFERENCES agents(id),
    day DATE NOT NULL,
    usage_calls BIGINT NOT NULL DEFAULT 0 CHECK (usage_calls >= 0),
    unpriced_calls BIGINT NOT NULL DEFAULT 0 CHECK (unpriced_calls >= 0),
    tokens_in BIGINT NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out BIGINT NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    tokens_cached BIGINT NOT NULL DEFAULT 0 CHECK (tokens_cached >= 0),
    tokens_reasoning BIGINT NOT NULL DEFAULT 0 CHECK (tokens_reasoning >= 0),
    turn_total BIGINT NOT NULL DEFAULT 0 CHECK (turn_total >= 0),
    turn_ok BIGINT NOT NULL DEFAULT 0 CHECK (turn_ok >= 0),
    exec_ok BIGINT NOT NULL DEFAULT 0 CHECK (exec_ok >= 0),
    exec_failed BIGINT NOT NULL DEFAULT 0 CHECK (exec_failed >= 0),
    cost_usd NUMERIC NOT NULL DEFAULT 0 CHECK (cost_usd >= 0 AND cost_usd <> 'NaN'::numeric),
    turn_duration_sum DOUBLE PRECISION NOT NULL DEFAULT 0,
    turn_duration_min DOUBLE PRECISION,
    turn_duration_max DOUBLE PRECISION,
    active_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    exec_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (agent_id, day)
);

-- A scan proves that this particular source/window was traversed, not that
-- upstream telemetry was collected without loss. Different runner mirrors are
-- independent sources, even when their date-stamped filenames are identical.
CREATE TABLE agent_metric_scans (
    source TEXT NOT NULL CHECK (source IN ('loki', 'full_jsonl', 'rollup_jsonl', 'archive_loki')),
    source_key TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    scanned_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (source, source_key, window_start, window_end),
    CHECK (window_end > window_start)
);

-- Runner-local source cursors advance in the same transaction as repaired facts.
CREATE TABLE agent_metric_file_cursors (
    source_key TEXT PRIMARY KEY,
    identity TEXT NOT NULL,
    position BIGINT NOT NULL CHECK (position >= 0),
    excluded_archive_rows BIGINT NOT NULL DEFAULT 0 CHECK (excluded_archive_rows >= 0)
);

-- Actual metadata transitions define nonterminated time from this epoch onward.
-- No reconstruction from lossy lifecycle telemetry, nor invented pre-cutover time.
CREATE TABLE agent_lifecycle_intervals (
    agent_id BIGINT NOT NULL REFERENCES agents(id),
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    PRIMARY KEY (agent_id, started_at),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX agent_lifecycle_intervals_open_idx
    ON agent_lifecycle_intervals (agent_id) WHERE ended_at IS NULL;
INSERT INTO agent_lifecycle_intervals (agent_id, started_at)
SELECT id, agent_metric_collection.started_at
FROM agents_meta CROSS JOIN agent_metric_collection
WHERE status <> 'terminated';

CREATE FUNCTION record_agent_lifecycle_interval() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE transitioned_at TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'terminated' THEN
            INSERT INTO agent_lifecycle_intervals (agent_id, started_at)
            VALUES (NEW.id, transitioned_at);
        END IF;
    ELSIF OLD.status = 'terminated' AND NEW.status <> 'terminated' THEN
        INSERT INTO agent_lifecycle_intervals (agent_id, started_at)
        VALUES (NEW.id, transitioned_at);
    ELSIF OLD.status <> 'terminated' AND NEW.status = 'terminated' THEN
        UPDATE agent_lifecycle_intervals SET ended_at = transitioned_at
        WHERE agent_id = NEW.id AND ended_at IS NULL;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER agents_meta_lifecycle_interval
    AFTER INSERT OR UPDATE OF status ON agents_meta
    FOR EACH ROW EXECUTE FUNCTION record_agent_lifecycle_interval();

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT ON agent_metric_observations TO ava_runner;
        GRANT SELECT, INSERT, UPDATE ON agent_metric_days, agent_lifecycle_intervals TO ava_runner;
        GRANT SELECT ON agent_metric_collection TO ava_runner;
        GRANT SELECT, INSERT, UPDATE ON agent_metric_scans, agent_metric_file_cursors TO ava_runner;
    END IF;
END $$;

COMMENT ON TABLE heartbeat_pause_log IS
    'Append-only heartbeat-pause trail: one row per ava.self.pause_heartbeat call. The latest row supplies Inspector pause duration without telemetry reads.';
