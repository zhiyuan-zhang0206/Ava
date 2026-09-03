ALTER TABLE agents_meta ADD COLUMN born_spawner TEXT;

COMMENT ON COLUMN agents_meta.born_spawner IS
'Birth-time original spawner. Immutable and never rewritten by folding; forks use agent:<fork_source>, plain spawns use the birth trigger, and backfilled rows are best-known.';

UPDATE agents_meta AS agent
SET born_spawner = CASE
    WHEN agent.fork_source_agent_id IS NOT NULL
        THEN 'agent:' || agent.fork_source_agent_id::TEXT
    ELSE COALESCE(
        (
            SELECT inbound.source
            FROM inbound_messages AS inbound
            WHERE inbound.agent_id = agent.id
              AND inbound.kind = 'chat'
              AND inbound.source ~ '^agent:[0-9]+$'
              AND inbound.created_at <= agent.spawned_at + interval '10 minutes'
            ORDER BY inbound.id
            LIMIT 1
        ),
        agent.spawner
    )
END
WHERE agent.born_spawner IS NULL;

CREATE OR REPLACE FUNCTION reject_agents_meta_born_spawner_update() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'agents_meta.born_spawner is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agents_meta_born_spawner_append_only
    BEFORE UPDATE OF born_spawner ON agents_meta
    FOR EACH ROW
    EXECUTE FUNCTION reject_agents_meta_born_spawner_update();
