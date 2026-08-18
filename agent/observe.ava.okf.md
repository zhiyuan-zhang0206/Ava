---
type: doc
title: Observability
description: LLM usage observation tool — extracts standardized token usage from LangChain's `usage_metadata` and logs it. Implemented by `agent/observe.py`
tags: []
---

# Observability

## What it is
LLM usage observation tool — extracts standardized token usage from LangChain's `usage_metadata` and logs it. Implemented by `agent/observe.py`. Records 4 metrics: input tokens, cache hits, output tokens, reasoning tokens.

## Core Responsibilities
- **Standardized usage extraction**: reads from LangChain's cross-provider unified `usage_metadata` field, does not rely on each provider's inconsistent `response_metadata.token_usage`
- **Reasoning token tracking**: Anthropic thinking_tokens and OpenAI reasoning_tokens unified mapping to `reason` field
- **Model tagging**: `model` parameter written into event payload for `/api/stats/dashboard` windowed cost aggregation by model
- **Turn-by-turn reasoning decrease monitoring**: turn 1 has long reasoning, subsequent turns' reasoning is transparently echoed into input (cache hit), reasoning volume decreases monotonically

## Key Dependencies
- [[shared/lm/lm.ava.okf.md]] — LangChain `AIMessage.usage_metadata` is the data source
- [[gateway-cli.ava.okf.md]] — gateway stats dashboard consumes model info in logs for cost accounting

## Entry Points
- `agent/observe.py:log_llm_usage(msg, model)` — called after each LLM invocation

## Notes
- Log format uses `[bracket]` prefix convention for easy grep
- Cost calculation is not in the observe layer — pricing is the single source of truth in `shared/lm/pricing.py`
