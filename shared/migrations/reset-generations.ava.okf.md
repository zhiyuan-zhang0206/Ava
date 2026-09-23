---
type: doc
title: Schema reset generations
description: Frozen migration inventories guard partial restores, atomically replace tracking history, and enforce the current rollback floor.
tags:
- shared
- database
---

# Schema reset generations

The 2026-09-23 reset folds the 101 preceding migration pairs into the baseline
and removes their seed stamps. `shared/migration_history.py` retains that exact
inventory plus the earlier 59-name generation: older backup/PITR restores have
not been ruled out. Before convergence deletes tracking rows, either partial
generation raises `MigrationHistoryGap`. Without the current reset anchor, the
complete 101-name predecessor set is mandatory, including on baseline-only
restores. Fresh databases stamp the new anchor directly. The upgrade inserts
the anchor and deletes folded tracking rows in one transaction, so an
interruption preserves a retryable generation.

The reset anchor is also a rollback floor, enforced before any down in a batch.
An older release cannot safely replay the folded strict DDL. Restore a matching
pre-reset backup or fix forward when recovery would cross this boundary.

## Reversibility

The baseline is the **rollback floor**, so everything above it must be
reversible: every post-baseline migration ships a paired `.down.sql`.
`scripts/lint_migrations.py` statically enforces the filename format, name
uniqueness, up/down pairing, and that `db/schema.sql` stamps the sentinel. There
is deliberately **no** continuity / next-number / cross-branch-collision check —
timestamp names make those checks meaningless.

A lossy operation (drop column/table, destructive transform) goes
**expand-contract**: the drop is its own later migration, decoupled from the
commit that stopped using the data, so any single upgrade's migration set stays
reversible.

A rollback that would cross the baseline cannot be performed — resetting code
under a newer schema would put every daemon in `CodeBehindSchema`, so the
recovery path leaves code and schema consistent on the new revision for
fix-forward and alerts loudly instead.

## Key dependencies

- [[shared/migrations/migrations.ava.okf.md]] — baseline, applied-set runner, and migration authority
