---
type: doc
title: Deployment primitives
description: Home lifecycle, maintenance, schema, release, provenance and deployment state.
tags: [base]
---

# Deployment primitives

`base/deploy/` owns home lifecycle, maintenance, schema, release, provenance and deployment state.
Its component nodes describe the current contracts and implementation.

## Documented components

- [[base/deploy/lifecycle/docs/home_lifecycle_locks.ava.okf.md]] — Home Lifecycle Mutex.
- [[base/deploy/maintenance/docs/maintenance.ava.okf.md]] — Native pause and maintenance.
- [[base/deploy/release/docs/runtime_interpreter.ava.okf.md]] — Loaded-runtime interpreter binding.
- [[base/deploy/schema/docs/migrations.ava.okf.md]] — Schema Migrations (baseline + deltas).
- [[base/deploy/schema/docs/reset-generations.ava.okf.md]] — Schema reset generations.
- [[base/deploy/schema/docs/rollback_snapshot.ava.okf.md]] — Recovery Snapshot Convention.
- [[base/deploy/state/docs/state.ava.okf.md]] — Base — deploy state & liveness (R1).
