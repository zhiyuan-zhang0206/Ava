-- Manual build of the understanding tree, session by session (gateway `POST
-- /api/agents/{id}/understanding/build`). All idempotent (IF NOT EXISTS): db/schema.sql already
-- holds these for a fresh database, and the ava_runner grants are gated on the role's existence
-- (base/cluster/authority/groups.py grants the same surface at birth and on every start).
--
-- understanding_rebuilds: the queue of upper-level rebuilds. A build enqueues one per agent (builds
-- of one agent merge into the agent's pending row); the agent-host loop claims it once the agent
-- has no chunk job pending or running, drops every node above level 1 and the grouping cursor, and
-- replays the leaves in message order (base/agents/history/hierarchy/rebuild.py).
--
-- understanding_builds: one row per build request that was executed (not a dry run): the sessions
-- asked for, the chunk jobs it enqueued or merged into ([{job_id, session}]) and its rebuild, so
-- GET .../understanding/builds/{id} can report progress and cost.

CREATE TABLE IF NOT EXISTS understanding_rebuilds (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    leaves INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error TEXT
);
CREATE INDEX IF NOT EXISTS understanding_rebuilds_live
    ON understanding_rebuilds (agent_id, id) WHERE status IN ('pending', 'running');

COMMENT ON TABLE understanding_rebuilds IS
    'Queue of upper-level rebuilds of the understanding tree: one pending row per agent absorbs concurrent builds; claimed by the agent-host loop once the agent has no live chunk job.';

CREATE TABLE IF NOT EXISTS understanding_builds (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL,
    sessions INTEGER[] NOT NULL,
    jobs JSONB NOT NULL,
    rebuild_id BIGINT NOT NULL REFERENCES understanding_rebuilds (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS understanding_builds_agent
    ON understanding_builds (agent_id, id);

COMMENT ON TABLE understanding_builds IS
    'One executed manual build of the understanding tree: the sessions asked for, the chunk jobs it owns ([{job_id, session}]) and its upper-level rebuild.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, UPDATE ON understanding_rebuilds TO ava_runner;
        GRANT DELETE ON understanding_group_state TO ava_runner;
    END IF;
END $$;
