---
type: doc
title: LLM event payload semantics
description: Usage-time pricing, provider error identity and completed-compaction attribution.
tags: [contracts]
---

# LLM event payload semantics

## Usage time price snapshot

```ava-contract
id = "usage-time-price-snapshot"
kind = "invariant"
statement = "LLM usage events preserve the rates in force at the call and its computed USD cost; readers must not reprice history against the current registry. Calls with unknown prices omit the cost/rate snapshot and set unpriced, while calls is one on every metered call."
code = [
  "base/events/declarations/llm.py:LlmUsage",
]
tests = [
  "agent/llm/tests/test_llm_usage.py::test_price_snapshot_rides_payload",
  "agent/llm/tests/test_llm_usage.py::test_price_snapshot_absent_for_unpriced_model",
]
```

Rates are USD per million tokens. Optional cache-write subdivisions and their TTL-specific rates retain the call-time input cost breakdown. `calls` provides a per-agent/model counter independently of histograms whose views drop agent identity. Typed payload keys also bound queryable attributes; undeclared keys must not become query fragments.

## Provider error dimensions

```ava-contract
id = "provider-error-dimensions"
kind = "invariant"
statement = "billing is independent of the transient/permanent error class: exhausted credit can require human action in either class. error_type preserves the provider body error.type alone, while error.code may affect billing classification without replacing error_type."
code = [
  "base/events/declarations/llm.py:LlmProviderError",
]
tests = [
  "base/packages/tests/lm/providers/test_provider_errors.py::test_billing_is_independent_of_error_class",
  "base/packages/tests/lm/providers/test_provider_errors.py::test_billing_code_match_does_not_widen_the_reported_error_type",
]
```

Every classified failure, including a retried transient, is observable; `fatal` describes whether it aborted the turn. `vendor` is the model-provider key and `model` is the model in force. `provider` identifies the SDK package raising the exception, which multiple vendors may share; it cannot identify whose billing key needs attention.
