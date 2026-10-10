---
type: doc
title: Provider Plugin Mechanics
description: 'The base-layer contract and explicit catalog construction for LLM provider plugins.'
tags:
- base
- library
- llm-inference
- plugins
---

# Provider Plugin Mechanics

`provider_api.py` is the shared extension contract. A composition root calls
`plugin_providers.build_model_catalog()` and retains the immutable result in its
Installation, application state or runtime owner. Every reader receives that
catalog, or its specific models, bindings, stops or prices view, explicitly.
Construction discovers enabled `provider.py` faces without importing their
agent-side `plugin.py` modules. Each attempt uses a fresh builder; zero bindings
or any loading failure rejects the entire attempt. Corrected configuration can
be retried by constructing a new catalog.

## Declaration and installation

- A plugin's `provider.py` registers nothing: it exports `contribute()` returning
  `PluginContributions(providers=(ProviderContribution(binding, models, pricing),))`.
  The loader (`base/lm/plugin_providers.py`) builds the model catalog: it checks
  the plugin's manifest `providers` key against the declaration, then installs it into a `CatalogBuilder` (`base/lm/catalog/__init__.py`).
  The prefix map is flat: duplicate or nested prefixes fail at load time, and a
  model id must begin with its binding prefix. Prices must name a registered
  model; they must be finite, non-negative, HTTPS-provenanced, and carry a
  YYYY-MM-DD source-check date.
- `ModelSpec` entries collect in the builder and receive the same facts/price/effort
  validation; `build()` freezes them into an immutable `ModelCatalog` whose derived
  views (`supported_models`, `context_windows`, `knowledge_cutoffs`, `identities`) are
  computed from it. Independently constructed catalogs do not change each other.
  `concurrency`'s known-key set is derived from the supplied catalog's bindings.
- Plugin rates are the runtime source for chat models. Registration removes an
  overlapping archive row from the in-memory catalog view; `rates_at` therefore
  selects plugin rates for bound chat models, archive rates for catalog-only
  services, then retired rates. Each call receives the owner's `PriceBook`; its
  active view excludes retired-history entries. The plugin that owns a client class registers its terminal-reason
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
  resolved_effort, thinking_budget_tokens, disable_streaming, timeout, provider_model_ids): no caller, agent, error
  history, or routing is exposed. The builder validates `resolved_effort` with
  `validate_effort`; `require_key(key_env)` fails at build time if the bootstrap
  environment lacks the key.
- The spawn boundary reads a plugin key from the cluster `.env`; split runners
  receive enabled bindings' present keys through bootstrap plugin-secrets, and
  a single-box agent child receives only those declared keys from its parent's
  env. A provider-plugin key is deliberately not a Settings field. `build_chat_model`,
  `validate_model_config`, model-list and context
  endpoints, vision checks and the compact gate's `resolve_context_budget`
  consume the supplied catalog without constructing an owner. DTO validators
  check overlay structure; admission checks membership with the request owner's
  model view before writes or execution.

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

Known declaration violations raise their typed registration error. Unexpected
Python errors from import, contribution, installation or final construction
propagate with their original object and traceback; no partial catalog is
published. A failed provider module is removed from `sys.modules` before rethrow.
