---
type: doc
title: Memory Indexer Backend Provisioning
description: Availability and provisioning of the memory indexer's fallback pgvector backend.
---

# Memory Indexer Backend Provisioning

- **Backend status**: pgvector is **v2 / fallback-only** (2026-08-29 decision; numpy stays the default — switching is one env var + restart). Its provisioning landed 2026-08-30: converge injects the pinned pgvector 0.8.6 files (mac Homebrew bottle / Linux PGDG deb, sha256-pinned, fail-fast) into the vendored runtime Postgres (`~/.ava/runtime/pg/17.4.0`, the tree `ava start` actually runs), and `ava start` pre-creates the extension with the bootstrap-superuser connection — pgvector's `vector.control` (0.8.6) has no `trusted = true`, so no NOSUPERUSER role can install it. The gateway's source `ava start` then creates the `memory_embeddings` table (or drops and recreates a table with another width or column set) acting as the schema owner (a remote-managed plane: through its provider URL), when `AVA_MEMORY_SEARCH_BACKEND=pgvector`; every runtime `connect()` only validates it and refuses a missing or stale table. A retained-image start performs no DDL: after an embedding-width change the indexer refuses to connect until a source `ava start` prepares the table. The CI smoke job `backend-pgvector-smoke` gates injection → CREATE EXTENSION → query on Linux. Remaining fail-closed surfaces: Windows (Docker Postgres) ships no pgvector, and brew/apt/remote Postgres without the pgvector package — the preflight keeps naming the fix there; numpy is the default backend
