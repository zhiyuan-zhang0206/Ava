---
type: doc
title: Recovery Snapshot Convention
description: Shared naming predicate for finite migration recovery tables and their archival retirement guard.
tags:
- base
- migrations
---

# Recovery Snapshot Convention

`base/deploy/schema/rollback_snapshot.py` defines `is_rollback_snapshot_table`: a
`*_backfill_*` table is a finite migration recovery buffer, not durable
application state. `scripts/content_lint/lint_migrations.py` applies the predicate to every
created migration table and requires a later forward `DROP TABLE IF EXISTS`
plan. A populated snapshot is dumped by hand before its retirement
(`.agents/skills/ava-guide/operations/references/db-restore.md`).
