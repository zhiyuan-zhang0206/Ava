---
type: doc
title: Language Model Provider Layer
description: '`shared/lm/` — unified LLM provider abstraction above LangChain; eight providers behind one factory.'
tags:
- shared
- library
- llm-inference
---

# Language Model Provider Layer

`shared/lm/` — unified LLM provider abstraction above LangChain, below the agent kernel: eight providers, provider-agnostic upper layers. Adding a provider is a core edit across up to seven files (`config/lm.py` key field, `registry.py` `MODELS` entries, `_providers.py` builder, `factory.py` `_MODEL_KEY_MAP` + branch, `_effort.py` vocabulary, `stop.py` terminal-reason entry when the client emits an unseen `model_provider`, `pyproject.toml` dep) — making it a plugin concern is planned in [model-providers-as-plugins](model-providers-as-plugins.md).

## Core Responsibilities

### factory (`factory.py`)
`build_chat_model(model)` dispatches by prefix:

| Prefix | LangChain Class | Key Env |
|---|---|---|
| `claude-` | ChatAnthropic (+prompt cache) | ANTHROPIC_API_KEY |
| `deepseek-` | ChatAnthropic (Anthropic-compat endpoint) | DEEPSEEK_API_KEY |
| `gemini-` | ChatGoogleGenerativeAI | GEMINI_API_KEY |
| `gpt-` | ChatOpenAI (Responses API) | OPENAI_API_KEY |
| `mimo-` | ReasoningContentChatModel (Xiaomi) | MIMO_API_KEY |
| `kimi-` | ChatMoonshot (`langchain-moonshot`) | MOONSHOT_API_KEY |
| `glm-` | ReasoningContentChatModel (Zhipu) | GLM_API_KEY |
| `grok-` | ChatXAI (`langchain-xai`) | XAI_API_KEY |

- SSOT is `registry.py:MODELS`, one `ModelSpec` per model id; `SUPPORTED_MODELS` (spawn dropdown), `MODEL_CONTEXT_WINDOW`, `MODEL_KNOWLEDGE_CUTOFF`, `MODEL_IDENTITY` are derived views re-exported through factory. Per-PROVIDER tables stay in factory (`_MODEL_KEY_MAP`, `_VISION_MODEL_PREFIXES`) / `_effort.py` (`_PROVIDER_EFFORT_LEVELS`).
- `validate_model_config()` — spawn-boundary pre-check (`POST /api/agents`): model registered + key configured, else 400 (fail-fast vs silent hang).
- `model_supports_vision()` — claude/gemini/gpt/kimi/grok native images; deepseek/mimo/glm text-only (endpoint 422s images).
- `AVA_LLM_OVERRIDE=mod:factory` injects a fake factory (e2e/multi-instance); key checks skipped.
- `thinking: ThinkingConfig | None` — `TypedDict` for Anthropic extended-thinking (`{"type":"disabled"}`/`{"type":"enabled","budget_tokens":N}`); gemini-*/gpt-* read only `type`, mirroring on/off to reasoning toggles.

### content block shapes (`content.py`)
LangChain types `AIMessage(Chunk).content` weakly as `str | list[str | dict[str, Any]]`. `ContentBlock` (`TypedDict, total=False`) names the shape once, fields implied by `type` (text→`text`; thinking→`thinking`; signature_delta→`signature`; openai reasoning→`summary`; tool_use→`id`/`name`/`input` or `partial_json`; `index`=offset aligning streaming tool-call chunks with snapshots). `content_blocks(content)` runtime-relabels the list branch to `list[str | ContentBlock]`; `reasoning.py` / `_reasoning_compat.py` already use them.

### reasoning normalization (`reasoning.py`)
- `to_canonical_reasoning()` — folds OpenAI Responses `{type:reasoning, summary:[…]}` → canonical `{type:thinking}` (claude/gemini native). DISPLAY-only: stored AIMessages keep provider-native form (OpenAI requires verbatim echo).
- `extract_reasoning_tokens()` — `usage_metadata.output_token_details` preferred, else char estimates.

### stop classification (`stop.py`)
- `classify_stop()` → `StopCategory` (NORMAL/TRUNCATED/UNEXPECTED/CORRUPTED) by `model_provider`; `_BY_PROVIDER` has five keys for eight providers (anthropic ← claude+deepseek, openai ← gpt+mimo+glm, google_genai, moonshot, xai). TRUNCATED retries with raised max_tokens; unknown provider fail-fast.

### billing (`pricing.py` + `pricing_catalog.json`)
- The reviewed JSON catalog is the sole volatile pricing source: every model has official-source provenance plus gapless effective periods, input-token tiers, and optional recurring UTC rate windows. The registry carries no duplicate price tuples.
- Date-only future increases with no provider timezone use the documented conservative UTC+14 boundary and carry an `effective_time_note`; exact published instants are used unchanged.
- `rates_at(model, at, input_tokens)` selects one exact 3-rate tuple `(cache_miss, cache_hit, out)` USD/M. `quote()` returns those rates and the computed cost atomically so a scheduled boundary cannot split the event snapshot; `cost_usd()` remains the compatibility reader and returns `None` for unknown models.
- `scripts/update_model_pricing.py` reconciles strict official-source adapters with the checked-in catalog. Automation proposes an append-only, reviewable PR; runtime pricing stays deterministic and network-free.

### context budget (`context_budget.py`)
- `resolve_context_budget(model)` → `ContextBudget(max_context_tokens, soft_compact_tokens, hard_compact_tokens)`: hard = `min(auto_compact_fraction × window, auto_compact_ceiling_tokens)`; soft = `compact_reminder_fraction × window` (scaled down when the ceiling bites). One flat rule for the whole roster — soft 30% / hard 40% of each model's own window (`DEFAULT_TUNING` 0.3/0.4, ceiling 0 = no cap, no per-model compact override), per-agent overridable; registry entry ⇒ correct thresholds, no parallel table. Unregistered models raise `UnknownModelWindowError` (compact hook bubbles it; gateway display degrades to 0/0/0).
- `latest_input_tokens(messages)` → `input_tokens` from the latest AIMessage with `usage_metadata` (provider's true occupancy), `None` if absent (first turn/post-compact fallback chars/4). Shared by compact trigger (`agent/hooks/compact.py`, Option Y) and token-usage endpoint — one unit for gauge/scale/trigger.

### compatibility layers
- `_anthropic_compat.py`: `ThinkingTokensChatAnthropic` — ChatAnthropic subclass patching thinking_tokens into usage_metadata (base drops it); shared by claude/deepseek.
- `_reasoning_compat.py`: `ReasoningContentChatModel` — ChatOpenAI subclass folding delta `reasoning_content` into canonical thinking blocks; **used only by glm / mimo**. kimi / grok use `langchain-moonshot` / `langchain-xai`; reasoning lands in `additional_kwargs["reasoning_content"]`, handled by fan-out + timeline.

## Notes

- **DeepSeek uses the Anthropic protocol, not langchain-deepseek**: the latter 1.0.1 breaks AIMessages on thinking + tool_calls + streaming (empty metadata → next-round 400s; upstream #34166 OPEN). The Anthropic-compat endpoint (`api.deepseek.com/anthropic`) sidesteps it.
- **max_tokens**: both anthropic-protocol branches (claude / deepseek) pin it explicitly to `ModelSpec.max_output_tokens` — langchain-anthropic falls back to a legacy 4096 for ids it doesn't know, truncating thinking mid-turn (#169). `_validate_registry` refuses a spawnable claude/deepseek entry without the cap; unregistered ids fail fast. OpenAI-style branches leave it unset (those APIs default to the model's own cap).
- **streaming default** (`ModelSpec.streaming`): True, and no registry entry sets False today (kimi-k3's former False was removed — `decisions/2026-07-25-per-model-tuning-values.md`). Explicit kwarg overrides.
- **model identity** (`ModelSpec.model_identity` → `MODEL_IDENTITY`): per-model note injected before knowledge cutoff in the system prompt (deepseek-v4-pro/flash, kimi-k3).
- **Anthropic prompt caching**: claude branch passes `cache_control: ephemeral`; system + eligible blocks cached 5 min server-side. No facade — submodules imported directly.

- Key deps: [[llm.ava.okf.md]] (agent/graph/_llm.py calls `build_chat_model`) — the eval harness (MyAva) reuses the factory too
