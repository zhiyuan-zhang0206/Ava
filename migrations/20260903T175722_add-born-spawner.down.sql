DROP TRIGGER IF EXISTS agents_meta_born_spawner_append_only ON agents_meta;
DROP FUNCTION IF EXISTS reject_agents_meta_born_spawner_update();
ALTER TABLE agents_meta DROP COLUMN born_spawner;
