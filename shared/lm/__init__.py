"""Unified LM provider layer — the provider-difference concerns that sit above
LangChain and below the agent kernel, consolidated into one package.

LangChain normalizes tool calls (`tool_calls`), usage, and content blocks, but
not everything; the rest is collected here so the kernel, gateway, and the
eval harness (MyAva) stay provider-agnostic. Plan for onboarding providers as
plugins: `shared/lm/model-providers-as-plugins.md`.
- `factory`   — `build_chat_model` (prefix dispatch to the provider SDK) + the
                model catalog (`SUPPORTED_MODELS`, `MODEL_CONTEXT_WINDOW`).
- `pricing`   — per-model rate table + `tally_tokens` / `cost_usd`.
- `reasoning` — `to_canonical_reasoning`: fold each provider's reasoning block
                (openai `reasoning`/summary, anthropic/gemini `thinking`) to the
                one canonical `thinking` shape the renderers speak.
- `stop`      — `classify_stop`: normalize each provider's finish/stop reason to
                one vocabulary (`StopCategory`).
- `errors`    — `classify_error`: normalize a failed call's provider-SDK
                exception to one `ErrorClass` (transient / permanent / unknown).

Import from the submodules directly (`from shared.lm.factory import
build_chat_model`); this package keeps no facade so importing one concern does
not pull the others.
"""
