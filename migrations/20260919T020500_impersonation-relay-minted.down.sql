-- Reverse of impersonation-relay-minted: drop the mint mark.

ALTER TABLE agent_impersonations
    DROP CONSTRAINT agent_impersonations_relay_minted_pair_check,
    DROP COLUMN relay_minted_at,
    DROP COLUMN relay_minted_generation,
    DROP COLUMN relay_minted_owner;
