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

The positive margin gives the gateway an opportunity to return its modeled
failure before the client's read timeout. It does not guarantee response arrival
under arbitrary network or scheduling delay. Historical outage timings do not
define the current timeout contract.

## Request budget

Retry eligibility belongs to the transport's modeled response contract. An
unknown 500 is exposed once; the removed blanket claim that every transient 5xx
is retried was broader than the implementation. A caller with its own exhausted
budget can request one attempt. Purpose selects the server's ranking question;
exclusions are sorted in the request and omitted when empty.

## Result projection

Each wire result becomes one MemorySearchResult carrying path, description, tuple-valued tags, float-valued score and chunk in server order.

Paths are relative to the memory pool. Descriptions and tags come from the
server's note metadata; the client does not synthesize descriptions or rerank
chunks. Consumers share this projection instead of interpreting wire fields
independently.
