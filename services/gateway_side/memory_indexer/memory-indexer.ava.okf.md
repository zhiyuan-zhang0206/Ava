---
type: doc
title: Memory-Indexer — Memory Pool Vector Index
description: Semantic indexing daemon for the memory pool — monitors `**/*.md` file changes under `gateway_memory_dir()` (combined machine `$AVA_HOME/gateway/memory`), uses Gemini embedding
tags: []
---

# Memory-Indexer — Memory Pool Vector Index

## What is it
Semantic indexing daemon for the memory pool — monitors `**/*.md` file changes under `gateway_memory_dir()` (combined machine `$AVA_HOME/gateway/memory`), uses the configured embedding provider (Gemini Embedding 2 by default, `AVA_EMBEDDING_BACKEND`) to vectorize markdown files, stores them in the selected memory search backend (numpy by default; pgvector and milvus behind the same `AVA_MEMORY_SEARCH_BACKEND` switch), supporting semantic search via `ava.memory.search()`.

**Role attribution**: gateway side (pure agent-runner doesn't run) — `ServiceSpec.capabilities=_GATEWAY` in `ops/spec.py`, roster derived by `services_for_capabilities` intersecting with the local machine's `machine_role()`.

## Core Responsibilities
- **Cold-start full scan**: at startup diff memory pool files against the selected backend index, embed new/changed files, purge deleted entries — worked in file-granular chunks that beat daemon liveness between chunks
- **Batch liveness**: `_process_paths` beats at entry and before every embed, delete, stale-row cleanup and upsert, on both success and partial failure. The ceiling is `max(180s, provider worst batch + 30s)`. The factory pairs each provider with its budget: Gemini allows twice the configured timeout per sync attempt (deadline checked between chunks plus one read window), retry backoff, capped Retry-After and both jitter terms; async attempts use cancellation deadlines. Defaults: 846s batch / 876s ceiling. Beats prevent multiple embeds, commit tails or delete-only batches accumulating past the ceiling; NumPy's 300s upsert fits independently. The margin covers scheduling, local processing and loop resumption; the floor preserves other branches' slack. True wedges take longer to detect: false kills cost a rebuild, delayed detection costs staleness while search reads the existing index.
- **Reconcile recovery**: incomplete reconciles and failed drain batches trigger follow-up passes with bounded exponential backoff (config: base 60s, cap 15min) until the index is complete. This closes the restart-only gap after the 2026-09-20 429-truncated rebuild (task #4097). `/healthz` reports `reconcile_pending` / `reconcile_retry_in_s` (informational, non-gating); liveness beats continue during the wait
- **File monitoring**: watchdog Observer monitors fs events, pushes dirty paths into queue
- **Incremental indexing**: main loop drains the queue every second (set dedup), batch embed + upsert / delete
- **Package layout**: `daemon.py` (main loop), `embeddings/` (provider contract + Gemini adapter + factory switch `AVA_EMBEDDING_BACKEND`), `backends/` (milvus / numpy / pgvector storage behind `AVA_MEMORY_SEARCH_BACKEND`)

## Key Dependencies
- [[services/gateway_side/milvus.ava.okf.md]] — milvus vector storage backend, the rollback path (`http://127.0.0.1:19530`)
- `services/memory_indexer/backends/pgvector.py` — pgvector backend over the cluster Postgres (**v2 / fallback-only** — see Notes)
- `services/memory_indexer/backends/numpy.py` — numpy backend over the local exact-search service (19531)
- [[shared/lm/lm.ava.okf.md]] — Gemini API (`GEMINI_API_KEY`)
- [[gateway-cli.ava.okf.md]] — Gateway hosts this service

## Entry Points
- `services/memory_indexer/daemon.py` — main loop entry
- `services/memory_indexer/embeddings/factory.py:get_provider()` — provider switch (`AVA_EMBEDDING_BACKEND`)
- `services/memory_indexer/embeddings/gemini.py:GeminiEmbeddingProvider` — Gemini Embedding 2 adapter (batch + query embeds)
- `services/memory_indexer/backends/factory.py:get_backend()` — backend switch (`AVA_MEMORY_SEARCH_BACKEND`)
- `services/memory_indexer/backends/milvus.py` — milvus storage primitives + `MilvusBackend`
- `services/memory_indexer/backends/pgvector.py` — `PGVectorBackend` (table `memory_embeddings` in the cluster Postgres, exact scan, no row cap)

## Notes
- The index uses the gateway's **consolidated checkout** (`gateway_memory_dir()`, combined machine = `$AVA_HOME/gateway/memory`, main branch), which is **deliberately separate** from the agent-runner's **authoring checkout** (`memory_dir()` = `$AVA_HOME/memory`, machine-`<name>` branch) (`daemon.py:49`, `shared/paths.py:125-135`); on gateway-only units the two are the same
- Milvus collection: `memory_embeddings`, one row per **chunk** — `pk` (VARCHAR 2048, folds `{path}\x1f{kind}\x1f{chunk_idx}` — milvus-lite 3.x has no composite primary keys), `path` (VARCHAR 1024), `kind` (`desc` = frontmatter description / `body` = body chunk), `chunk_idx` (INT64), `mtime` (DOUBLE, cold-start reconcile), `content_hash` (VARCHAR 128, secondary gate when mtime changes but content hasn't), `embedder` (VARCHAR 64, the provider fingerprint the row was built with), `vector` (FLOAT_VECTOR COSINE AUTOINDEX, width = the provider's dim)
- A file indexes as 0-or-1 `desc` row + N `body` chunks (~1800 chars each, ~200-char overlap, paragraph-boundary aware) — embedding the description on its own keeps short entity-bearing frontmatter lines searchable, which a single whole-file embedding diluted (entity queries like "handoff to 402" used to miss)
- **Full-file replacement** (issue #1946): a re-embedded file commits as one unit — after every row of the file is embedded, the new rows upsert and the rows the current content no longer produces (removed description, shrunken body tail, emptied file) are deleted via `delete_stale_rows` (kind -> current row count; the indexer writes contiguous chunk_idx from 0, so any row past its kind's count is a stale tail). The tail delete runs BEFORE the upsert: if the upsert then fails, the surviving rows still carry the old content hash, so `all_meta` keeps disagreeing with the file and the next fs event / reconcile re-embeds it — the reverse order would leave the tail while the new mtime/hash make the file look current (the reconcile-blind state this issue is about). A file whose embedding fails part way keeps its old rows intact (consistent all-old state; the mismatching hash retriggers it) and only fully-embedded sibling files commit
- `search_topk` retrieves top-200 raw chunk hits, aggregates per path (best cosine wins), returns top-k paths — caller-facing semantics unchanged
- Schema mismatch (legacy path-PK collection, missing `embedder` column, or a vector width other than the configured provider's dim) is detected at connect: writable indexer-daemon connections drop + recreate the collection, then cold-start rebuilds the index chunked; read-only reconcile-tool connections refuse a missing or mismatched collection without persistent mutation
- **Backend provisioning and availability**: [[services/gateway_side/memory_indexer/backend-provisioning.ava.okf.md]]
- The Gemini adapter does not create a module-level `genai.Client` — tests replace the HTTP call (`httpx.post` / `httpx.AsyncClient`) via monkeypatch
- **Two retry policies, split by call site** (`embeddings/gemini.py`): `_EMBED_POLICY` (4 attempts, 1→2→4→8s) for the daemon's document embeds — no caller deadline, resilience = the retry; `_QUERY_EMBED_POLICY` (2 attempts, 1s) for search query embeds, which run inside the gateway endpoint's own deadline and are retried by the caller, not the embed
- The gateway search endpoint sizes its query-embed concurrency gate from `AVA_MEMORY_SEARCH_MAX_CONCURRENCY` and fails a request fast (503, `indexer_unavailable`) when a gate permit is not free within `AVA_MEMORY_SEARCH_ACQUIRE_TIMEOUT_SECONDS` (~1s) — a congested gate degrades instead of queueing requests until the search deadline
