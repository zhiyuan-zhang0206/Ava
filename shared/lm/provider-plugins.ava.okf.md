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
loads each enabled plugin's `provider.py` lazily, once, and under a lock. This
path is independent of `plugin.py`: the gateway, labeler, and eval harness need
provider bindings even though they do not import agent-side plugin code.

## Registration

- A plugin's `provider.py` calls `register(binding, models=..., pricing=...)`.
  The prefix map is flat: duplicate or nested prefixes fail at load time, and a
  model id must begin with its binding prefix. Prices must name a registered
  model; they must be finite, non-negative, HTTPS-provenanced, and carry a
  YYYY-MM-DD source-check date.
- `ModelSpec` entries merge into `MODELS` in place and receive the same
  facts/price/effort validation as the core roster. The derived
  `SUPPORTED_MODELS`, `MODEL_CONTEXT_WINDOW`, `MODEL_KNOWLEDGE_CUTOFF`, and
  `MODEL_IDENTITY` views rebuild in place, so existing import sites see the
  plugin. Provider registration also invalidates `_concurrency`'s known-key
  cache.
- Plugin rates live in the plugin price table: `rates_at` selects catalog,
  plugin, then retired rates. `MODEL_PRICING` includes current catalog and
  plugin prices, never retired-history entries. A binding optionally registers
  a terminal-reason `StopSpec` when its client emits an unseen
  `model_provider`.

## Builder and key contract

- `build(ctx)` is a pure function of `BuildContext` (model, spec, thinking,
  resolved_effort, disable_streaming, timeout): no caller, agent, error
  history, or routing is exposed. The builder clamps `resolved_effort` with
  `_clamp_effort`; `require_key(key_env)` fails at build time if the bootstrap
  environment lacks the key.
- The spawn boundary reads a plugin key from the cluster `.env`; split runners
  receive it through bootstrap plugin-secrets. It is deliberately not a
  Settings field. `build_chat_model`, `validate_model_config`, model-list and
  context endpoints, and vision checks ensure the loader has run before they
  consult registration state.
