-- Reverses the expand step. Requires no NULL token_hash rows; rows written by
-- the new code carry NULL, so run this before the contract phase drops the
-- column entirely.
ALTER TABLE agent_impersonations ALTER COLUMN token_hash SET NOT NULL;
