CREATE TABLE native_compact_observations (
    id UUID PRIMARY KEY,
    agent_id BIGINT NOT NULL CHECK (agent_id > 0),
    work_id UUID NOT NULL,
    target JSONB NOT NULL CHECK (jsonb_typeof(target) = 'object'),
    resources JSONB NOT NULL CHECK (jsonb_typeof(resources) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX native_compact_observed_agent_idx ON native_compact_observations(agent_id, created_at DESC, id);

CREATE TABLE native_compact_commands (
    id UUID PRIMARY KEY,
    agent_id BIGINT NOT NULL CHECK (agent_id > 0),
    operation_key TEXT NOT NULL UNIQUE,
    request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    acceptance JSONB NOT NULL CHECK (jsonb_typeof(acceptance) = 'object'),
    outcome TEXT NOT NULL DEFAULT 'accepted'
        CHECK (outcome IN ('accepted','prepared','applying','applied','noop','rejected','uncertain')),
    reason TEXT CHECK (length(reason) <= 128),
    attempt_id UUID,
    attempt_provider TEXT CHECK (length(attempt_provider)>0),
    execution JSONB CHECK (jsonb_typeof(execution) = 'object'),
    result JSONB CHECK (jsonb_typeof(result) = 'object'),
    checkpoint_id TEXT CHECK (length(checkpoint_id)>0),
    recovery_checkpoint_id TEXT CHECK (length(recovery_checkpoint_id)>0),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at TIMESTAMPTZ,
    CHECK ((attempt_id IS NULL) = (execution IS NULL)),
    CHECK ((attempt_id IS NULL) = (attempt_provider IS NULL)),
    CHECK (result IS NULL OR attempt_id IS NOT NULL),
    CHECK (outcome NOT IN ('prepared','applying','applied') OR result IS NOT NULL),
    CHECK (outcome <> 'applied' OR checkpoint_id IS NOT NULL),
    CHECK (outcome <> 'noop' OR (attempt_id IS NULL AND result IS NULL AND released_at IS NOT NULL)),
    CHECK (outcome <> 'rejected' OR attempt_id IS NOT NULL OR released_at IS NOT NULL),
    CHECK (outcome <> 'uncertain' OR reason IS NOT NULL),
    CHECK (outcome <> 'uncertain' OR released_at IS NULL OR recovery_checkpoint_id IS NOT NULL),
    CHECK (checkpoint_id IS NULL OR outcome = 'applied'),
    CHECK (recovery_checkpoint_id IS NULL OR (outcome = 'uncertain' AND released_at IS NOT NULL))
);
CREATE UNIQUE INDEX native_compact_pending_agent_idx ON native_compact_commands(agent_id)
    WHERE released_at IS NULL;

COMMENT ON TABLE native_compact_observations IS
    'Actual new-host compact source observations after ended work and resource closure. No inferred capability or TTL.';
COMMENT ON TABLE native_compact_commands IS
    'Guarded manual compaction intent, original generation attempt, durable result and cold application proof. Retained without cleanup FK.';
