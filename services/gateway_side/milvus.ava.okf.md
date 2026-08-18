---
type: doc
title: Milvus — Vector Database
description: Milvus-lite standalone gRPC server wrapper — launches `milvus-lite server` via `os.execvp`, providing
tags: []
---

# Milvus — Vector Database

## What is it
Milvus-lite standalone gRPC server wrapper — launches `milvus-lite server` via `os.execvp`, providing vector storage and semantic search backend for memory-indexer. Binds only `127.0.0.1`, purely local service.

**Role attribution**: gateway side (pure agent-runner doesn't run) — `ServiceSpec.capabilities=_GATEWAY` in `ops/spec.py`, roster derived by `services_for_capabilities` intersecting with the local machine's `machine_role()`.

## Core Responsibilities
- **Vector storage**: stores Gemini embedding vectors, supports ANN search
- **Independent process**: runs in a dedicated `ava-milvus` session (`ops/spec.py` `ServiceSpec(session="milvus")` generated via `session_name` — under path-only cluster identity the session name no longer carries a cluster token; the per-home session namespace (`$AVA_HOME/run/sessions/`) already isolates clusters)
- **Local binding**: only `127.0.0.1:19530` (default), does not open LAN ports

## Key Dependencies
- [[memory-indexer.ava.okf.md]] — sole writer and reader
- [[services/watchdog/watchdog.ava.okf.md]] — kept alive via `healthchecks/milvus.py`

## Entry Points
- `services/milvus/daemon.py` — `.venv/bin/python -m services.milvus.daemon`

## Notes
- Data directory: `AVA_MILVUS_DATA_DIR` (default `~/.ava/milvus-data/`)
- Port: `AVA_MILVUS_PORT` (default 19530)
- The `index.py` layer abstracts the backend — switching between milvus-lite / full Milvus Docker / Zilliz Cloud only requires changing `AVA_MILVUS_URI`
