-- Controller authority is the session id plus caller attestation: the controller
-- token is no longer minted or checked (user ruling 2026-09-16). Expand step —
-- stop requiring the legacy column; keeping it until the contract phase lets any
-- one upgrade stay reversible.
-- Transition note: rows recorded before this change carry the legacy raw create_time;
-- verification matches those with a 2.0 s tolerance only. Extreme clock drift (e.g.
-- WSL legacy rows) may fail closed once until the row rotates — fail-closed by design.
ALTER TABLE agent_impersonations ALTER COLUMN token_hash DROP NOT NULL;
