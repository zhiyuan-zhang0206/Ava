---
type: doc
title: Memory-Indexer — Memory Pool Vector Index
description: Semantic indexing daemon for the memory pool — patrols `**/*.md` under `gateway_memory_dir()` (combined machine `$AVA_HOME/gateway/memory`) every 15 minutes, uses Gemini embedding
tags: []
---

# Memory-Indexer — Memory Pool Vector Index

## What is it
Semantic indexing daemon for the memory pool — patrols `**/*.md` under `gateway_memory_dir()` (combined machine `$AVA_HOME/gateway/memory`) every `patrol_interval_s`, vectorizes them with the configured embedding provider (Gemini Embedding 2 by default, `AVA_EMBEDDING_BACKEND`) and stores them in the selected backend (numpy default; pgvector via `AVA_MEMORY_SEARCH_BACKEND`), backing `ava.memory.search()`.

**Role attribution**: gateway side (pure agent-runner doesn't run) — `ServiceSpec.capabilities=GATEWAY` in `supervision/roster/spec.py`, roster derived by `services_for_capabilities` intersecting with the local machine's `machine_role()`.

## Core Responsibilities
- **Periodic patrol (the only refresh path)**: every `patrol_interval_s` (15 minutes) diff the pool against the backend by **content hash vs stored record**, embed changes and purge deletions in file-granular chunks that beat liveness. The first pass is due immediately (a fresh backend rebuilds); there is no filesystem-event path. A pass logs its scan / compare / embed durations, warns when it outlives the interval, and never overlaps the next.
- **Bounded retry**: a failed pass retries after `retry_backoff_s` doubling, at most `retry_limit` times; then it is marked over-limit and returns to the normal interval. Over-limit only stops the fast retries — the paths stay pending, so a cleared failure still converges.
- **Batch liveness**: `_process_paths` beats at entry and before every embed, delete, stale-row cleanup and upsert, on both success and partial failure. The ceiling is `max(180s, provider worst batch + 30s)`. The factory pairs each provider with its budget. Each Gemini attempt, sync or async, runs under an asyncio cancellation deadline equal to the configured timeout; the budget includes retry backoff, capped Retry-After and both jitter terms. Defaults: 606s batch / 636s ceiling. Beats prevent multiple embeds, commit tails or delete-only batches accumulating past the ceiling; NumPy's 300s upsert fits independently.
- **Health lag**: `/healthz` carries `index` (`pending_paths` = not caught up, `consecutive_failures`, `over_limit`, `last_success_at`), and each pass emits one bounded `memory_index_patrol` telemetry event. Informational only — 200/503 stays with liveness, so an outage never becomes a restart loop.
- **Navigation files excluded**: `MEMORY.md` and each `index.md` never enter the index (exact basename, `excluded_basenames`); the next pass prunes their rows. Consolidation regenerates them (16% of pool changes over 90 days); they only list indexed notes.
- **Package layout**: `daemon.py` (main loop), `embeddings/` (provider contract + Gemini adapter + factory switch `AVA_EMBEDDING_BACKEND`), `backends/` (numpy / pgvector storage behind `AVA_MEMORY_SEARCH_BACKEND`)

## Key Dependencies
- `services/derived/memory_indexer/backends/pgvector.py` — pgvector backend over the cluster Postgres (**v2 / fallback-only** — see Notes)
- `services/derived/memory_indexer/backends/numpy.py` — numpy backend over the local exact-search service (19531)
- [[base/lm/docs/lm/lm.ava.okf.md]] — Gemini API (`GEMINI_API_KEY`)
- [[gateway-cli.ava.okf.md]] — Gateway hosts this service

## Entry Points
- `services/derived/memory_indexer/daemon.py` — main loop; `ConfigBoot` binds live inputs via `config.py`
- `services/derived/memory_indexer/embeddings/factory.py:get_provider()` — provider switch (`AVA_EMBEDDING_BACKEND`)
- `services/derived/memory_indexer/embeddings/gemini.py:GeminiEmbeddingProvider` — Gemini Embedding 2 adapter (batch + query embeds)
- `services/derived/memory_indexer/backends/factory.py:get_backend()` — backend switch (`AVA_MEMORY_SEARCH_BACKEND`)
- `services/derived/memory_indexer/backends/pgvector.py` — `PGVectorBackend` (table `memory_embeddings` in the cluster Postgres, exact scan, no row cap)

## Notes
- The index uses the gateway's **consolidated checkout** (`gateway_memory_dir()`, combined machine = `$AVA_HOME/gateway/memory`, main branch), which is **deliberately separate** from the agent-runner's **authoring checkout** (`memory_dir()` = `$AVA_HOME/memory`, machine-`<name>` branch); on gateway-only units the two are the same
- Rows are one per **chunk**: the folded `pk` (`{path}\x1f{kind}\x1f{chunk_idx}`), `path`, `kind` (`desc` = frontmatter description / `body` = body chunk), `chunk_idx`, `mtime` (stored, never compared), `content_hash` (the patrol compares it against the file's sha256), the provider fingerprint, and the vector (width = the provider's dim)
- A file indexes as 0-or-1 `desc` row + N `body` chunks (~1800 chars each, ~200-char overlap, paragraph-boundary aware) — a separate description row keeps short entity-bearing frontmatter lines searchable, which whole-file embedding diluted
- **Full-file replacement** (issue #1946): a re-embedded file commits as one unit — after every row of the file is embedded, the new rows upsert and the rows the current content no longer produces (removed description, shrunken body tail, emptied file) are deleted via `delete_stale_rows` (kind -> current row count; the indexer writes contiguous chunk_idx from 0, so any row past its kind's count is a stale tail). The tail delete runs BEFORE the upsert: if the upsert then fails, the surviving rows still carry the old content hash, so `all_meta` keeps disagreeing with the file and the next patrol re-embeds it — the reverse order would leave the tail while the new mtime/hash make the file look current (the state issue #1946 is about). A file whose embedding fails part way keeps its old rows intact (consistent all-old state; the next patrol retries it) and only fully-embedded sibling files commit
- **Read path**: best chunk per file, then decision-model ranking — [[services/docs/gateway_side/memory_indexer/memory-rerank.ava.okf.md]]
- A vector width other than the configured provider's dim is detected at connect/boot: the numpy store starts empty, and the pgvector table is rebuilt by `ava start`; the first patrol then refills the index
- **Backend provisioning and availability**: [[services/docs/gateway_side/memory_indexer/backend-provisioning.ava.okf.md]]
- Roots pass provider/backend names, catalog and narrow readers. Construction and empty batches read no key/timeout; each embedding operation captures both before retries. NumPy reads URI at connect/async search; pgvector ignores it. Gateway concurrency is fixed at gate creation; acquire/deadline budgets stay live. Metadata uses `factory.get_descriptor(name)` without constructing a provider. Unknown setup/request/accounting failures propagate without replaying successful HTTP calls; billing sink policy is unchanged.
- Gemini uses a fresh `httpx.AsyncClient` per sync attempt; loop shutdown leaves stalled DNS threads in the background. Sync/async calls classify error headers before reading bodies, preserving status and Retry-After. Cancellation covers headers, compressed bodies and framing.
- **Two retry policies, split by call site** (`embeddings/gemini.py`): `_EMBED_POLICY` (4 attempts, 1→2→4→8s) for the daemon's document embeds — no caller deadline, resilience = the retry; `_QUERY_EMBED_POLICY` (2 attempts, 1s) for search query embeds, which run inside the gateway endpoint's own deadline and are retried by the caller, not the embed
- The gateway search endpoint sizes its query-embed concurrency gate from `AVA_MEMORY_SEARCH_MAX_CONCURRENCY` and fails a request fast (503, `indexer_unavailable`) when a gate permit is not free within `AVA_MEMORY_SEARCH_ACQUIRE_TIMEOUT_SECONDS` (~1s) — a congested gate degrades instead of queueing requests until the search deadline
