---
type: doc
title: Memory Search Read Path — Retrieval and Decision-Model Ranking
description: How POST /api/memory/search ranks a query's chunks - vector top-k per file, chunk text restoration, batched yes/no relevance questions to a decision model, and the no-fallback failure rule.
---

# Memory Search Read Path

- **Retrieval**: `search_topk` retrieves the top-200 raw chunk hits, aggregates per path (best cosine wins) and returns the top-k files as `ChunkHit(path, kind, chunk_idx, score)` — the winning row's coordinates and its cosine similarity. The gateway asks for `max(k, memory_rerank_candidates)` files (default 50).
- **Chunk text**: the index stores vectors, not text. The gateway reads the note file and restores the row with `chunking.chunk_text`, which applies the indexer's own split rules. A hit whose file is gone, unreadable or no longer produces that row (the index lags the pool) is dropped before ranking.
- **Ranking**: `rerank.py` asks the decision model `memory_rerank_model` one `YesNo` question per chunk — is this excerpt, with its note path and description, relevant to the query (the decision call's state) — and orders results by the probability of yes, ties keeping vector order. No text is generated.
- **Batching**: a decision call has input limits and the provider never truncates, so `rerank.py` splits the questions into requests that each fit the model's request limit and runs them concurrently. A single question that cannot fit alone is rejected by the call, not shortened.
- **Failure**: a failed decision call fails the search as `IndexerUnavailable` (wire 503) naming the model and the cause, and the gateway logs a warning. There is no fallback to cosine order: a silent downgrade would hide a broken ranking behind plausible-looking results. The whole request shares `memory_search_deadline_seconds`; the rerank runs outside the query-embed concurrency gate.
- **Usage**: each decision call writes one `llm_usage` row with `usage_kind=memory_rerank`.

## Key Dependencies

- [[services/docs/gateway_side/memory_indexer/memory-indexer.ava.okf.md]] — the indexer that writes the rows
- [[ava_builtins/plugins/ava_memory/docs/memory-api.ava.okf.md]] — the SDK surface and wire result
