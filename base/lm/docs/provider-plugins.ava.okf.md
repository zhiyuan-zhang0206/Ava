---
type: doc
title: Provider Plugin Mechanics
description: 'The base-layer contract and lazy loader for LLM provider plugins.'
tags:
- base
- library
- llm-inference
- plugins
---

# Provider Plugin Mechanics

`provider_api.py` is the shared extension contract and `plugin_providers.py`
loads each enabled plugin's `provider.py` lazily, once, and under a lock.
Discovery identity remains the sibling `plugin.py`, but provider loading does
not import that agent-side module: gateway, labeler, and eval harness need the
binding without an agent runtime.
Core registers no binding or chat-model fallback. The eight repository `lm_*`
plugins are enabled by default, and gateway startup calls the loader eagerly;
an empty binding registry raises before the once flag is set so recovery is
retryable after the enable configuration is fixed.

## Declaration and installation

- A plugin's `provider.py` registers nothing: it exports `contribute()` returning
  `PluginContributions(providers=(ProviderContribution(binding, models, pricing),))`.
  The loader (`base/lm/plugin_providers.py`) builds the model catalog: it checks
  the plugin's manifest `providers` key against the declaration (a mismatch is a fail-soft load
  failure of that plugin), then installs it into a `CatalogBuilder` (`base/lm/catalog.py`).
  The prefix map is flat: duplicate or nested prefixes fail at load time, and a
  model id must begin with its binding prefix. Prices must name a registered
  model; they must be finite, non-negative, HTTPS-provenanced, and carry a
  YYYY-MM-DD source-check date.
- `ModelSpec` entries collect in the builder and receive the same facts/price/effort
  validation; `build()` freezes them into an immutable `ModelCatalog` whose derived
  views (`supported_models`, `context_windows`, `knowledge_cutoffs`, `identities`) are
  computed from it. The process holds one catalog, handed out by
  `plugin_providers.model_catalog()`; readers take it from there. `concurrency`'s
  known-key set is derived from the catalog's bindings at each call.
- Plugin rates are the runtime source for chat models. Registration removes an
  overlapping archive row from the in-memory catalog view; `rates_at` therefore
  selects plugin rates for bound chat models, archive rates for catalog-only
  services, then retired rates. `MODEL_PRICING` excludes retired-history
  entries. The plugin that owns a client class registers its terminal-reason
  `StopSpec`; compatible bindings share that emitted `model_provider` key.
- A binding's optional `AttachPolicy` owns provider-specific per-media byte
  limits, image-dimension tiers, and native PDF document blocks. An absent
  policy preserves the core attachment defaults for older plugins.

## Builder and key contract

Fast inference services are independent selectable Ava IDs. A provider may use
`with_fast_variants(PROVIDER, {standard_id: PriceRates(...)})` in `contribute()`:
it derives `<standard_id>-fast` rows with `ModelSpec.fast_of`, inheriting model
facts and tuning while keeping separate prices and Fast supersession links.
The builder maps `fast_of` to the provider's wire model and Fast parameter.
Native vendor IDs such as MiMo UltraSpeed need no alias.

Bindings offering Fast variants must supply `served_speed(metadata)`. It
validates the actual response receipt as `InferenceSpeed`; missing or unknown
receipts fail before accounting. An explicit Standard receipt selects the base
ID's rates. OpenAI Standard IDs explicitly send `service_tier="default"`, so
project-level Fast defaults cannot silently change their pricing.
The existing model API and picker derive both IDs and their prices from the
catalog; effort remains a separate setting. The pricing synchronizer parses
both literal rate tables without executing plugin code.

- `build(ctx)` is a pure function of `BuildContext` (model, spec, thinking,
  resolved_effort, disable_streaming, timeout): no caller, agent, error
  history, or routing is exposed. The builder validates `resolved_effort` with
  `validate_effort`; `require_key(key_env)` fails at build time if the bootstrap
  environment lacks the key.
- The spawn boundary reads a plugin key from the cluster `.env`; split runners
  receive enabled bindings' present keys through bootstrap plugin-secrets, and
  a single-box agent child receives only those declared keys from its parent's
  env. A provider-plugin key is deliberately not a Settings field. `build_chat_model`,
  `validate_model_config`, model-list and context
  endpoints, vision checks, the compact gate's `resolve_context_budget`,
  and the config-overlay validation (`validate_config_overlay`) ensure the
  loader has run before they consult registration state.

## Explicit single-attempt construction

`BuildContext.max_retries` is additive and defaults to `None`; ordinary builders
retain their existing retry policy. `ProviderBinding.build_single_attempt` is
an optional explicit provider-owned construction contract. A binding that does
not declare it cannot support `build_chat_model(single_attempt=True)`. Custom
model overrides are likewise unsupported rather than inferred from a client
class or mutating a cached model.

The OpenAI and Anthropic owners construct fresh clients with `max_retries=0` for
this path. Guarded generation freezes the original available model and rejects
unavailable-model fallback. Callers must also suppress their own cache/retry
loops; this construction contract cannot prove exactly-once external vendor
execution or recover a response that was lost before durable storage.
The manual compact consumer and its proof boundary are documented in
[[base/agents/compaction/docs/manual-compact/manual-compact.ava.okf.md]].
