-- Best-effort reverse of the exposed computer-use MCP server rename across
-- all three persisted agent-config stores. The same guarded recursive rule as
-- the up migration restores legacy server-map keys and exact capability
-- strings. If both spellings are present, the rollback target wins.

CREATE OR REPLACE FUNCTION pg_temp._rewrite_mcp_server_refs(
    document jsonb,
    old_name text,
    new_name text
) RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
DECLARE
    item_key text;
    item_value jsonb;
    rewritten jsonb := '{}'::jsonb;
    legacy_spec jsonb;
    has_legacy_spec boolean := false;
BEGIN
    CASE jsonb_typeof(document)
        WHEN 'object' THEN
            FOR item_key, item_value IN SELECT * FROM jsonb_each(document)
            LOOP
                IF item_key = 'shared' AND item_value = to_jsonb(old_name::text) THEN
                    rewritten := rewritten || jsonb_build_object(
                        item_key, to_jsonb(new_name::text)
                    );
                ELSIF item_key = old_name
                   AND jsonb_typeof(item_value) = 'object'
                   AND item_value ?| ARRAY[
                       'command', 'url', 'args', 'env', 'shared', 'requires',
                       'description', 'oauth', 'headers'
                   ]
                THEN
                    legacy_spec := pg_temp._rewrite_mcp_server_refs(
                        item_value, old_name, new_name
                    );
                    has_legacy_spec := true;
                ELSE
                    rewritten := rewritten || jsonb_build_object(
                        item_key,
                        pg_temp._rewrite_mcp_server_refs(item_value, old_name, new_name)
                    );
                END IF;
            END LOOP;

            IF has_legacy_spec AND NOT rewritten ? new_name THEN
                rewritten := rewritten || jsonb_build_object(new_name, legacy_spec);
            END IF;
            RETURN rewritten;

        WHEN 'array' THEN
            SELECT COALESCE(
                jsonb_agg(pg_temp._rewrite_mcp_server_refs(value, old_name, new_name)),
                '[]'::jsonb
            )
            INTO rewritten
            FROM jsonb_array_elements(document);
            RETURN rewritten;

        WHEN 'string' THEN
            IF document = to_jsonb(('mcps.' || old_name)::text) THEN
                RETURN to_jsonb(('mcps.' || new_name)::text);
            END IF;
            RETURN document;

        ELSE
            RETURN document;
    END CASE;
END;
$$;

UPDATE agent_presets AS preset
SET config = pg_temp._rewrite_mcp_server_refs(
    preset.config, 'computer_use', 'computer'
)
WHERE EXISTS (
    SELECT 1
    WHERE pg_temp._rewrite_mcp_server_refs(
        preset.config, 'computer_use', 'computer'
    ) IS DISTINCT FROM preset.config
);

UPDATE agents_meta AS agent
SET config_overlay = pg_temp._rewrite_mcp_server_refs(
    agent.config_overlay, 'computer_use', 'computer'
)
WHERE EXISTS (
    SELECT 1
    WHERE pg_temp._rewrite_mcp_server_refs(
        agent.config_overlay, 'computer_use', 'computer'
    ) IS DISTINCT FROM agent.config_overlay
);

UPDATE agents_meta AS agent
SET birth_config = pg_temp._rewrite_mcp_server_refs(
    agent.birth_config, 'computer_use', 'computer'
)
WHERE EXISTS (
    SELECT 1
    WHERE pg_temp._rewrite_mcp_server_refs(
        agent.birth_config, 'computer_use', 'computer'
    ) IS DISTINCT FROM agent.birth_config
);
