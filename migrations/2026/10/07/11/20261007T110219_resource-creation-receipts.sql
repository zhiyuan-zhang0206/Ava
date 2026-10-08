CREATE TABLE IF NOT EXISTS resource_creation_receipts (
    operation_key TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    resource_id BIGINT,
    resource_created_at TIMESTAMPTZ,
    resource_updated_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE resource_creation_receipts IS
    'Preset/schedule creation acceptance identities. Only request fingerprint and original resource identity/timestamps are retained, without raw request/config/secret copies or expiry.';
