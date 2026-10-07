-- Retire the compact-driven hierarchy worker (the old understanding-tree builder). The code is gone
-- and nothing reads or writes its tables any more, but the tables themselves are dropped by a LATER
-- migration, after this release has been deployed everywhere (expand-contract): until then a
-- rollback to the previous release still finds `hierarchy_jobs`, `hierarchy_worker_state` and
-- `hierarchy_worker_breaker` and starts; its worker is shipped dark, so it builds nothing.
--
-- What this migration cleans up is data, not schema:
-- - the built-in `hierarchy-worker` schedule row: the manifest no longer carries it and its stored
--   script imports the deleted worker, so the row would crash-loop. A previous-release gateway that
--   boots after this runs (a rollback) provisions its own manifest's row again; that old script
--   works with that release's code, which is what a rollback means.
-- - the old tree's nodes: the chunk-triggered pipeline writes `chunk-*` / `group-*` engine versions
--   only, and the old cuts (the token-budget seal cascade) do not line up with its groups of
--   message units. A rollback finds the old tree empty; its worker is off, so it stays so.
-- Idempotent.
DELETE FROM schedules WHERE name = 'hierarchy-worker';

DELETE FROM understanding_nodes
 WHERE engine_version NOT LIKE 'chunk-%' AND engine_version NOT LIKE 'group-%';

COMMENT ON TABLE understanding_nodes IS
    'The understanding tree: one text per (agent_id, depth, message span) — depth 1 written by a chunk call, each level above by an upper-level grouping check; hashes make rewrites idempotent.';
