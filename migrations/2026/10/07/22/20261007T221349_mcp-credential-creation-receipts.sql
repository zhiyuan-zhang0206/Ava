CREATE TABLE mcp_credential_creation_receipts (
    operation_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    acceptance JSONB NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE mcp_credential_creation_receipts IS
    'Original credential creation metadata only. No token or credential hash, cleanup FK, expiry, or implicit token reissue.';
