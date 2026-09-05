-- Removing the gate while work or a handoff remains would allow two consumers.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM agent_impersonations p
               LEFT JOIN inbound_messages i ON i.id = p.summary_inbound_id
               WHERE p.status IN ('requested', 'accepted', 'active')
                  OR p.delta_version > p.applied_version
                  OR i.status <> 'done') THEN
        RAISE EXCEPTION 'Finish impersonations and consume their handoffs before rollback';
    END IF;
END $$;
DROP TRIGGER IF EXISTS agents_meta_revoke_impersonation ON agents_meta;
DROP FUNCTION IF EXISTS revoke_terminated_impersonation();
DROP TABLE IF EXISTS agent_impersonation_messages;
DROP TABLE IF EXISTS agent_impersonations;
