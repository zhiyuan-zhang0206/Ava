-- A rollback must never discard the permanent history introduced by this schema.
DO $$ BEGIN
    IF to_regclass('agent_impersonation_entries') IS NULL THEN RETURN; END IF;
    IF EXISTS (SELECT 1 FROM agent_impersonation_entries)
       OR EXISTS (SELECT 1 FROM agent_impersonations WHERE automatic) THEN
        RAISE EXCEPTION 'Cannot roll back permanent impersonation history';
    END IF;
    DROP TRIGGER agent_impersonations_allocate ON agent_impersonations;
    DROP FUNCTION allocate_impersonation_session();
    DROP TRIGGER inbound_messages_impersonation_history ON inbound_messages;
    DROP FUNCTION record_impersonation_inbound();
    DROP TRIGGER agent_impersonations_lifecycle ON agent_impersonations;
    DROP FUNCTION record_impersonation_lifecycle();
    DROP TABLE agent_impersonation_entries;
    DROP TRIGGER agent_impersonations_preserve_history ON agent_impersonations;
    DROP FUNCTION preserve_impersonation_history();
    ALTER TABLE agent_impersonation_messages DROP CONSTRAINT agent_impersonation_messages_lease_id_fkey;
    ALTER TABLE agent_impersonations DROP CONSTRAINT agent_impersonations_pkey;
    ALTER TABLE agent_impersonations DROP CONSTRAINT agent_impersonations_id_key;
    ALTER TABLE agent_impersonations ADD PRIMARY KEY(id);
    ALTER TABLE agent_impersonation_messages ADD FOREIGN KEY(lease_id)
        REFERENCES agent_impersonations(id) ON DELETE CASCADE;
    DROP INDEX agent_impersonations_one_open;
    DROP INDEX agent_impersonations_events_pending;
    CREATE UNIQUE INDEX agent_impersonations_one_open ON agent_impersonations(agent_id)
    WHERE status IN ('requested','accepted','active') OR delta_version>applied_version;
    ALTER TABLE agent_impersonations
        DROP COLUMN session_id, DROP COLUMN name, DROP COLUMN executor_name,
        DROP COLUMN process_metadata, DROP COLUMN automatic, DROP COLUMN summary,
        DROP COLUMN handoff_document, DROP COLUMN handoff_path, DROP COLUMN handoff_applied_at,
        DROP COLUMN next_entry, DROP COLUMN events_cursor,
    DROP COLUMN events_next_read_at, DROP COLUMN events_completed_at;
    ALTER TABLE agents DROP COLUMN impersonation_index;
END $$;
