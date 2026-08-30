-- Reverse: drop the settle-start column. Nothing else depends on it.
-- IF EXISTS mirrors the up direction's idempotence (the smoke replays both).

ALTER TABLE deployment_state DROP COLUMN IF EXISTS settle_started_at;
