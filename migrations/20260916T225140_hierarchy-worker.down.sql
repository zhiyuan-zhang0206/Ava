-- Down: drop the P2b worker tables. No other object references them (no FKs,
-- no grants beyond the owner's).
DROP TABLE IF EXISTS hierarchy_worker_state;
DROP TABLE IF EXISTS hierarchy_jobs;
