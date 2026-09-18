-- Reverse of impersonation-relay-minted: drop the mint mark.

ALTER TABLE agent_impersonations
    DROP CONSTRAINT IF EXISTS agent_impersonations_relay_minted_pair_check,
    DROP COLUMN IF EXISTS relay_minted_at,
    DROP COLUMN IF EXISTS relay_minted_generation,
    DROP COLUMN IF EXISTS relay_minted_owner;
