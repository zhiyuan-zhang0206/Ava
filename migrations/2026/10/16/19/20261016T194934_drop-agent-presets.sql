-- Contract the preset mechanism (expand-contract: the code that resolved, stored
-- and displayed presets shipped removed first, and step 1 deleted the only live
-- instance). A preset was a named per-agent config template a spawn named inside
-- its config overlay; the user ruling of 2026-10-17 removed the mechanism
-- (task #5236). Nothing on origin/main reads or writes either object below.
--
--   * agent_presets           - the template table behind the REST CRUD
--                               (/api/presets), the CLI (`ava presets`) and the
--                               SDK (ava.agents.presets);
--   * agents_meta.preset_name - the display-only reference to the preset row a
--                               spawn resolved, copied verbatim by a fork.
--
-- task_assignment_receipts.birth_snapshot still requires a 'preset_name' key
-- (merged CHECK, 20261007T201556); new snapshots write it as JSON null, so that
-- constraint is left alone here.
--
-- Idempotent: every statement is IF EXISTS, so it replays on a baseline that
-- already lacks the objects.

DROP TABLE IF EXISTS agent_presets;
ALTER TABLE agents_meta DROP COLUMN IF EXISTS preset_name;

COMMENT ON TABLE resource_creation_receipts IS
    'Schedule creation acceptance identities. Only request fingerprint and original resource identity/timestamps are retained, without raw request/config/secret copies or expiry.';
