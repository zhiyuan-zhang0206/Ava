DROP TRIGGER IF EXISTS agents_meta_resurrect_fold_spawner ON agents_meta;
DROP TRIGGER IF EXISTS agents_meta_terminate_fold_live_child_spawners ON agents_meta;
DROP FUNCTION IF EXISTS fold_live_child_spawners_on_terminate();
DROP FUNCTION IF EXISTS fold_resurrected_agent_spawner();
DROP FUNCTION IF EXISTS nearest_living_agent_spawner(TEXT);
