---
type: doc
title: Language Model Provider Layer
description: '`base/lm/` — provider-neutral LLM contracts above LangChain; enabled plugins own every chat provider.'
tags:
- base
- library
- llm-inference
---

# Language Model Provider Layer

`base/lm/` — provider-neutral contracts above LangChain, below the agent kernel. Core registers no providers or chat models; enabled plugins own every chat binding and model fact. The repository's eight `lm_*` plugins are enabled by default. Mechanics: [[base/lm/docs/provider-plugins.ava.okf.md]]; design: [model-providers-as-plugins](../model-providers-as-plugins.md).

## Core Responsibilities

### factory (`factory.py`)
`build_chat_model(model, *, catalog, llm_override, overrides)` dispatches through plugin-owned prefix bindings:

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

- `base/lm/catalog/__init__.py:ModelCatalog` is an immutable value built from the enabled plugins (`plugin_providers.build_model_catalog()`). A withdrawn model resolves persisted config to its declared spawnable fallback, never after provider failure.
- `validate_model_config()` — spawn-boundary pre-check (`POST /api/agents`): model registered, explicit effort supported, and key configured, else 400. Effort is validated exactly, never translated to a nearby grade.
- [[base/lm/docs/model-configuration.ava.okf.md]] — effective agent model validation.
- Gateway lifespan builds and retains its catalog; zero bindings rejects boot, and a fresh construction can retry corrected configuration.
- [[media-capabilities.ava.okf.md]] — per-model media resolution and attachment packing.
- `AVA_LLM_OVERRIDE=mod:factory` injects a fake factory (e2e/multi-instance); key checks skipped. Factories receive `factory(model, *, agent_id)`; None means a non-agent caller. Use the argument, not a host SDK binding.
- `thinking: ThinkingConfig | None` — `TypedDict` for Anthropic extended-thinking (`{"type":"disabled"}`/`{"type":"enabled","budget_tokens":N}`); gemini-*/gpt-* read only `type`, mirroring on/off to reasoning toggles.

### content block shapes (`content.py`)
LangChain types `AIMessage(Chunk).content` weakly as `str | list[str | dict[str, Any]]`. `ContentBlock` (`TypedDict, total=False`) names the shape once, fields implied by `type` (text→`text`; thinking→`thinking`; signature_delta→`signature`; openai reasoning→`summary`; tool_use→`id`/`name`/`input` or `partial_json`; `index`=offset aligning streaming tool-call chunks with snapshots). `content_blocks(content)` runtime-relabels the list branch to `list[str | ContentBlock]`; `reasoning.py` / `compat/openai_reasoning.py` already use them.

### reasoning normalization (`reasoning.py`)
- `to_canonical_reasoning()` — folds OpenAI Responses `{type:reasoning, summary:[…]}` → canonical `{type:thinking}` (claude/gemini native). DISPLAY-only: stored AIMessages keep provider-native form (OpenAI requires verbatim echo).
- `extract_reasoning_tokens()` — `usage_metadata.output_token_details` preferred, else char estimates.

### stop classification (`stop.py`)
- `classify_stop(..., stops=catalog.stops)` → `StopCategory` (NORMAL/TRUNCATED/UNEXPECTED/CORRUPTED) by `model_provider`; plugin bindings declare four client-class keys for eight providers (anthropic ← claude+deepseek, openai ← gpt+mimo+glm+qwen, google_genai, moonshot). TRUNCATED retries with raised max_tokens; an unregistered provider key fails.

### billing (`pricing/billing.py` + `pricing/__init__.py` + `pricing_catalog_archive.json`) — [[pricing.ava.okf.md]]
- `pricing/billing.py` records one `ava.billing.call` span for each completed provider call. Its v1 attributes use the `ava.billing.*` ledger schema and deliberately carry no task dimension. Agent and birth-lineage usage is queried independently of task records. Core/provider-plugin manufacturer resolution, catalog pricing, and tracing guards are centralized so call sites provide the response, usage kind and their explicit catalog.
- Plugin `PriceRates` are the live chat source and carry full history, tiers, windows, and future periods. The archive is their reconciliation ledger, live only for catalog-only services; `pricing_catalog.json` is an empty shell. `quote()` returns rates and cost atomically; both sources share the child node's parser and selector.

### durable usage — [[usage.ava.okf.md]]

### provider errors — [[provider-errors.ava.okf.md]]

### context budget — [[base/lm/docs/lm/context-budget.ava.okf.md]]

### provider plugins
- [[base/lm/docs/provider-plugins.ava.okf.md]] — plugin binding and `key_env`
  delivery contract.

### compatibility layers — [[base/lm/compat/docs/compat.ava.okf.md]]

## Notes

- **DeepSeek uses the Anthropic protocol, not langchain-deepseek**: the latter 1.0.1 breaks AIMessages on thinking + tool_calls + streaming (empty metadata → next-round 400s; upstream #34166 OPEN). The Anthropic-compat endpoint (`api.deepseek.com/anthropic`) sidesteps it.
- **max_tokens**: both anthropic-protocol branches (claude / deepseek) pin it explicitly to `ModelSpec.max_output_tokens` — langchain-anthropic falls back to a legacy 4096 for ids it doesn't know, truncating thinking mid-turn (#169). `_validate_registry` refuses a spawnable claude/deepseek entry without the cap; unregistered ids fail fast. OpenAI-style branches leave it unset (those APIs default to the model's own cap).
- **streaming default** (`ModelSpec.streaming`): True, and no registry entry sets False today (kimi-k3's former False was removed — `docs/decisions/engineering/design/simplification/2026-07-25-per-model-tuning-values.md`). Explicit kwarg overrides.
- **model identity** (`ModelSpec.model_identity` → `ModelCatalog.identities`): per-model note injected before knowledge cutoff in the system prompt (deepseek-flash, kimi-k3, both qwen3.8s).
- **Anthropic prompt caching**: claude branch passes `cache_control: ephemeral`; system + eligible blocks cached 5 min server-side. No facade — submodules imported directly.
- **Qwen**: graded by a token budget, not a level enum, so the knob rides the `enable_thinking` switch (mimo's binary `none`/`high`). Endpoint is CONFIG (`AVA_DASHSCOPE_BASE_URL`) — a dedicated workspace host is unreachable from the public default, and region changes reprice. Verified live 2026-08-20: thinking-off honored, and the streamed usage frame carries `cached_tokens` → `cache_read`. Explicit `cache_control` tier unwired.

- Key deps: [[llm.ava.okf.md]] (agent/graph/llm/node.py calls `build_chat_model`)
