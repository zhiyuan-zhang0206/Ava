"""Schema evolution: migration layout and the applied-set runner.

``migration_layout`` validates the tracked `migrations/` directory shape (filename
format, git-tracking); ``migrations`` is the applied-set runner
(apply pending, the schema-version mismatch check) built on it;
``migration_errors`` / ``migration_history`` hold its error hierarchy and the
frozen pre-reset generation inventories a convergence path checks before
deleting old tracking rows. ``rollback_snapshot`` names the
``*_backfill_*`` tables that hold finite recovery data a migration set aside.
"""
