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

- [[base/deploy/lifecycle/docs/home_lifecycle_locks.meiva.okf.md]] — Home Lifecycle Mutex.
- [[base/deploy/lifecycle/docs/service-selection.meiva.okf.md]] — Durable service intent.
- [[base/deploy/maintenance/docs/maintenance.meiva.okf.md]] — Native pause and maintenance.
- [[base/deploy/release/docs/collector-artifact.meiva.okf.md]] — Collector artifact download.
- [[base/deploy/schema/docs/migrations.meiva.okf.md]] — Schema Migrations (baseline + deltas).
- [[base/deploy/schema/docs/reset-generations.meiva.okf.md]] — Schema reset generations.
- [[base/deploy/schema/docs/rollback_snapshot.meiva.okf.md]] — Recovery Snapshot Convention.
- [[base/deploy/state/docs/state.meiva.okf.md]] — Base — deploy state & liveness (R1).
