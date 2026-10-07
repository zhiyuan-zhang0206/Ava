# Preset moves inside config overlay; fork is config-stable and cache-friendly

## Context

User ruling 2026-09-10 (task #2694), with the explicit note that cache
friendliness is "very important":

1. `preset` should stop being a spawn parameter parallel to `config_overlay`
   and become a field inside it (`config_overlay.preset = "name"`).
2. Display surfaces (the inspector's config-overlay section) must show, when a
   preset was used, only the preset reference plus the fields that the overlay
   gives *separately* and that differ from (or are absent in) the preset —
   diff semantics, no duplication of preset-carried fields.
3. Fork must not change config in a way that breaks the context cache: the
   fork's effective config stays what the source had, and a skill added at
   fork time (the one sanctioned change) must load at the tail of the context,
   never rewriting the cached prefix.

The current mechanics those requirements collide with:

- `POST /api/agents` accepts a top-level `preset`; the gateway folds the
  preset's stored config into `body.config` (explicit wins per key) and clears
  the field, so the runner never sees the preset and the stored row records
  only the merged map — provenance "preset vs explicit" is lost, and the
  inspector cannot know a preset was involved.
- A fork **without** config stores `config_overlay=NULL` and inherits only the
  source's `birth_config` stamp — it silently drops the source's own overlay.
  A fork **with** config changes the effective model/effort/skills freely.
  Both shapes change what the fork runs relative to the source: the provider
  cache is keyed per model (and the inherited SystemMessage, byte-stable by the
  snapshot invariant, names the source's model), so a model drift at fork
  re-caches the whole inherited prefix — and after the fork's first compaction
  the system prompt is rebuilt from the fork's config anyway, a second
  full-price cache.
- Skills the fork's config adds to `skills_to_inject_into_system_prompt` are
  invisible to the fork until its first compaction: the `# Capabilities` index
  is frozen inside the inherited SystemMessage. (`skills_to_expand_at_start`
  additions are already tail-grafted by the `on_fork` preloaded-skills note.)

## Decision

**Input surface.** `config_overlay` may carry `"preset": "<name>"`. The legacy
`preset` spawn parameter / wire field stays accepted for one compatibility
window and is normalized to the same key (passing both is a 400 / ValueError).
Preset resolution happens at the spawn boundary, exactly as before: the
effective overlay is `{**preset.config, **explicit}`, an unknown preset is a
400, and the launch op / runner boot see only the resolved map (no `preset`
key — `apply_config_overlay` and the pins resolvers never learn it).

**Storage.** `agents_meta.config_overlay` keeps storing the **resolved**
effective overlay (today's fold), and a new `agents_meta.preset_name` column
stores the preset reference. Chosen over storing the raw `{preset, explicit...}`
map: every existing consumer of `config_overlay` (the hosted-turn pins, plugin
pins, child boot, restart merge, fork inheritance, inspect) then keeps working
unchanged — a consumer that forgets the new column still sees correct values,
whereas a forgotten raw-preset resolution silently drops the preset's fields.
Resolved storage also freezes the spawn-time values against later preset edits
(consistent with the birth-frozen identity philosophy — the preset is resolved
once at spawn, and a later edit re-brains only future spawns).

**Display diff.** `GET /api/agents/{id}/inspect` also returns `preset_name`.
The inspector shows a `Preset: <name>` row, then only the overlay fields whose
value differs from the preset's *current* config (or that the preset lacks);
fields the preset already supplies verbatim are suppressed. The diff is
computed client-side against `GET /api/presets` (already fetched by the spawn
picker). A preset deleted since spawn falls back to the plain full overlay
list plus the reference. The spawn dialog sends `config.preset` instead of the
sibling `preset` field.

**Fork rule.** The fork's effective config must equal the source's effective
config (`{**birth_config, **config_overlay}` on both sides), with exactly one
carve-out: the two skill lists (`skills_to_inject_into_system_prompt`,
`skills_to_expand_at_start`) may only gain skills (superset). Any other
difference is rejected up front with a new wire error
(`fork_config_change_not_allowed`, 400). A fork *without* config now copies
the source's resolved `config_overlay` + `preset_name` verbatim instead of
dropping the overlay — the fork runs exactly what the source ran, so the
inherited SystemMessage stays truthful and the provider cache key
(model, prompt) is the same one the source's context already rides.

**Tail loading.** The gateway computes the delta at spawn time:
`(fork_inject - source_inject) - fork_expand` — inject-list skills the fork
adds that its preloaded note does not already cover — and carries the names in
the fork lifecycle inbound's JSONB payload. The claim's fork handler resolves
them (same resolver + warn-and-skip as the preloaded-skills note) and appends
one system note with their full SKILL.md bodies at the tail, after the fork
marker and the `on_fork` notes, tagged `preloaded_skills` so the next fork
strips it like any other skill note. Nothing in front of the first
source-identity note changes, so the cached prefix survives byte-for-byte.
Expand-list additions need no new mechanism — the existing `fork_notes` graft
already puts them at the tail.

## Alternatives rejected

**Store the raw `{preset, explicit}` map in `config_overlay`.** One column, no
migration, and the explicit fields stay distinguishable from preset-derived
ones. Rejected: every read path (hosted pins, plugin pins, child boot, restart,
fork inheritance, inspect) must resolve the preset, and one that forgets
silently loses preset-carried values — the exact class of failure the
provenance split exists to prevent. The dedicated column confines preset logic
to the spawn boundary and the display.

**A new agents_meta column for the explicit diff, resolved values in the
overlay.** Keeps the overlay resolved and the explicit map separately. Rejected
as a third storage shape for one concern: the diff the display needs is
recoverable by comparing the resolved overlay against the preset's current
config, and the rare drift case (preset edited after spawn) is a display-only
inaccuracy, not a config one.

**Allow every cache-safe config change at fork.** Effort and live operational
knobs do not touch the cached prefix and could be let through. Rejected: the
user's principle is "fork does not change config", the sanctioned exception is
skill additions, and a per-field cache-safety audit is a standing maintenance
liability for zero demand.

**Graft the delta skills at the next compaction instead of at fork.** Rejected
directly by the requirement: the skill must be usable from the fork's first
turn, and compaction is not guaranteed to arrive soon (or at all).

## Consequences

- One migration (`agents_meta.preset_name`), reversible; old rows read NULL and
  the inspector renders exactly as today.
- A fork of an agent that carried an overlay now keeps that overlay — a
  deliberate behavior change that also fixes the silent model/identity drift
  at fork (the fork's inherited SystemMessage no longer names a model it does
  not run).
- Spawn callers that want to change a fork's model/effort must do it
  explicitly after the fact (restart with an overlay) instead of at fork time;
  the 400 names the offending keys.
- The legacy top-level `preset` parameter remains on the wire for one
  compatibility window; the SDK docstring marks it deprecated.
- Preset configs edited after a spawn drift from the stored overlay: the
  inspector then shows those fields as "differs from preset" — honest about
  the effective values, and the next spawn picks up the edited preset.
