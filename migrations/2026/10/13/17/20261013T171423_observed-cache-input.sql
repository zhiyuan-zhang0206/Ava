-- Null preserves the legacy denominator for already-collected observations.
-- New producers distinguish unmetered input (0) from reported cache usage.
ALTER TABLE agent_metric_observations
    ADD COLUMN IF NOT EXISTS tokens_cache_input BIGINT CHECK (tokens_cache_input >= 0);
ALTER TABLE agent_metric_days
    ADD COLUMN IF NOT EXISTS tokens_cache_input BIGINT CHECK (tokens_cache_input >= 0);
