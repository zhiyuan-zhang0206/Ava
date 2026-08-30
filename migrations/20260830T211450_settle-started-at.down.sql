-- Reverse: drop the settle-start column. Nothing else depends on it.

ALTER TABLE deployment_state DROP COLUMN settle_started_at;
