---
type: doc
title: LLM Provider Errors
description: '`base/lm/errors.py` — the shared provider-error signal; the application applies no outbound concurrency cap.'
tags:
- base
- library
- llm-inference
---

# LLM Provider Errors

The application does not cap or queue outbound LLM calls. Rate limiting is the
provider's 429, coordinated by exponential-backoff retry: `invoke_response` in
`base/lm/call.py` for synchronous callers and `agent/graph/llm/_retry.py` for
the agent LLM node. Agent admission and database pool sizes do not resize
provider capacity; operators allocate the provider's account budget across
hosts and processes.

`base/lm/errors.py:emit_provider_error()` emits `llm_provider_error` for both
agent streams and synchronous SDK calls, so Grafana's provider-grouped HTTP 429
alert covers batch traffic as well as turns.

- Key deps: [[lm.ava.okf.md]] (provider-layer overview)
