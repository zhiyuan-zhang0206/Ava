"""Schema evolution: migration layout, the applied-set runner, and release-bound authority.

``migration_layout`` validates the tracked `migrations/` directory shape (filename
format, up/down pairing, git-tracking); ``migrations`` is the applied-set runner
(apply pending, roll back, the schema-version mismatch check) built on it;
``migration_errors`` / ``migration_history`` hold its error hierarchy and the
frozen pre-reset generation inventories a convergence path checks before
deleting old tracking rows. ``runtime_migration`` is the release-bound
migration authority a prepared candidate's SQL must satisfy before an update
operation may write to the schema. ``rollback_snapshot`` names the
``*_backfill_*`` tables a finite migration rollback restores from.
"""
