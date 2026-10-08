CREATE TABLE task_assignment_receipts (
    operation_key TEXT PRIMARY KEY CHECK (char_length(operation_key) BETWEEN 1 AND 128),
    request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    result JSONB NOT NULL CHECK (
        jsonb_typeof(result) = 'object'
        AND result ?& ARRAY['task', 'agent_id', 'launch_attempt_id']
        AND jsonb_typeof(result->'task') = 'object'
        AND result->>'agent_id' ~ '^[1-9][0-9]*$'
        AND result->>'launch_attempt_id' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
    ),
    birth_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(birth_snapshot) = 'object'
        AND birth_snapshot ?& ARRAY['machine', 'config_overlay', 'birth_config', 'preset_name', 'launch_attempt_id']
        AND jsonb_typeof(birth_snapshot->'machine') = 'string'
        AND jsonb_typeof(birth_snapshot->'birth_config') = 'object'
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE task_assignment_receipts IS
    'Immutable principal-scoped compound acceptance tombstones; no FK or automatic expiry.';
