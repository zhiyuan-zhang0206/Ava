---
type: doc
title: Schema reset generations
description: Frozen migration inventories guard partial restores and atomically replace tracking history at the current reset anchor.
tags:
- base
- database
---

# Schema reset generations

The 2026-09-23 reset folds the 101 preceding migration pairs into the baseline
and removes their seed stamps. `base/deploy/schema/migration_history.py` retains that exact
inventory plus the earlier 59-name generation: older backup restores have
not been ruled out. Before convergence deletes tracking rows, either partial
generation raises `MigrationHistoryGap`. Without the current reset anchor, the
complete 101-name predecessor set is mandatory, including on baseline-only
restores. Fresh databases stamp the new anchor directly. The upgrade inserts
the anchor and deletes folded tracking rows in one transaction, so an
interruption preserves a retryable generation.

An older release cannot safely replay the folded strict DDL. Restore a matching
pre-reset backup or fix forward when recovery would cross this boundary.

## Discipline

There are no down migrations: a mistake is fixed forward by a new migration.
`scripts/content_lint/lint_migrations.py` statically enforces the filename format, name
uniqueness, that no migration already on main is modified, deleted or renamed, and
that `db/schema.sql` stamps the sentinel. There is deliberately **no** continuity /
next-number / cross-branch-collision check — timestamp names make those checks
meaningless.

A lossy operation (drop column/table, destructive transform) goes
**expand-contract**: the drop is its own later migration, decoupled from the
commit that stopped using the data.

## Key dependencies

- [[base/deploy/schema/docs/migrations.ava.okf.md]] — baseline, applied-set runner, and migration authority
