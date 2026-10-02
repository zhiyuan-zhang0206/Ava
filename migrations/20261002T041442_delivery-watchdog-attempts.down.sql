-- Reverse: drop the attempt state. The loops then start with empty cooldowns,
-- which is what a restart did before the table existed. IF EXISTS so a
-- repeated / standalone rollback is a no-op.
DROP TABLE IF EXISTS delivery_watchdog_attempts;
