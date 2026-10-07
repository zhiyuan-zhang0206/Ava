-- Acceptance snapshots survive queue retention without pinning its rows.
CREATE TABLE agent_control_receipts (
    path TEXT NOT NULL,
    operation_key TEXT NOT NULL CHECK (char_length(operation_key) BETWEEN 1 AND 128),
    agent_id BIGINT NOT NULL CHECK (agent_id > 0),
    kind TEXT NOT NULL CHECK (kind IN ('cancel','compact_request')),
    result TEXT NOT NULL CHECK (result IN ('enqueued','already_terminated')),
    inbound_id BIGINT CHECK (inbound_id > 0),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (path, operation_key),
    CONSTRAINT agent_control_receipt_shape_check CHECK (
        (result = 'enqueued' AND inbound_id IS NOT NULL)
        OR (result = 'already_terminated' AND kind = 'cancel' AND inbound_id IS NULL)
    )
);
COMMENT ON TABLE agent_control_receipts IS
    'Immutable cancel/compact acceptance snapshots, not execution receipts or a queue; no expiry or foreign key may turn a retained retry into fresh work or pin queue retention.';
