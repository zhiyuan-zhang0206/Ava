---
type: doc
title: "CI data plane"
description: "Native database prerequisites, cached installation, and isolated clusters in GitHub Actions."
tags:
  - ci
  - infrastructure
---

# CI data plane

Each hosted job provisions its own toolchain: uv, Node 22, Postgres 17 + Redis 8.2 +
PgBouncer server binaries on PATH, and Playwright chromium. The suite spins
throwaway native clusters and poolers itself (`tests/_containers.py` and the
PgBouncer wire fixtures).

Postgres/Redis/PgBouncer installation lives in the composite action
`actions/install-pg-redis`, with cached apt archives and timeout+retry. Its
versioned cache includes the pooler's dependency closure. An executable
PgBouncer probe follows installation, including the archive-only fallback, so a
missing pooler fails setup instead of silently skipping the wire/capacity tests.
PgBouncer is pinned to an exact pgdg version (the one `scripts/provision/database.sh`
installs and holds on Linux hosts) and setup fails on any other: its releases
change pooled-session semantics the wire fixtures assert, so moving the pin is a
dependency upgrade that needs the maintainer's approval.

Redis comes from the redis.io apt repo at the 8.2 series production installs and
holds (`base.host.brew_pin.REDIS_APT_VERSION`); the action fails when another
series ends up installed. The Postgres server a test cluster runs is the
vendored zonky tree production vendors (`runtime_binaries`), built once per job
by `actions/vendor-pg-runtime` (`scripts/ci/vendor_pg_runtime.py`, after the
Python deps, cached on the pin file's hash) and linked into each session's
temp home by `tests/fixtures/env_bootstrap.py` via `CI_VENDORED_RUNTIME_ROOT`.
PGDG keeps only its newest 17.x, so no apt pin could match; the apt
`postgresql-17` stays for the host tools the vendored tree lacks.

Both `backend` and `e2e` point `AVA_DB_URL` / `AVA_REDIS_URL` at unreachable
sentinel ports: the suite must provision its own throwaway cluster, and a
sentinel turns "a test quietly reached a real data plane" into a connection
error instead of a silent pass.
