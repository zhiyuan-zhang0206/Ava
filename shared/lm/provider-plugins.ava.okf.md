---
type: doc
title: Provider Plugin Mechanics
description: 'The shared-layer contract and lazy loader for LLM provider plugins.'
tags:
- shared
- library
- llm-inference
- plugins
---

# Provider Plugin Mechanics

`provider_api.py` is the shared extension contract and `_plugin_providers.py`
loads each enabled plugin's `provider.py` lazily, once, and under a lock.
Discovery identity remains the sibling `plugin.py`, but provider loading does
not import that agent-side module: gateway, labeler, and eval harness need the
binding without an agent runtime.
Core registers no binding or chat-model fallback. The eight repository `lm_*`
plugins are enabled by default, and gateway startup calls the loader eagerly;
an empty binding registry raises before the once flag is set so recovery is
retryable after the enable configuration is fixed.

## Registration

- A plugin's `provider.py` calls `register(binding, models=..., pricing=...)`.
  The prefix map is flat: duplicate or nested prefixes fail at load time, and a
  model id must begin with its binding prefix. Prices must name a registered
  model; they must be finite, non-negative, HTTPS-provenanced, and carry a
  YYYY-MM-DD source-check date.
- `ModelSpec` entries merge into the initially empty `MODELS` table in place and
  receive the same facts/price/effort validation. The derived
  `SUPPORTED_MODELS`, `MODEL_CONTEXT_WINDOW`, `MODEL_KNOWLEDGE_CUTOFF`, and
  `MODEL_IDENTITY` views rebuild in place, so existing import sites see the
  plugin. Provider registration also invalidates `_concurrency`'s known-key
  cache.
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

- `build(ctx)` is a pure function of `BuildContext` (model, spec, thinking,
  resolved_effort, disable_streaming, timeout): no caller, agent, error
  history, or routing is exposed. The builder clamps `resolved_effort` with
  `_clamp_effort`; `require_key(key_env)` fails at build time if the bootstrap
  environment lacks the key.
- The spawn boundary reads a plugin key from the cluster `.env`; split runners
  receive enabled bindings' present keys through bootstrap plugin-secrets, and
  a single-box agent child receives only those declared keys from its parent's
  env. The install seed allowlist uses the same declaration only for unmodeled
  keys or already-seedable Settings aliases; it excludes unrelated modeled
  settings, cluster identity/data-plane aliases, and the runner database
  password. It is deliberately not a Settings field. `build_chat_model`,
  `validate_model_config`, model-list and context
  endpoints, and vision checks ensure the loader has run before they consult
  registration state.
