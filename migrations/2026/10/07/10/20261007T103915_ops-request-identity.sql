-- Preserve immutable request identity through ops response replay.
ALTER TABLE api_idempotency ADD COLUMN IF NOT EXISTS request_hash TEXT;
COMMENT ON COLUMN api_idempotency.request_hash IS
    'SHA-256 of the immutable ops kind/payload; NULL on legacy/HTTP records. Unknown identity fails closed on ops replay.';
