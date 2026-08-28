-- Reverse: drop the pause-history table. Nothing else depends on it.

DROP TABLE IF EXISTS heartbeat_pause_log;
