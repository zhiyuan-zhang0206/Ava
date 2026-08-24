-- Backup candidates for 20260824T144451_mcp-computer-name-canonical.
-- Run before the migration on the target database. The text predicates are a
-- deliberate safe superset of the migration's stricter server-spec check, so
-- every affected row is captured even if its surrounding JSON shape varies.

DROP TABLE IF EXISTS _mig_backup_20260824_mcp_name_agent_presets;
DROP TABLE IF EXISTS _mig_backup_20260824_mcp_name_agents_meta;

CREATE TABLE _mig_backup_20260824_mcp_name_agent_presets AS
SELECT id, config
FROM agent_presets
WHERE config::text ~ '"computer"\s*:\s*\{'
   OR config::text ~ '"shared"\s*:\s*"computer"'
   OR config::text LIKE ('%"mcps.' || 'computer"%');

CREATE TABLE _mig_backup_20260824_mcp_name_agents_meta AS
SELECT id, config_overlay, birth_config
FROM agents_meta
WHERE config_overlay::text ~ '"computer"\s*:\s*\{'
   OR config_overlay::text ~ '"shared"\s*:\s*"computer"'
   OR config_overlay::text LIKE ('%"mcps.' || 'computer"%')
   OR birth_config::text ~ '"computer"\s*:\s*\{'
   OR birth_config::text ~ '"shared"\s*:\s*"computer"'
   OR birth_config::text LIKE ('%"mcps.' || 'computer"%');

-- Manual restoration after a bad run:
-- UPDATE agent_presets AS p
-- SET config = b.config
-- FROM _mig_backup_20260824_mcp_name_agent_presets AS b
-- WHERE p.id = b.id;
--
-- UPDATE agents_meta AS a
-- SET config_overlay = b.config_overlay,
--     birth_config = b.birth_config
-- FROM _mig_backup_20260824_mcp_name_agents_meta AS b
-- WHERE a.id = b.id;
