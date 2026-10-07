-- The understanding tree's chunk-triggered pipeline: the job queue, the raw record of every
-- provider call, and the upper-level grouping cursor and call record. All idempotent (IF NOT
-- EXISTS): db/schema.sql already holds these for a fresh database, and the ava_runner grants are
-- gated on the role's existence so the fresh-bootstrap replay stays green (base/cluster/authority/
-- groups.py grants the same surface at birth and on every start).
--
-- understanding_chunk_jobs: one row per stretch of an agent's context to describe. The agent's llm
-- node enqueues a chunk when the provider-reported input tokens grow by the threshold past the
-- previous cut, and the compact paths enqueue the segment's closing remainder; the agent-host
-- loop claims rows with FOR UPDATE SKIP LOCKED. (agent_id, compact_version, start_index,
-- end_index) identifies a chunk, so a re-enqueue is a no-op; boundary_checkpoint_id is set only on
-- a segment's closing chunk.

CREATE TABLE IF NOT EXISTS understanding_chunk_jobs (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL,
    compact_version INTEGER NOT NULL,
    start_index INTEGER NOT NULL,
    end_index INTEGER NOT NULL,
    end_msg_id TEXT NOT NULL,
    boundary_checkpoint_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS understanding_chunk_jobs_identity
    ON understanding_chunk_jobs (agent_id, compact_version, start_index, end_index);
CREATE INDEX IF NOT EXISTS understanding_chunk_jobs_live
    ON understanding_chunk_jobs (id) WHERE status IN ('pending', 'running');

COMMENT ON TABLE understanding_chunk_jobs IS
    'Chunk-triggered understanding queue: one row per context stretch to describe; claimed with SKIP LOCKED by the agent-host loop, result lands as a depth-1 understanding_nodes row.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT, UPDATE ON understanding_chunk_jobs TO ava_runner;
        GRANT USAGE, SELECT ON SEQUENCE understanding_chunk_jobs_id_seq TO ava_runner;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS understanding_chunk_calls (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL,
    agent_id BIGINT NOT NULL,
    attempt INTEGER NOT NULL,
    round INTEGER NOT NULL,
    model TEXT NOT NULL,
    instruction TEXT NOT NULL,
    prefix_len INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    content JSONB,
    tool_calls JSONB,
    additional_kwargs JSONB,
    usage_metadata JSONB,
    response_metadata JSONB,
    duration_ms DOUBLE PRECISION NOT NULL,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind TEXT NOT NULL DEFAULT 'leaf',
    problem TEXT
);
CREATE INDEX IF NOT EXISTS understanding_chunk_calls_job
    ON understanding_chunk_calls (job_id, attempt, round);
CREATE INDEX IF NOT EXISTS understanding_chunk_calls_agent
    ON understanding_chunk_calls (agent_id, created_at);

COMMENT ON TABLE understanding_chunk_calls IS
    'Raw record of each provider call of chunk-triggered understanding (instruction, reply as returned, usage, timing, error); failed calls included.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT INSERT ON understanding_chunk_calls TO ava_runner;
        GRANT USAGE, SELECT ON SEQUENCE understanding_chunk_calls_id_seq TO ava_runner;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS understanding_group_state (
    agent_id BIGINT NOT NULL,
    level INTEGER NOT NULL,
    last_checked_open INTEGER NOT NULL DEFAULT 0,
    claimed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, level)
);

COMMENT ON TABLE understanding_group_state IS
    'Upper-level grouping cursor per (agent, level): open-node count at the last check, and the lease of the runner checking it.';

CREATE TABLE IF NOT EXISTS understanding_group_calls (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL,
    level INTEGER NOT NULL,
    check_key TEXT NOT NULL,
    round INTEGER NOT NULL,
    model TEXT NOT NULL,
    mode TEXT,
    open_ids BIGINT[] NOT NULL,
    request TEXT NOT NULL,
    content JSONB,
    additional_kwargs JSONB,
    usage_metadata JSONB,
    response_metadata JSONB,
    duration_ms DOUBLE PRECISION NOT NULL,
    problem TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS understanding_group_calls_check
    ON understanding_group_calls (check_key, round);
CREATE INDEX IF NOT EXISTS understanding_group_calls_agent
    ON understanding_group_calls (agent_id, created_at);

COMMENT ON TABLE understanding_group_calls IS
    'Raw record of each provider call of an upper-level grouping check (request, reply as returned, usage, timing, refusal reason, error); failed calls included.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT, UPDATE ON understanding_group_state TO ava_runner;
        GRANT INSERT ON understanding_group_calls TO ava_runner;
        GRANT USAGE, SELECT ON SEQUENCE understanding_group_calls_id_seq TO ava_runner;
    END IF;
END $$;
