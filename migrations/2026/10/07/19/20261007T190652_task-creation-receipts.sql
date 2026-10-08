CREATE TABLE task_creation_receipts (
    actor_agent_id BIGINT NOT NULL CHECK (actor_agent_id > 0),
    operation_key TEXT NOT NULL CHECK (length(operation_key) BETWEEN 1 AND 128),
    request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (actor_agent_id, operation_key)
);
