ALTER TABLE schedules ADD COLUMN IF NOT EXISTS desired_revision BIGINT NOT NULL DEFAULT 0;
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS applied_revision BIGINT NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS schedule_operation_receipts (
    operation_key TEXT PRIMARY KEY,
    request JSONB NOT NULL,
    response JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE schedule_operation_receipts IS
    'Immutable schedule mutation receipts, committed with desired state and sync work; retained without expiry so old retries cannot become new restart intents.';
COMMENT ON TABLE schedule_sync_requests IS
    'Pending desired-state convergence. Matching revision/session provenance is adopted. The consumer deletes a row after convergence, only if requested_at is unchanged.';
