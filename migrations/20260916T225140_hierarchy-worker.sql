-- Hierarchy worker (task #3704, P2b): the compact-driven understanding-tree
-- build queue and its per-agent scan cursor.
--
-- hierarchy_jobs — one row per execution attempt of one agent build. The
-- worker (a gateway-hosted resident schedule, services/hierarchy_worker/)
-- enqueues on new compact boundaries, claims atomically, and runs the build
-- in a child process; a crash/kill leaves a row the next pass recovers, and
-- the build itself is idempotent (its generation reuse cache lives in
-- understanding_nodes), so a retry resumes from the hash cache and never
-- redoes completed nodes. `skipped` counts nodes a run's time budget left
-- ungenerated: the next job replays the (pure, cheap) seal cascade, skips
-- everything already materialized and continues there — the zero-loss
-- continuation invariant (review 3187).
--
-- The live unique index is the enqueue de-dup: at most one pending/running
-- job per (agent, kind), whatever the scan races say. 'compact' is the only
-- kind today; P2c extends the vocabulary (day boundary / on-demand).
--
-- Writer: the worker runs on the gateway host as the cluster's main identity
-- (the data-plane owner). No ava_runner surface — no runner-side path writes
-- these tables (unlike understanding_nodes' first-run/regeneration path).
CREATE TABLE hierarchy_jobs (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('compact')),
    trigger_boundary TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed')),
    -- The build mode: first build of an agent seals its trailing stretch too
    -- (include_tail=true, same semantics as the manual first run); every
    -- later compact-driven pass leaves the tail pending for the next trigger.
    include_tail BOOLEAN NOT NULL,
    -- Production-version snapshot of the attempt (the nodes themselves each
    -- carry their own; this is the job-grain record for drift forensics).
    model TEXT,
    engine_version TEXT,
    prompt_version TEXT,
    -- Scope, recorded for cost observability (review 3187): stretches are the
    -- sealed trigger batches the run walked; nodes/generated/reused/failed
    -- partition the sealed tree (nodes = materialized rows written,
    -- generated = LLM-written, reused = input-hash cache hits, failed =
    -- nodes that got no text, skipped = ungenerated when the budget stopped).
    stretches INTEGER,
    nodes INTEGER,
    generated INTEGER,
    reused INTEGER,
    failed INTEGER,
    skipped INTEGER,
    -- Generation token sums (input sources / produced texts); the llm usage
    -- ledger carries the authoritative per-call numbers with
    -- usage_source='hierarchy.generate'.
    src_tokens BIGINT,
    out_tokens BIGINT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

-- Enqueue de-dup: at most one live job per (agent, kind).
CREATE UNIQUE INDEX hierarchy_jobs_live
    ON hierarchy_jobs (agent_id, kind) WHERE status IN ('pending', 'running');
-- The claim/recovery paths read pending/running rows.
CREATE INDEX hierarchy_jobs_live_status
    ON hierarchy_jobs (status) WHERE status IN ('pending', 'running');
-- Per-agent attempt history (scan reads the last finished attempt).
CREATE INDEX hierarchy_jobs_agent
    ON hierarchy_jobs (agent_id, id DESC);

COMMENT ON TABLE hierarchy_jobs IS
    'Understanding-tree build queue (task #3704 P2b): one row per execution '
    'attempt; hash-idempotent retries, crash-recoverable, scope+token stats.';

-- hierarchy_worker_state — the scan cursor: the newest compact boundary the
-- worker has FULLY covered for the agent. First sight of an agent records the
-- current newest boundary without building (the silent baseline: pre-go-live
-- history is not backfilled by the worker); a job advances it only when its
-- run skipped nothing. A plain BIGINT agent_id (no FK): history outlives
-- agent rows, mirrored from the checkpoint tables' own no-FK posture.
CREATE TABLE hierarchy_worker_state (
    agent_id BIGINT PRIMARY KEY,
    last_processed_boundary TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE hierarchy_worker_state IS
    'Per-agent scan cursor of the understanding-tree worker (task #3704 P2b): '
    'newest fully covered compact boundary; the row itself is the silent baseline.';
