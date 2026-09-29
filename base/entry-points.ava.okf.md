---
type: doc
title: Shared Libraries Entry Points
description: The shared-layer public entry points — model factory, pricing, agent-status enum, message kwargs reader, metrics report, bootstrap.
tags:
- shared
---

# Shared Libraries Entry Points

## Entry points

- `base/lm/factory.py:build_chat_model` — dispatches to the appropriate LangChain chat model based on model name prefix
- `base/lm/factory.py:validate_model_config` — model/key pre-check at spawn boundary
- `base/lm/pricing.py:tally_tokens` / `cost_usd` — token usage and three-tier cost calculation
- `base/agents/contract.py:AgentStatus` — agent lifecycle status enum (RUNNING / IDLING / RESTARTING / TERMINATED)
- `base/agents/messages/kwargs.py:read_ava_kwargs` — typed reading entry point for message `additional_kwargs`
- `base/telemetry/metrics/aggregate.py:build_report_from_aggregate` — assemble metrics report
- `base/host/env/bootstrap.py` — system boot entry point


Parent: [[base/base.ava.okf.md|Shared Libraries]].
