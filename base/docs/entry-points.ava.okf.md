---
type: doc
title: Base Library Entry Points
description: The base-layer public entry points — model factory, pricing, agent-status enum, message kwargs reader, metrics report, bootstrap.
tags:
- base
---

# Base Library Entry Points

## Entry points

- `base/lm/factory.py:build_chat_model` — dispatches to the appropriate LangChain chat model based on model name prefix
- `base/lm/factory.py:validate_model_config` — model/key pre-check at spawn boundary
- `base/lm/pricing.py:tally_tokens` / `cost_usd` — token usage and three-tier cost calculation
- `base/agents/contract.py:AgentStatus` — agent lifecycle status enum (RUNNING / IDLING / TERMINATED)
- `base/agents/messages/kwargs.py:read_ava_kwargs` — typed reading entry point for message `additional_kwargs`
- `base/telemetry/metrics/aggregate.py:build_report_from_aggregate` — assemble metrics report
- `base/host/env/bootstrap.py` — system boot entry point


Parent: [[base/docs/base.ava.okf.md|Base Library]].
