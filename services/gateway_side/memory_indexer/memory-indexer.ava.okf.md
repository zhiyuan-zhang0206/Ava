---
type: doc
title: Memory-Indexer — Memory Pool Vector Index
description: Semantic indexing daemon for the memory pool — monitors `**/*.md` file changes under `gateway_memory_dir()` (combined machine `$AVA_HOME/gateway/memory`), uses Gemini embedding
tags: []
---

# Memory-Indexer — Memory Pool Vector Index

## What is it
Semantic indexing daemon for the memory pool — monitors `**/*.md` file changes under `gateway_memory_dir()` (combined machine `$AVA_HOME/gateway/memory`), uses Gemini embedding to vectorize markdown files, stores them in the selected memory search backend (milvus by default; pgvector and numpy behind the same `AVA_MEMORY_SEARCH_BACKEND` switch), supporting semantic search via `ava.memory.search()`.

**Role attribution**: gateway side (pure agent-runner doesn't run) — `ServiceSpec.capabilities=_GATEWAY` in `ops/spec.py`, roster derived by `services_for_capabilities` intersecting with the local machine's `machine_role()`.

## Core Responsibilities
- **Cold-start full scan**: at startup diff memory pool files against Milvus index, embed new/changed files, purge deleted entries
- **File monitoring**: watchdog Observer monitors fs events, pushes dirty paths into queue
- **Incremental indexing**: main loop drains the queue every second (set dedup), batch embed + upsert / delete
- **Three-file architecture**: `daemon.py` (main loop), `embedder.py` (Gemini embedding client), `index.py` (Milvus index operations)

## Key Dependencies
- [[services/gateway_side/milvus.ava.okf.md]] — default vector storage backend (`http://127.0.0.1:19530`)
- `services/memory_indexer/backends/pgvector.py` — pgvector backend over the cluster Postgres (**v2 / fallback-only** — see Notes)
- `services/memory_indexer/backends/numpy.py` — numpy backend over the local exact-search service (19531)
- [[shared/lm/lm.ava.okf.md]] — Gemini API (`GEMINI_API_KEY`)
- [[gateway-cli.ava.okf.md]] — Gateway hosts this service

## Entry Points
- `services/memory_indexer/daemon.py` — main loop entry
- `services/memory_indexer/embedder.py:embed_documents()` — batch embedding
- `services/memory_indexer/backends/factory.py:get_backend()` — backend switch (`AVA_MEMORY_SEARCH_BACKEND`)
- `services/memory_indexer/backends/milvus.py` — milvus storage primitives + `MilvusBackend` (`services/memory_indexer/index.py` is a legacy compat shim)
- `services/memory_indexer/backends/pgvector.py` — `PGVectorBackend` (table `memory_embeddings` in the cluster Postgres, exact scan, no row cap)

## Notes
- The index uses the gateway's **consolidated checkout** (`gateway_memory_dir()`, combined machine = `$AVA_HOME/gateway/memory`, main branch), which is **deliberately separate** from the agent-runner's **authoring checkout** (`memory_dir()` = `$AVA_HOME/memory`, machine-`<name>` branch) (`daemon.py:49`, `shared/paths.py:125-135`); on gateway-only units the two are the same
- Milvus collection: `memory_embeddings`, one row per **chunk** — `pk` (VARCHAR 2048, folds `{path}\x1f{kind}\x1f{chunk_idx}` — milvus-lite 3.x has no composite primary keys), `path` (VARCHAR 1024), `kind` (`desc` = frontmatter description / `body` = body chunk), `chunk_idx` (INT64), `mtime` (DOUBLE, cold-start reconcile), `content_hash` (VARCHAR 128, secondary gate when mtime changes but content hasn't), `vector` (FLOAT_VECTOR COSINE AUTOINDEX)
- A file indexes as 0-or-1 `desc` row + N `body` chunks (~1800 chars each, ~200-char overlap, paragraph-boundary aware) — embedding the description on its own keeps short entity-bearing frontmatter lines searchable, which a single whole-file embedding diluted (entity queries like "handoff to 402" used to miss)
- `search_topk` retrieves top-200 raw chunk hits, aggregates per path (best cosine wins), returns top-k paths — caller-facing semantics unchanged
- Schema mismatch (legacy path-PK collection, or future drift) is detected at connect and the collection is dropped + recreated; cold-start then rebuilds the index chunked
- **Backend status** (2026-08-29 decision): pgvector is **v2 / fallback-only** until its provisioning lands on the vendored runtime Postgres (`~/.ava/runtime/pg/17.4.0`, the tree `ava start` actually runs) — `scripts/provision/database.sh` and the CI action only install the extension into brew/apt trees today, so the switch is fail-closed with a preflight error naming what to fix instead of a runtime 503 storm; numpy is the pilot path. Extra pgvector limits: Windows (Docker Postgres) ships no pgvector, so the switch fails closed there; brew's `vector.control` (0.8.6) has no `trusted = true`, so `CREATE EXTENSION` needs a superuser on NOSUPERUSER (split-runner) deployments
- embedder does not create a module-level `genai.Client` — tests replace module functions via monkeypatch
