-- Contract step of the hierarchy worker retirement (the expand step is
-- 20261007T045502_retire-hierarchy-worker): the compact-driven worker's three tables are dropped
-- now that no code reads or writes them. Their indexes, primary-key sequence, constraints and
-- the ava_runner INSERT grant on hierarchy_jobs go with them. Idempotent.
DROP TABLE IF EXISTS hierarchy_jobs;
DROP TABLE IF EXISTS hierarchy_worker_state;
DROP TABLE IF EXISTS hierarchy_worker_breaker;
