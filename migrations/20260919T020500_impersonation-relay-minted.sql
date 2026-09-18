-- Durable relay mint mark (task #3998): who minted the lease's relay
-- credential and when. The native supervisor reads it after a restart to tell
-- "minted by an earlier incarnation" from "never minted", so a stale heartbeat
-- outside the fresh-start window always stops the lease instead of silently
-- re-provisioning. No plaintext credential is stored -- the hash column
-- remains the only credential record.

ALTER TABLE agent_impersonations
    ADD COLUMN relay_minted_at TIMESTAMPTZ,
    ADD COLUMN relay_minted_generation UUID,
    ADD COLUMN relay_minted_owner UUID,
    ADD CONSTRAINT agent_impersonations_relay_minted_pair_check
        CHECK ((relay_minted_generation IS NULL) = (relay_minted_owner IS NULL));
