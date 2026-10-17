# Preset mechanism removed

## Context

User ruling, 2026-10-17 (direct, task #5236): delete the preset mechanism at
every layer — gateway routes and the spawn/creation chain, the `meiva presets`
CLI, the SDK surface (`meiva.agents.presets`, the `spawn(preset=...)`
parameter), the Web UI (spawn picker, console Presets page, inspector diff),
the IM bridge spawn menu, the database objects, documentation and tests.

The mechanism had outlived its job: model choice and per-agent config are
already carried by `config_overlay` and the per-model registry, and the
guide's model skill owns the standing policy for picking one. This record
supersedes in part two earlier records:

- [2026-09-10-preset-in-config-overlay-fork-cache.md](2026-09-10-preset-in-config-overlay-fork-cache.md)
  defined the preset's config-overlay fold, the `agents_meta.preset_name`
  column and the inspect diff. Its fork config-stability and cache-prefix
  rules — the bulk of that record — remain current.
- [2026-08-01-spawn-picker-concrete-effort-defaults.md](../../frontend/ui/2026-08-01-spawn-picker-concrete-effort-defaults.md)
  Decision 3 (a picked preset overrides the model pickers) no longer applies;
  Decisions 1–2 (concrete per-model effort defaults) remain.

## Decision

- **No tombstone.** There is no compatibility route or wire field: a client
  that still sends the old top-level `preset` spawn field is rejected in the
  ordinary way for unknown input, and a leftover `config_overlay["preset"]`
  key fails at agent boot like any other unknown overlay key (the established
  boot fail-fast), not at a new filtered check.
- **Database.** One forward migration drops `agent_presets` and
  `agents_meta.preset_name` (idempotent, no down migration) and
  `db/schema.sql` is synchronized. The task-assignment receipt keeps its
  historical `preset_name` key as an explicit JSON-null placeholder because
  the merged birth-snapshot CHECK still requires that key; the migration
  comment records the origin.
- **IM bridge spawn menu.** The `/spawn` menu becomes two layers
  (model → effort) with the same summary [Spawn] button and receipt shape;
  the preset layer and its copy strings are removed.
- **Guide.** The `meiva-guide` presets sub-skill is deleted; onboarding and
  migration documents point at the model skill and `config_overlay` instead.

## Consequences

- Historical decision records keep their prose as written; the two records
  above carry a Superseded-in-part note pointing here, and passing mentions of
  presets in other records remain historical evidence rather than current
  surfaces.
