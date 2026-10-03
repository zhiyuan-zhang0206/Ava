# Memory search drops milvus; numpy stays the default and pgvector stays the fallback

## Context

The memory index had three interchangeable backends behind
`AVA_MEMORY_SEARCH_BACKEND`: a numpy exact-search service
(`services/memory_search/`), a milvus-lite server, and pgvector in the cluster
Postgres. numpy has been the default since 2026-09-02 and every production unit
runs it: the setting is unset on every machine, and `ava status` shows the milvus
session skipped. The pool is small (about 2k chunk rows of 3072 floats, roughly
24 MB), so one exact matrix product answers a query in microseconds.

The 2026-08-29 position, kept in
`services/docs/gateway_side/memory_indexer/backend-provisioning.ava.okf.md`, is that
pgvector is the **v2 / fallback** backend while numpy is the default: switching is
one env var and a restart, and pgvector's provisioning (the pinned extension files
injected into the vendored Postgres, the superuser pre-create, the owner-authority
table prepare, the CI smoke job) was built so that the fallback works on the
zero-manual-install path.

milvus has no such role. It needs a supervised ~1 GB `milvus-lite` process (gated
off by hand, then by a spec gate), a dependency tree of `pymilvus`, `milvus-lite`,
`faiss-cpu`, `pyarrow` and a pinned `setuptools`, a fixed port slot, a
session-scoped test server, and a probe, a health check and an ops branch, all for
an approximate index the pool size does not need.

## Decision

Delete the milvus backend and everything that existed only for it: the `milvus`
session (`services/milvus/`, its health check, roster entry, spec gate and
owned-service probe), the `milvus` slot of the fixed port table, the `AVA_MILVUS_*`
settings, the milvus test fixture and tests, and the `pymilvus[milvus-lite]`,
`milvus-lite` and `setuptools` dependencies. `memory_search_backend` stays with its
two values, `numpy` (default) and `pgvector`; the backend factory, the preflight
probe and the two-backend reconcile script stay with them.

## Alternatives rejected

- **Delete pgvector too and keep numpy alone.** Proposed first and rejected by the
  user: pgvector is the documented fallback, its provisioning and CI gate are in
  place, and a production database already carries the `vector` extension.
- **Keep milvus as the scale-out path.** An approximate index only pays above pool
  sizes this system is nowhere near, and an idle milvus-lite process, five
  transitive packages and a port slot are paid on every host regardless. If the pool
  outgrows the single-process numpy store, the scale answer is pgvector (which
  already exists behind the same switch) or a better store inside `memory_search`,
  not a second server.

## Consequences

- A gateway home's start intent still records the `milvus` port slot, and the fixed
  port table is closed, so the rollout needs the one-time runbook step ("retiring
  the `milvus` port slot") between `down` and `up`. A `.env` keeps its
  `AVA_MILVUS_*` keys, which are inert.
- Memory search keeps two backends to maintain; the pgvector path is still gated by
  its CI smoke job and the owner-authority tests.
