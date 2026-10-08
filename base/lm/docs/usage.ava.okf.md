---
type: doc
title: Durable LLM Usage
description: '`base/lm/usage.py` — canonical durable usage events and matching billing spans for completed LLM calls.'
tags:
- base
- library
- llm-inference
- billing
---

# Durable LLM Usage

`base/lm/usage.py` turns completed LLM calls into durable `llm_usage` events and matching billing spans. It is the shared accounting boundary for both LangChain messages and provider-specific raw usage fields.

## Usage emitters

- `log_usage_from_message()` is the accounting path for completed LangChain messages; it records the `llm_usage` event, usage-time price snapshot (or `unpriced=1`), and its matching billing span.
- `log_usage_fields()` serves non-LangChain providers such as the Gemini embedding REST adapter. `for_agent_id` explicitly attributes a daemon-generated event to its target agent.
- `usage_kind` distinguishes agent, chat, batch, and embedding consumption. `source` is an optional payload field for shared text callers (`web.fetch`, `understand`, and `understand.media`) and is kept distinct from the transport provenance column.
- `log_usage_from_message(..., stamp_message=True)` (the main-conversation `log_llm_usage`) also writes the event's figures to `msg.additional_kwargs["ava_usage"]`: one `quote`, one dict, two destinations.
- `cache_mechanism` / `cache_scope` are optional provenance labels for `cache_read`. Gemini with an explicit `cachedContent` attached reports ONLY the explicit block (implicit tail hits are billed but not reported), so callers that rode that path label the event `cache_mechanism=mixed, cache_scope=explicit_block` instead of letting the dashboard misread the share as full-prefix. Absent labels = unknown, never fabricated (task #2660).

## Notes

- Usage events keep price snapshots at the time of use so cost accounting remains stable when catalog rates later change.
- Cache-write counts (`cache_write_5m`, `cache_write_1h`) and declared rate snapshots (`price_write_5m`, `price_write_1h`) preserve TTL-specific creation costs. Writes are included in `in_total`, so they are deducted from ordinary input, priced once, and also recorded as additive billing-span attributes. Actual served Standard/Fast identity selects all rates together. Existing persisted cost snapshots are not rewritten.
- Calls without provider usage metadata emit nothing, except raw-field callers that intentionally account for a completed zero-token provider response.
- For a requested Fast ID, `usage_model()` reads the provider's actual service
  receipt before pricing: OpenAI `fast` / legacy `priority` and Claude `fast`
  keep the Fast ID; OpenAI `default` / Claude `standard` select the base ID.
  Missing or unknown receipts raise instead of inventing a premium charge.
  `llm_usage.model` records the served ID and `requested_model` preserves the
  selected ID. Compaction also passes the selected Ava ID rather than the
  client's wire model name.
- Cache and reasoning counts include LangChain's tier-prefixed detail keys,
  such as `priority_cache_read` and `priority_reasoning`.

- Key deps: [[lm.ava.okf.md]] (provider-layer overview) and [[pricing.ava.okf.md]] (price selection).

## Selecting usage scopes

LLM usage is attributed to agents, not Fleet task records. The independent
`ava-being-a-long-running-agent` [usage script](../../../ava_builtins/skills/coordination/ava-being-a-long-running-agent/references/usage.md)
selects IDs, windows, and birth ancestry. `base/telemetry/metrics/usage.py` owns its
read-only aggregation over retained events or lifetime ledger + tail. Threshold
notifications leave convergence, preservation, and handoff to the agent.
