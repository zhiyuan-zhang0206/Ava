-- Never discard accepted commands during rollback. Old code cannot replay them.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM agents_meta WHERE lifecycle_command_id IS NOT NULL)
        OR EXISTS (SELECT 1 FROM inbound_messages WHERE target_generation IS NOT NULL) THEN
        RAISE EXCEPTION 'archive lifecycle receipts and settle accepted commands before rollback';
    END IF;
END $$;
ALTER TABLE agents_meta DROP CONSTRAINT agents_meta_lifecycle_command_fk;
ALTER TABLE agents_meta DROP COLUMN lifecycle_command_id;
ALTER TABLE inbound_messages DROP CONSTRAINT inbound_lifecycle_target_check;
ALTER TABLE inbound_messages DROP CONSTRAINT inbound_agent_command_unique;
ALTER TABLE inbound_messages DROP COLUMN target_generation, DROP COLUMN target_owner,
    DROP COLUMN applied_at, DROP COLUMN observed_at;
