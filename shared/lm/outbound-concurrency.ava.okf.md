---
type: doc
title: LLM Outbound Concurrency
description: '`shared/lm/_concurrency.py` and `errors.py` — provider-call caps and the shared rate-limit signal.'
tags:
- shared
- library
- llm-inference
---

# LLM Outbound Concurrency

`LLMConcurrencyLimiter` holds one synchronous or asynchronous slot across the
entire provider call, including SDK-internal retries. Its default `deepseek:31`
limits asynchronous calls across the hosted process; synchronous calls have a
separate process-local limiter. Agent admission and database pool sizes do not
resize these caps. Operators allocate the provider's account budget across hosts
and processes; an explicit empty `AVA_LLM_MAX_CONCURRENT` disables caps.

`shared/lm/errors.py:emit_provider_error()` emits `llm_provider_error` for both
agent streams and synchronous SDK calls, so Grafana's provider-grouped HTTP 429
alert covers batch traffic as well as turns.

- Key deps: [[lm.ava.okf.md]] (provider-layer overview)
