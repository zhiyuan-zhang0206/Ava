---
type: doc
title: Language Model Provider Layer
description: '`shared/lm/` — provider-neutral LLM contracts above LangChain; enabled plugins own every chat provider.'
tags:
- shared
- library
- llm-inference
---

# Language Model Provider Layer

`shared/lm/` — provider-neutral contracts above LangChain, below the agent kernel. Core registers no providers or chat models; enabled plugins own every chat binding and model fact. The repository's eight `lm_*` plugins are enabled by default. Mechanics: [[shared/lm/provider-plugins.ava.okf.md]]; design: [model-providers-as-plugins](model-providers-as-plugins.md).

## Core Responsibilities

### factory (`factory.py`)
`build_chat_model(model)` dispatches through plugin-owned prefix bindings:

| Prefix | LangChain Class | Key Env |
|---|---|---|
| `claude-` | ChatAnthropic (+prompt cache) | ANTHROPIC_API_KEY |
| `deepseek-` | ChatAnthropic (Anthropic-compat endpoint) | DEEPSEEK_API_KEY |
| `gemini-` | ChatGoogleGenerativeAI | GEMINI_API_KEY |
| `gpt-` | ChatOpenAI (Responses API) | OPENAI_API_KEY |
| `mimo-` | ReasoningContentChatModel (Xiaomi) | MIMO_API_KEY |
| `kimi-` | ChatMoonshot (`langchain-moonshot`) | MOONSHOT_API_KEY |
| `glm-` | ReasoningContentChatModel (Zhipu) | GLM_API_KEY |
| `qwen` | ReasoningContentChatModel (Alibaba) | DASHSCOPE_API_KEY + `AVA_DASHSCOPE_BASE_URL` |

- `shared/lm/registry.py:MODELS` starts empty and is plugin-populated; derived views rebuild in place, so imported readers see new models immediately. A withdrawn model resolves persisted config to its declared spawnable fallback, never after provider failure.
- `validate_model_config()` — spawn-boundary pre-check (`POST /api/agents`): model registered + key configured, else 400 (fail-fast vs silent hang).
- Gateway lifespan loads providers; zero bindings raises before the once flag, so a corrected config is retryable.
- [[media-capabilities.ava.okf.md]] — per-model media resolution and attachment packing.
- `AVA_LLM_OVERRIDE=mod:factory` injects a fake factory (e2e/multi-instance); key checks skipped.
- `thinking: ThinkingConfig | None` — `TypedDict` for Anthropic extended-thinking (`{"type":"disabled"}`/`{"type":"enabled","budget_tokens":N}`); gemini-*/gpt-* read only `type`, mirroring on/off to reasoning toggles.

### content block shapes (`content.py`)
LangChain types `AIMessage(Chunk).content` weakly as `str | list[str | dict[str, Any]]`. `ContentBlock` (`TypedDict, total=False`) names the shape once, fields implied by `type` (text→`text`; thinking→`thinking`; signature_delta→`signature`; openai reasoning→`summary`; tool_use→`id`/`name`/`input` or `partial_json`; `index`=offset aligning streaming tool-call chunks with snapshots). `content_blocks(content)` runtime-relabels the list branch to `list[str | ContentBlock]`; `reasoning.py` / `_reasoning_compat.py` already use them.

### reasoning normalization (`reasoning.py`)
- `to_canonical_reasoning()` — folds OpenAI Responses `{type:reasoning, summary:[…]}` → canonical `{type:thinking}` (claude/gemini native). DISPLAY-only: stored AIMessages keep provider-native form (OpenAI requires verbatim echo).
- `extract_reasoning_tokens()` — `usage_metadata.output_token_details` preferred, else char estimates.

### stop classification (`stop.py`)
- `classify_stop()` → `StopCategory` (NORMAL/TRUNCATED/UNEXPECTED/CORRUPTED) by `model_provider`; core's table starts empty and plugin bindings register four client-class keys for eight providers (anthropic ← claude+deepseek, openai ← gpt+mimo+glm+qwen, google_genai, moonshot). TRUNCATED retries with raised max_tokens; an unregistered provider key fails with a `register_stop_spec` pointer.

### billing (`billing.py` + `pricing.py` + `pricing_catalog_archive.json`) — [[pricing.ava.okf.md]]
- `billing.py` records one `ava.billing.call` span for each completed provider call. Its v1 attributes use the `ava.billing.*` ledger schema and deliberately carry no task dimension; task budgets instead consume explicit `task_id` on `llm_usage` events. Core/provider-plugin manufacturer resolution, catalog pricing, and tracing guards are centralized so call sites only provide the response and usage kind.
- Plugin `PriceRates` are the live chat source and carry full history, tiers, windows, and future periods. The archive is their reconciliation ledger, live only for catalog-only services; `pricing_catalog.json` is an empty shell. `quote()` returns rates and cost atomically; both sources share the child node's parser and selector.

### durable usage — [[usage.ava.okf.md]]

### outbound concurrency — [[outbound-concurrency.ava.okf.md]]

### context budget (`context_budget.py`)
- `resolve_context_budget(model)` → `ContextBudget(max_context_tokens, soft_compact_tokens, hard_compact_tokens)`: hard = `min(auto_compact_fraction × window, auto_compact_ceiling_tokens)`; soft = `compact_reminder_fraction × window` (scaled down when the ceiling bites). One flat rule for the whole roster — soft 30% / hard 40% of each model's own window (`DEFAULT_TUNING` 0.3/0.4, ceiling 0 = no cap, no per-model compact override), per-agent overridable; registry entry ⇒ correct thresholds, no parallel table. Unregistered models raise `UnknownModelWindowError` (compact hook bubbles it; gateway display degrades to 0/0/0).
- `latest_input_tokens(messages)` → `input_tokens` from the latest AIMessage with `usage_metadata` (provider's true occupancy), `None` if absent (first turn/post-compact fallback chars/4). Shared by compact trigger (`agent/hooks/compact.py`, Option Y) and token-usage endpoint — one unit for gauge/scale/trigger.

### provider plugins
- [[shared/lm/provider-plugins.ava.okf.md]] — plugin binding and `key_env`
  delivery contract.

### compatibility layers
- `_anthropic_compat.py`: `ThinkingTokensChatAnthropic` — ChatAnthropic subclass patching thinking_tokens into usage_metadata (base drops it); shared by claude/deepseek.
- `_reasoning_compat.py`: `ReasoningContentChatModel` — ChatOpenAI subclass folding delta `reasoning_content` into canonical thinking blocks; **used by glm / mimo / qwen**. kimi uses `langchain-moonshot`; reasoning lands in `additional_kwargs["reasoning_content"]`, handled by fan-out + timeline.

## Notes

- **DeepSeek uses the Anthropic protocol, not langchain-deepseek**: the latter 1.0.1 breaks AIMessages on thinking + tool_calls + streaming (empty metadata → next-round 400s; upstream #34166 OPEN). The Anthropic-compat endpoint (`api.deepseek.com/anthropic`) sidesteps it.
- **max_tokens**: both anthropic-protocol branches (claude / deepseek) pin it explicitly to `ModelSpec.max_output_tokens` — langchain-anthropic falls back to a legacy 4096 for ids it doesn't know, truncating thinking mid-turn (#169). `_validate_registry` refuses a spawnable claude/deepseek entry without the cap; unregistered ids fail fast. OpenAI-style branches leave it unset (those APIs default to the model's own cap).
- **streaming default** (`ModelSpec.streaming`): True, and no registry entry sets False today (kimi-k3's former False was removed — `decisions/2026-07-25-per-model-tuning-values.md`). Explicit kwarg overrides.
- **model identity** (`ModelSpec.model_identity` → `MODEL_IDENTITY`): per-model note injected before knowledge cutoff in the system prompt (deepseek-v4-pro/flash, kimi-k3, both qwen3.8s).
- **Anthropic prompt caching**: claude branch passes `cache_control: ephemeral`; system + eligible blocks cached 5 min server-side. No facade — submodules imported directly.
- **Qwen**: graded by a token budget, not a level enum, so the knob rides the `enable_thinking` switch (mimo's binary `none`/`high`). Endpoint is CONFIG (`AVA_DASHSCOPE_BASE_URL`) — a dedicated workspace host is unreachable from the public default, and region changes reprice. Verified live 2026-08-20: thinking-off honored, and the streamed usage frame carries `cached_tokens` → `cache_read`. Explicit `cache_control` tier unwired.

- Key deps: [[llm.ava.okf.md]] (agent/graph/_llm.py calls `build_chat_model`)
