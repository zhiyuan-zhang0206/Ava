---
type: doc
title: "CI data plane"
description: "Native database prerequisites, cached installation, and isolated clusters in GitHub Actions."
tags:
  - ci
  - infrastructure
---

# CI data plane

Each hosted job provisions its own toolchain: uv, Node 22, Postgres 17 + Redis +
PgBouncer server binaries on PATH, and Playwright chromium. The suite spins
throwaway native clusters and poolers itself (`tests/_containers.py` and the
PgBouncer wire fixtures).

Postgres/Redis/PgBouncer installation lives in the composite action
`actions/install-pg-redis`, with cached apt archives and timeout+retry. Its
versioned cache includes the pooler's dependency closure. An executable
PgBouncer probe follows installation, including the archive-only fallback, so a
missing pooler fails setup instead of silently skipping the wire/capacity tests.

Both `backend` and `e2e` point `AVA_DB_URL` / `AVA_REDIS_URL` at unreachable
sentinel ports: the suite must provision its own throwaway cluster, and a
sentinel turns "a test quietly reached a real data plane" into a connection
error instead of a silent pass.
