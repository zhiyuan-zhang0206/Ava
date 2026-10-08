CREATE TABLE task_patch_receipts (
    path TEXT NOT NULL,
    operation_key TEXT NOT NULL CHECK (length(operation_key) BETWEEN 1 AND 128),
    request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (path, operation_key)
);

COMMENT ON TABLE task_patch_receipts IS
    'Immutable task PATCH acceptance snapshots; retained independently of tasks and notifications.';
