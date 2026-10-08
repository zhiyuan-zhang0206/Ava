CREATE TABLE task_update_receipts (
    actor_agent_id BIGINT NOT NULL CHECK (actor_agent_id > 0),
    task_id BIGINT NOT NULL CHECK (task_id > 0),
    operation_key TEXT NOT NULL CHECK (length(operation_key) BETWEEN 1 AND 128),
    request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (actor_agent_id, task_id, operation_key)
);

COMMENT ON TABLE task_update_receipts IS
    'Immutable SDK task update/log commit tombstones; agent provenance scope, not authentication or notification execution receipts.';
