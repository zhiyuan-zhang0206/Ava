---
type: doc
title: "Required whole-file backend shards"
description: "Fresh runner-local planning, fail-closed collection checks and native CI ownership for backend file shards."
tags:
- infrastructure
- testing
---

# Required whole-file backend shards

Each ordinary backend runner creates a fresh complete eligible plan from its
current checkout before starting four workers. Planning uses the same native
environment, filters, configuration overrides and duration input as execution.
Each worker collects only its group's files and checks the exact planned nodes
and declared fixture closure before test bodies. Planning errors, configuration
drift and collection/worker validation errors fail directly without fallback or
retry; ordinary test failures retain the existing retry and canary policy.
Duration measurement retries restore the committed input before executing the
same plan, and attempt reports remain distinct.

As with the previous independent pytest-split collections, grouping assumes
deterministic full collection across runners with identical source, dependencies,
configuration and timings. There is no shared planner job, discovery cache or
artifact dependency. Local plans, diagnostics and per-worker reports accompany
the existing native reports; uploads retain the non-gating transport policy.
Those artifacts alone do not certify cross-runner equivalence. The full manual
proof below supplies that migration evidence. Selection routing, serial/static
lanes, native JUnit verdicts and the combined coverage gate retain their owners.

Planning and migration evidence: [[file-shard-shadow.ava.okf.md]].
