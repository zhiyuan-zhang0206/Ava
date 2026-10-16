-- Reproducible read models; source checkpoint rows and blobs stay untouched.
CREATE TABLE IF NOT EXISTS run_timeline_segments (
    agent_id BIGINT NOT NULL,
    version INTEGER NOT NULL,
    session INTEGER NOT NULL CHECK (session >= 0),
    checkpoint_id TEXT NOT NULL,
    message_start INTEGER NOT NULL CHECK (message_start >= 0),
    message_end INTEGER NOT NULL CHECK (message_end >= message_start),
    local_start INTEGER NOT NULL CHECK (local_start IN (0, 1)),
    read_before TIMESTAMPTZ,
    read_after TIMESTAMPTZ,
    start_ts TIMESTAMPTZ,
    end_ts TIMESTAMPTZ,
    totals JSONB NOT NULL,
    PRIMARY KEY (agent_id, version, session),
    UNIQUE (agent_id, version, checkpoint_id)
);
CREATE INDEX IF NOT EXISTS run_timeline_segments_message_idx
    ON run_timeline_segments (agent_id, version, message_end);

CREATE TABLE IF NOT EXISTS run_timeline_message_facts (
    agent_id BIGINT NOT NULL,
    version INTEGER NOT NULL,
    session INTEGER NOT NULL,
    idx INTEGER NOT NULL CHECK (idx >= 0),
    start_ts TIMESTAMPTZ,
    end_ts TIMESTAMPTZ,
    request_ts TIMESTAMPTZ,
    before_totals JSONB NOT NULL,
    after_totals JSONB NOT NULL,
    PRIMARY KEY (agent_id, version, idx),
    FOREIGN KEY (agent_id, version, session)
        REFERENCES run_timeline_segments (agent_id, version, session) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS run_timeline_message_request_idx
    ON run_timeline_message_facts (agent_id, version, idx) WHERE request_ts IS NOT NULL;
CREATE INDEX IF NOT EXISTS run_timeline_message_start_idx
    ON run_timeline_message_facts (agent_id, version, start_ts, idx);
CREATE INDEX IF NOT EXISTS run_timeline_message_end_idx
    ON run_timeline_message_facts (agent_id, version, end_ts, idx);
CREATE INDEX IF NOT EXISTS understanding_nodes_span_end_idx
    ON understanding_nodes (agent_id, span_end);

CREATE TABLE IF NOT EXISTS run_timeline_blocks (
    agent_id BIGINT NOT NULL,
    version INTEGER NOT NULL,
    session INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    start_ts TIMESTAMPTZ NOT NULL,
    end_ts TIMESTAMPTZ NOT NULL,
    payload BYTEA NOT NULL,
    PRIMARY KEY (agent_id, version, session, ordinal),
    FOREIGN KEY (agent_id, version, session)
        REFERENCES run_timeline_segments (agent_id, version, session) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS run_timeline_blocks_start_idx
    ON run_timeline_blocks (agent_id, version, start_ts);
CREATE INDEX IF NOT EXISTS run_timeline_blocks_end_idx
    ON run_timeline_blocks (agent_id, version, end_ts);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT, DELETE ON run_timeline_segments,
            run_timeline_message_facts, run_timeline_blocks TO ava_runner;
    END IF;
END $$;
