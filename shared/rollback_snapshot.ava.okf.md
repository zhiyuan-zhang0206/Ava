---
type: doc
title: Rollback Snapshot Convention
description: Shared naming predicate for finite migration recovery tables and their archival retirement guard.
tags:
- shared
- migrations
---

# Rollback Snapshot Convention

`shared/rollback_snapshot.py` defines `is_rollback_snapshot_table`: a
`*_backfill_*` table is a finite migration recovery buffer, not durable
application state. `scripts/lint_migrations.py` applies the predicate to every
created migration table and requires a later forward `DROP TABLE IF EXISTS`
plan. The PITR archive CLI accepts the same predicate before it permits
archive, verification, or retirement.
