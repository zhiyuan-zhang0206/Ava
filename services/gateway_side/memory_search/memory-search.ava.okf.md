---
type: doc
title: Memory Search — NumPy exact-search service
description: Local FastAPI service (19531) serving exact cosine search over an in-memory float32 matrix persisted as vectors.npz — the lightweight numpy backend behind AVA_MEMORY_SEARCH_BACKEND
tags: []
---

# Memory Search — NumPy Service

## What is it
A standalone local process (`python -m services.memory_search.daemon`) serving
the memory search HTTP API on `127.0.0.1:19531` (one past milvus's 19530). The
indexer daemon and the gateway both dial it like the milvus daemon, so the
numpy backend needs no cross-process shared state: this one process owns the
in-memory matrix (~2k rows x 3072 dims float32 ≈ 24MB) and its npz
persistence.

**Role attribution**: gateway side (pure agent-runner doesn't run) —
`ServiceSpec.capabilities=_GATEWAY` in `ops/spec.py`.

## Core Responsibilities
- **Exact search**: one matrix product per query over every row, aggregated
  per path — no approximate index, so its results are the reconciliation
  baseline for the approximate backends (milvus)
- **Persistence**: every mutation rewrites `$AVA_HOME/memory-search/vectors.npz`
  atomically (tmp file + rename) before acking — a kill-after-ack never loses
  a row; a fresh service loads the file at boot and the indexer's cold-start
  reconcile fills what disk says
- **Local binding**: only `127.0.0.1`, no LAN ports

## Key Dependencies
- [[memory-indexer.ava.okf.md]] — writer (indexer daemon) and reader (gateway search)
- [[services/watchdog/watchdog.ava.okf.md]] — kept alive via `healthchecks/memory_search.py`

## Entry Points
- `services/memory_search/daemon.py` — `.venv/bin/python -m services.memory_search.daemon`
- `services/memory_search/app.py:build_app()` — the FastAPI app (upsert / delete / meta / search / healthz)

## Notes
- Port: `AVA_MEMORY_SEARCH_PORT` (default 19531), URI: `AVA_MEMORY_SEARCH_URI`
- Data dir: `AVA_MEMORY_SEARCH_DATA_DIR` (default `$AVA_HOME/memory-search/`)
- Selected via `AVA_MEMORY_SEARCH_BACKEND=numpy` (`services/memory_indexer/backends/factory.py`)
- **Growth boundary**: upsert copies the full matrix (`np.vstack`) and every
  mutation rewrites the whole npz, so cold-start rebuild is quadratic in rows
  — fine at the current pool scale (~2k rows, tens of seconds), not at 5k+;
  a bulk-upsert endpoint (batch rewrite, single save) is the known cure if the
  pool outgrows the single-digit-thousands range
