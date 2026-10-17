---
type: doc
title: Memory Search — NumPy exact-search service
description: Local FastAPI service (19531) serving exact cosine search over an in-memory float32 matrix persisted as vectors.npz — the lightweight numpy backend behind MEIVA_MEMORY_SEARCH_BACKEND
tags: []
---

# Memory Search — NumPy Service

## What is it
A standalone local process (`python -m services.derived.memory_search.daemon`) serving
the memory search HTTP API on `127.0.0.1:19531` . The
indexer daemon and the gateway both dial it over HTTP, so the
numpy backend needs no cross-process shared state: this one process owns the
in-memory matrix (~2k rows x 3072 dims float32 ≈ 24MB) and its npz
persistence.

**Role attribution**: gateway side (pure agent-runner doesn't run) —
`ServiceSpec.capabilities=GATEWAY` in `services/supervision/roster/spec.py`.

## Core Responsibilities
- **Exact search**: one matrix product per query over every row, aggregated
  per path (the response lists `hits`: each file's best row as path, kind,
  chunk_idx and cosine score) — no approximate index, so its results are the reconciliation
  baseline
- **Persistence**: every mutation rewrites `$MEIVA_HOME/memory-search/vectors.npz`
  atomically (tmp file + rename) before acking — a kill-after-ack never loses
  a row; a fresh service loads the file at boot and the indexer's next patrol
  fills what disk says
- **Broken cache never raises**: a torn, foreign or width-mismatched npz loads
  empty instead of raising — the pool is the source of truth and the indexer's
  next patrol rebuilds it; a raise would make the supervisor respawn into the
  same broken file every 60 s. An absent `last_success_at` / `last_save_seconds`
  in an event means "not yet", never zero
- **Local binding**: only `127.0.0.1`, no LAN ports

## Key Dependencies
- [[memory-indexer.meiva.okf.md]] — writer (indexer daemon) and reader (gateway search)
- [[services/supervision/meiva_root_glue/docs/meiva_root_glue.meiva.okf.md]] — kept alive via `healthchecks/memory_search.py`

## Entry Points
- `services/derived/memory_search/daemon.py` — `.venv/bin/python -m services.derived.memory_search.daemon`
- `services/derived/memory_search/app.py:build_app()` — the FastAPI app (upsert / upsert_batch / delete / delete_stale_batch / meta / search / healthz); `delete_stale_batch` is the tail-cleanup companion to `upsert_batch` (issue #1946)

## Notes
- Port: `MEIVA_MEMORY_SEARCH_PORT` (default 19531), URI: `MEIVA_MEMORY_SEARCH_URI`
- Data dir: `MEIVA_MEMORY_SEARCH_DATA_DIR` (default `$MEIVA_HOME/memory-search/`)
- The daemon entry owns one `ConfigBoot`, builds its `MemorySearchConfig` slice from that owner and passes a live embedding-name reader to storage startup. The no-argument supervision probe is an operation root: it owns a cold `ConfigBoot` and supplies its URI and embedding-name reader to the HTTP helper.
- The store width/fingerprint and supervision search probe use `embeddings.factory.get_descriptor(name)` with the root's configured name; these metadata-only paths do not construct an embedding provider or load a model catalog. The root passes the descriptor width to `build_app(store, max_batch_rows, embedding_dim=...)`; each app owns its vector schema bounds, with no configuration read during module import.
- Selected via `MEIVA_MEMORY_SEARCH_BACKEND=numpy` (`services/derived/memory_indexer/backends/factory.py`)
- **Probe limitation (tracked)**: the healthcheck's POST /search probe carries
  no identity payload, so the `PORT_TAKEN` terminal verdict is unreachable for
  it — another unit's daemon answering a valid search payload would read as
  alive (no respawn attempted). The shared keepalive policy still bounds
  persistent failure via exponential backoff + the respawn breaker. Tracked
  limitation at single-unit scale, not a bug.
- **Growth boundary**: upsert copies the full matrix (`np.vstack`) and every
  mutation rewrites the whole npz, so cold-start rebuild is quadratic in rows
  — fine at the current pool scale (~2k rows, tens of seconds), not at 5k+;
  a bulk-upsert endpoint (batch rewrite, single save) is the known cure if the
  pool outgrows the single-digit-thousands range

## Process composition

The main retains its existing configuration boot owner and captures one loaded
image. Its lazy log database uses that owner's live dial slice and process gate;
the indexer's work handle and health use the same factory and image. The search
server adds no database startup requirement. Before hard exit each daemon stops
its owned pipeline with a two-second bound; an unfinished receipt is reported,
and cleanup failure retains a failing exit. The standalone reconciliation tool
captures its gate only after confirmation and a nonempty query pool.
