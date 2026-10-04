# The model catalog is an immutable value built by the provider loader

## Context

`decisions/2026-10-03-plugins-declare-the-framework-registers.md` left the model catalog as a
deliberate non-change: a process-level singleton with one writer. In code that singleton was
twelve module-level mutables (`MODELS`, four derived views rebuilt in place, the provider
`REGISTRY`, the plugin price table, the archive table that registration `pop`ped from, the stop
vocabulary table, a core-prefix reservation seam that was always empty) plus two import-time calls
that validated an empty table. Every one was written by registration and read to decide.

## Decision

`base/lm/catalog.py:ModelCatalog` is a frozen value: models, provider bindings, stop vocabularies and
a `PriceBook` (archive plus plugin prices), with the derived views computed from it. A
`CatalogBuilder` accumulates each plugin's `ProviderContribution`; `build()` validates the whole model
graph and freezes it. Nothing registers at import and nothing mutates after `build()`; a failed
installation discards the builder.

`base/lm/plugin_providers.py:model_catalog()` builds it once per process from the enabled plugins and
is the only way readers get it (`ensure_provider_plugins_loaded` and the module-level tables are
gone). The one slot that holds it is the loader's own `_STATE`; `use_catalog` lends a different
catalog for a block, and tests inject rows through the `add_models` / `add_bindings` / `set_prices`
fixtures instead of writing into tables.

## Alternatives rejected

- **Thread the catalog through every caller as a parameter.** `resolve_setting`,
  `resolve_context_budget`, `media_types_for_model`, `provider_key_of_model` and the pricing functions
  are called from the agent graph, the gateway, telemetry and plugins; none of those composition roots
  owns a catalog today, so the change would add the parameter to every one of those signatures and a second source of
  truth to each root. A process never holds two catalogs, so the slot is not a hidden dependency.
- **Keep the module tables and fix their leaks.** The leaks were the tables: a test that restored part
  of them left a half-registered process, and a failed plugin install left earlier prefixes bound.
- **Keep the reserved-prefix seam.** Core registers no providers; the set was always empty.

## Consequences

- Readers that previously worked without loading plugins (`classify_stop`, the pricing functions) now
  load them on first use, as every other catalog reader already did.
- `_STATE` in `plugin_providers.py` is the one remaining baselined site of this family.

<!-- Narrows: decisions/2026-10-03-plugins-declare-the-framework-registers.md ("the model catalog stays a process-level singleton with one writer") -->
