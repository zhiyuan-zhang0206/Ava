---
type: doc
title: Memory Search Read Path — Retrieval and Decision-Model Ranking
description: How POST /api/memory/search ranks a query's chunks - vector top-k per file, chunk text restoration, one yes/no decision request per candidate, and the no-fallback failure rule.
---

# Memory Search Read Path

- **Retrieval**: `search_topk` retrieves the top-200 raw chunk hits, aggregates per path (best cosine wins) and returns the top-k files as `ChunkHit(path, kind, chunk_idx, score)` — the winning row's coordinates and its cosine similarity. The gateway asks for `max(k, memory_rerank_candidates)` files (default 50).
- **Chunk text**: the index stores vectors, not text. The gateway reads the note file and restores the row with `chunking.chunk_text`, which applies the indexer's own split rules. A hit whose file is gone, unreadable or no longer produces that row (the index lags the pool) is dropped before ranking.
- **Ranking**: `rerank.py` makes one decision request per candidate, concurrently (at most 25 in flight). The state is the query plus that one note (path, description, excerpt of at most 1800 characters); the single yes/no question asks whether the note is worth the agent reading. `purpose` on the request picks the wording: `recall` (the query is the recent conversation) or `search` (an agent's own query). Results are ordered by the probability of yes, ties keeping vector order. No text is generated.
- **Calibration**: the passive-recall threshold is a probability from this exact model (`jev-1.13.0`) and these exact templates, calibrated by the 2026-10-11 benchmark. The wording lives only in `rerank.py`; changing the model, a question or a state template means recalibrating `memory_recall_inject_threshold`.
- **Cost**: a search issues up to `memory_rerank_candidates` (50) decision requests, so two waves of about a second each on top of the embed and vector read.
- **Failure**: a failed decision call fails the search as `IndexerUnavailable` (wire 503) naming the model and the cause, and the gateway logs a warning. There is no fallback to cosine order: a silent downgrade would hide a broken ranking behind plausible-looking results. The whole request shares `memory_search_deadline_seconds`; the rerank runs outside the query-embed concurrency gate.
- **Usage**: each decision call writes one `llm_usage` row with `usage_kind=memory_rerank`.

## Key Dependencies

- [[services/docs/gateway_side/memory_indexer/memory-indexer.ava.okf.md]] — the indexer that writes the rows
- [[ava_builtins/plugins/ava_memory/docs/memory-api.ava.okf.md]] — the SDK surface and wire result
