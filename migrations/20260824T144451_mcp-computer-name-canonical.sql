-- Canonicalize the built-in computer-use MCP server's exposed name across
-- every persisted agent-config store. The server may appear as a key in a
-- nested MCP server map or as an exact `mcps.<server>` capability reference.
--
-- The recursive helper changes an object key only when its value looks like an
-- MCP server spec (and rewrites a legacy `"shared": "<old>"` value inside a
-- spec the same way). That avoids rewriting unrelated config objects which
-- happen to use the same noun. If both spellings are present, the canonical entry wins
-- and the legacy entry is removed. Every UPDATE is guarded by an actual JSONB
-- difference, so a repeated run is a no-op.
--
-- Backup: run scripts/backup-20260824T144451-mcp-computer-name.sql first.

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
    preset.config, 'computer', 'computer_use'
)
WHERE EXISTS (
    SELECT 1
    WHERE pg_temp._rewrite_mcp_server_refs(
        preset.config, 'computer', 'computer_use'
    ) IS DISTINCT FROM preset.config
);

UPDATE agents_meta AS agent
SET config_overlay = pg_temp._rewrite_mcp_server_refs(
    agent.config_overlay, 'computer', 'computer_use'
)
WHERE EXISTS (
    SELECT 1
    WHERE pg_temp._rewrite_mcp_server_refs(
        agent.config_overlay, 'computer', 'computer_use'
    ) IS DISTINCT FROM agent.config_overlay
);

UPDATE agents_meta AS agent
SET birth_config = pg_temp._rewrite_mcp_server_refs(
    agent.birth_config, 'computer', 'computer_use'
)
WHERE EXISTS (
    SELECT 1
    WHERE pg_temp._rewrite_mcp_server_refs(
        agent.birth_config, 'computer', 'computer_use'
    ) IS DISTINCT FROM agent.birth_config
);
