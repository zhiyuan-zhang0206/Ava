ALTER TABLE agents_meta ADD COLUMN creation_key TEXT;
ALTER TABLE agents_meta ADD COLUMN creation_request_hash TEXT;
ALTER TABLE agents_meta ADD CONSTRAINT agents_meta_creation_identity_check CHECK (
    (creation_key IS NULL AND creation_request_hash IS NULL) OR
    (creation_key IS NOT NULL AND char_length(creation_key) BETWEEN 1 AND 128
     AND creation_request_hash IS NOT NULL AND creation_request_hash ~ '^[0-9a-f]{64}$')
);
CREATE UNIQUE INDEX agents_meta_creation_key ON agents_meta (creation_key)
    WHERE creation_key IS NOT NULL;
COMMENT ON COLUMN agents_meta.creation_key IS
    'Immutable creation intent; retained with agent identity so lost-response retries cannot create another agent.';
