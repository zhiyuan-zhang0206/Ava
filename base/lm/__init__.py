"""Unified LM provider layer — the provider-difference concerns that sit above
LangChain and below the agent kernel, consolidated into one package.

LangChain normalizes tool calls (`tool_calls`), usage, and content blocks, but
not everything; the rest is collected here so the kernel, gateway, and the
callers stay provider-agnostic. Plan for onboarding providers as
plugins: `base/lm/model-providers-as-plugins.md`.
- `factory`   — `build_chat_model` (prefix dispatch to a plugin binding).
- `provider_api` — the provider-plugin contract (`ProviderBinding`,
                `BuildContext`, `ProviderContribution`).
- `catalog`   — `ModelCatalog`, the immutable value of models, bindings, stop
                vocabularies and prices, and the `CatalogBuilder` that makes one.
- `plugin_providers` — the base-layer loader importing every enabled
                plugin's `provider.py` once per process into the process's
                catalog (`model_catalog()`).
- `pricing`   — per-model rate table + `tally_tokens` / `cost_usd`.
- `billing`   — one trace-safe `ava.billing.*` span per completed provider call.
- `reasoning` — `to_canonical_reasoning`: fold each provider's reasoning block
                (openai `reasoning`/summary, anthropic/gemini `thinking`) to the
                one canonical `thinking` shape the renderers speak.
- `stop`      — `classify_stop`: normalize each provider's finish/stop reason to
                one vocabulary (`StopCategory`).
- `errors`    — `classify_error`: normalize a failed call's provider-SDK
                exception to one `ErrorClass` (transient / permanent / unknown).

Import from the submodules directly (`from base.lm.factory import
build_chat_model`); this package keeps no facade so importing one concern does
not pull the others.
"""
