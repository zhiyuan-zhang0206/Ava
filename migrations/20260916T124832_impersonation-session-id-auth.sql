-- Controller authority is the session id plus caller attestation: the controller
-- token is no longer minted or checked (user ruling 2026-09-16). Expand step —
-- stop requiring the legacy column; keeping it until the contract phase lets any
-- one upgrade stay reversible.
ALTER TABLE agent_impersonations ALTER COLUMN token_hash DROP NOT NULL;
