---
type: doc
title: Shared Libraries Entry Points
description: The shared-layer public entry points — model factory, pricing, agent-status enum, message kwargs reader, metrics report, bootstrap.
tags:
- shared
---

# Shared Libraries Entry Points

## Entry points

- `shared/lm/factory.py:build_chat_model` — dispatches to the appropriate LangChain chat model based on model name prefix
- `shared/lm/factory.py:validate_model_config` — model/key pre-check at spawn boundary
- `shared/lm/pricing.py:tally_tokens` / `cost_usd` — token usage and three-tier cost calculation
- `shared/agents.py:AgentStatus` — agent lifecycle status enum (RUNNING / IDLING / RESTARTING / TERMINATED, plus ops-only HIBERNATING)
- `shared/message_kwargs.py:read_ava_kwargs` — typed reading entry point for message `additional_kwargs`
- `shared/metrics_aggregate.py:build_report_from_aggregate` — assemble metrics report
- `shared/bootstrap.py` — system boot entry point


Parent: [[shared/shared.ava.okf.md|Shared Libraries]].
