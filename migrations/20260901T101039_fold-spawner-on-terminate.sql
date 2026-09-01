-- Keep every live agent's tree parent at its nearest living ancestor when an
-- intermediate agent terminates. Fork provenance stays in fork_source_agent_id.
CREATE OR REPLACE FUNCTION nearest_living_agent_spawner(initial_spawner TEXT) RETURNS TEXT AS $$
DECLARE
    ancestor_spawner TEXT := initial_spawner;
    parent_spawner TEXT;
    ancestor_id BIGINT;
    ancestor_status TEXT;
    hops INTEGER := 0;
BEGIN
    LOOP
        IF ancestor_spawner !~ '^agent:[0-9]+$' THEN
            RETURN ancestor_spawner;
        END IF;

        hops := hops + 1;
        IF hops > 32 THEN
            RETURN 'user';
        END IF;

        BEGIN
            ancestor_id := substring(ancestor_spawner FROM 7)::BIGINT;
        EXCEPTION
            WHEN numeric_value_out_of_range THEN
                RETURN 'user';
        END;

        SELECT spawner, status
          INTO parent_spawner, ancestor_status
          FROM agents_meta
         WHERE id = ancestor_id;

        IF NOT FOUND THEN
            RETURN 'user';
        END IF;

        IF ancestor_status IN ('running', 'idling', 'restarting') THEN
            RETURN ancestor_spawner;
        END IF;

        ancestor_spawner := parent_spawner;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fold_live_child_spawners_on_terminate() RETURNS TRIGGER AS $$
DECLARE
    ancestor_spawner TEXT := nearest_living_agent_spawner(NEW.spawner);
BEGIN

    UPDATE agents_meta
       SET spawner = ancestor_spawner
     WHERE spawner = 'agent:' || NEW.id::TEXT
       AND status IN ('running', 'idling', 'restarting');

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agents_meta_terminate_fold_live_child_spawners ON agents_meta;

CREATE TRIGGER agents_meta_terminate_fold_live_child_spawners
    AFTER UPDATE OF status ON agents_meta
    FOR EACH ROW
    WHEN (NEW.status = 'terminated' AND OLD.status IS DISTINCT FROM 'terminated')
    EXECUTE FUNCTION fold_live_child_spawners_on_terminate();

CREATE OR REPLACE FUNCTION fold_resurrected_agent_spawner() RETURNS TRIGGER AS $$
BEGIN
    UPDATE agents_meta
       SET spawner = nearest_living_agent_spawner(NEW.spawner)
     WHERE id = NEW.id;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agents_meta_resurrect_fold_spawner ON agents_meta;

CREATE TRIGGER agents_meta_resurrect_fold_spawner
    AFTER UPDATE OF status ON agents_meta
    FOR EACH ROW
    WHEN (
        OLD.status = 'terminated'
        AND NEW.status IN ('running', 'idling', 'restarting')
    )
    EXECUTE FUNCTION fold_resurrected_agent_spawner();
