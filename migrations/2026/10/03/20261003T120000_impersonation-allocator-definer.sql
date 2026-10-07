-- The impersonation session allocator bumps agents.impersonation_index from a
-- BEFORE INSERT trigger on agent_impersonations, which `ava impersonate request`
-- inserts as the runner role. As SECURITY INVOKER that made the runner's
-- table-wide UPDATE on agents load-bearing. DEFINER (the convention of every
-- other impersonation function) lets the refresh revoke that grant: agents is
-- gateway-written only.
CREATE OR REPLACE FUNCTION allocate_impersonation_session() RETURNS trigger AS $$
BEGIN
    PERFORM id FROM agents_meta WHERE id=NEW.agent_id FOR UPDATE;
    UPDATE agents SET impersonation_index=impersonation_index+1 WHERE id=NEW.agent_id
        RETURNING impersonation_index-1 INTO NEW.session_id;
    IF NEW.name='' THEN NEW.name='Session ' || NEW.session_id; END IF;
    IF NEW.executor_name='' THEN NEW.executor_name=NEW.source; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public;
