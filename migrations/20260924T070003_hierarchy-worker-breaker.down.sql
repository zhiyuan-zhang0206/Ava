-- Reverse: drop the breaker. IF EXISTS so a repeated / standalone rollback is a no-op.
DROP TABLE IF EXISTS hierarchy_worker_breaker;
