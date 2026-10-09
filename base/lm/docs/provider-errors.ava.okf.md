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

Retry authority comes from official LangChain `ModelError.is_retryable` and
actual provider SDK error types. Known permanent rejections fail immediately;
unknown errors retain their original exception and traceback and are attempted
once. Matching `status_code`, `body` or `is_retryable` attributes on an unrelated
exception, or an arbitrary chained provider cause, do not grant retry authority.
Only an official model wrapper can supply metadata from its direct typed SDK
cause. For example, Google's generic non-`ModelError` HTTP 408 wrapper remains
unknown at the application layer; the SDK's internal retry policy is unchanged.

Raw HTTPX network and timeout errors are normalized only around the direct model
invoke or iterator await. A transport error from an output callback or another
service is unknown to the node's retry policy. Explicit stream stalls and the
single configured overload/cache recoveries keep their existing contracts.
Malformed terminal frames, unknown stop reasons and truncation fail once.
Compaction retries typed transient failures and its empty/short-summary checks;
an unknown programming error cannot trigger emergency trimming of history.

`base/lm/errors.py:emit_provider_error()` emits `llm_provider_error` for both
agent streams and synchronous SDK calls, so Grafana's provider-grouped HTTP 429
alert covers batch traffic as well as turns.

- Key deps: [[lm.ava.okf.md]] (provider-layer overview)
