---
type: doc
title: Durable LLM Usage
description: '`shared/lm/usage.py` — canonical durable usage events and matching billing spans for completed LLM calls.'
tags:
- shared
- library
- llm-inference
- billing
---

# Durable LLM Usage

`shared/lm/usage.py` turns completed LLM calls into durable `llm_usage` events and matching billing spans. It is the shared accounting boundary for both LangChain messages and provider-specific raw usage fields.

## Usage emitters

- `log_usage_from_message()` is the accounting path for completed LangChain messages; it records the `llm_usage` event, usage-time price snapshot (or `unpriced=1`), and its matching billing span.
- `log_usage_fields()` serves non-LangChain providers such as the Gemini embedding REST adapter. `for_agent_id` explicitly attributes a daemon-generated event to its target agent.
- `usage_kind` distinguishes agent, chat, batch, and embedding consumption. `source` is an optional payload field for shared text callers (`web.fetch`, `understand`, and `understand.media`) and is kept distinct from the transport provenance column.

## Notes

- Usage events keep price snapshots at the time of use so cost accounting remains stable when catalog rates later change.
- Calls without provider usage metadata emit nothing, except raw-field callers that intentionally account for a completed zero-token provider response.

- Key deps: [[lm.ava.okf.md]] (provider-layer overview) and [[pricing.ava.okf.md]] (price selection).
