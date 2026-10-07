-- Retire the compact-driven hierarchy worker (the old understanding-tree builder): its job queue,
-- scan cursor and regeneration breaker are dropped, the built-in `hierarchy-worker` schedule row is
-- removed (the manifest no longer carries it; its stored script imports the deleted worker), and the
-- nodes the old builder wrote are deleted: the chunk-triggered pipeline writes `chunk-*` / `group-*`
-- engine versions only, and the old cuts (token-budget seal cascade) do not line up with its
-- message-unit groups. Nothing reads these objects any more. Idempotent.
DROP TABLE IF EXISTS hierarchy_jobs;
DROP TABLE IF EXISTS hierarchy_worker_state;
DROP TABLE IF EXISTS hierarchy_worker_breaker;

DELETE FROM schedules WHERE name = 'hierarchy-worker';

DELETE FROM understanding_nodes
 WHERE engine_version NOT LIKE 'chunk-%' AND engine_version NOT LIKE 'group-%';

COMMENT ON TABLE understanding_nodes IS
    'The understanding tree: one text per (agent_id, depth, message span) — depth 1 written by a chunk call, each level above by an upper-level grouping check; hashes make rewrites idempotent.';
