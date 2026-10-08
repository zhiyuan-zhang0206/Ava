CREATE TABLE agent_upload_batches (
    operation_key text PRIMARY KEY,
    agent_id bigint NOT NULL,
    request_fingerprint text NOT NULL,
    manifest jsonb NOT NULL,
    receipt jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    ready_at timestamptz,
    CHECK ((receipt IS NULL) = (ready_at IS NULL))
);
CREATE INDEX agent_upload_batches_receiving_agent_idx
    ON agent_upload_batches (agent_id) WHERE receipt IS NULL;
COMMENT ON TABLE agent_upload_batches IS
    'Silent upload identities; receiving manifests reserve quota without expiry, ready receipts replay acceptance.';
