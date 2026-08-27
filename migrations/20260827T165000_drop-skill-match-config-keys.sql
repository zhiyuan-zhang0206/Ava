-- drop-skill-match-config-keys: strip the deleted skill semantic matcher's
-- per-agent config keys from every persisted agent-configuration home.
--
-- The skill semantic matcher was removed per user ruling 2026-08-27
-- (274ab9287); a later merge (b78ca4ca1, Task #1820 rollout telemetry) was
-- based on a pre-deletion branch and accidentally restored the feature --
-- fields, module, and docs -- on main, where it was then deployed. Stored
-- per-agent overlays that carry the four keys would fail agent boot once the
-- fields are unregistered again: the overlay resolver rejects unknown keys
-- (shared/plugin_config_registry.py resolve_overlay_targets raises
-- InvalidConfigOverlay). This migration deletes the keys wherever agent
-- configuration is persisted:
--   * agents_meta.config_overlay -- per-agent spawn-time override map
--   * agents_meta.birth_config   -- frozen-field birth snapshot (defensive:
--                                   the keys were lifecycle "live", never
--                                   frozen, so this is normally empty of them)
--   * agent_presets.config       -- preset templates applied at spawn
-- Historical inbound payloads are audit records and are deliberately left
-- untouched: a restart inbound's config_overlay is consumed at wake time,
-- never replayed.
--
-- JSONB '-' removes a key; '?|' tests key existence so NULL columns and rows
-- without the keys are skipped untouched.

UPDATE agents_meta
SET config_overlay = config_overlay
        - 'skill_match_enabled'
        - 'skill_match_top_k'
        - 'skill_match_min_score'
        - 'skill_match_budget_ms'
WHERE config_overlay ?| ARRAY[
    'skill_match_enabled',
    'skill_match_top_k',
    'skill_match_min_score',
    'skill_match_budget_ms'
];

UPDATE agents_meta
SET birth_config = birth_config
        - 'skill_match_enabled'
        - 'skill_match_top_k'
        - 'skill_match_min_score'
        - 'skill_match_budget_ms'
WHERE birth_config ?| ARRAY[
    'skill_match_enabled',
    'skill_match_top_k',
    'skill_match_min_score',
    'skill_match_budget_ms'
];

UPDATE agent_presets
SET config = config
        - 'skill_match_enabled'
        - 'skill_match_top_k'
        - 'skill_match_min_score'
        - 'skill_match_budget_ms'
WHERE config ?| ARRAY[
    'skill_match_enabled',
    'skill_match_top_k',
    'skill_match_min_score',
    'skill_match_budget_ms'
];
