---
type: doc
title: Gateway memory-search client
description: Explicit-context memory-search request budgets and wire-result projection.
tags: [sdk, memory, contracts]
---

# Gateway memory-search client

The client supplies memory search to passive recall, the memory SDK and write-time
deduplication. It receives an explicit context and uses the context's transport
inputs. The server owns retrieval and ranking; the client preserves result order.

## Default timeout

```ava-contract
id = "default-timeout"
kind = "invariant"
statement = "The default per-attempt HTTP timeout is the context's current memory-search deadline plus MEMORY_SEARCH_TIMEOUT_MARGIN_S."
code = ["base/agents/context/gateway_client/memory_search.py:memory_search_timeout"]
tests = ["ava/tests/namespaces/gateway_client/test_gateway_client.py::TestTransientHttpRetry::test_memory_search_dedicated_timeout_and_single_retry"]
```

The positive margin gives the gateway an opportunity to return its modeled
failure before the client's read timeout. It does not guarantee response arrival
under arbitrary network or scheduling delay. Historical outage timings do not
define the current timeout contract.

## Request budget

```ava-contract
id = "request-budget"
kind = "invariant"
statement = "An explicit timeout becomes the request's deadline_seconds and the HTTP timeout adds the margin. Without an explicit timeout the request omits deadline_seconds. attempts bounds transport attempts; the default is two."
code = ["base/agents/context/gateway_client/memory_search.py:memory_search"]
tests = [
  "ava/tests/namespaces/gateway_client/test_gateway_client.py::TestTransientHttpRetry::test_memory_search_caller_timeout_travels_in_the_request",
  "ava/tests/namespaces/gateway_client/test_gateway_client.py::TestTransientHttpRetry::test_memory_search_without_a_timeout_sends_no_deadline",
  "ava/tests/namespaces/gateway_client/test_gateway_client.py::TestTransientHttpRetry::test_memory_search_attempts_bound_the_retries",
  "ava/tests/namespaces/gateway_client/test_gateway_client.py::TestTransientHttpRetry::test_memory_search_unknown_500_is_exposed_once",
]
```

Retry eligibility belongs to the transport's modeled response contract. An
unknown 500 is exposed once; the removed blanket claim that every transient 5xx
is retried was broader than the implementation. A caller with its own exhausted
budget can request one attempt. Purpose selects the server's ranking question;
exclusions are sorted in the request and omitted when empty.

## Result projection

```ava-contract
id = "wire-results"
kind = "invariant"
statement = "Each wire result becomes one MemorySearchResult carrying path, description, tuple-valued tags, float-valued score and chunk in server order."
code = ["base/agents/context/gateway_client/memory_search.py:MemorySearchResult", "base/agents/context/gateway_client/memory_search.py:memory_search"]
tests = ["ava/tests/namespaces/gateway_client/test_gateway_client.py::TestTransientHttpRetry::test_memory_search_maps_the_wire_result"]
```

Paths are relative to the memory pool. Descriptions and tags come from the
server's note metadata; the client does not synthesize descriptions or rerank
chunks. Consumers share this projection instead of interpreting wire fields
independently.
