-- Watcher scripts are frozen at spawn; the registry now records the
-- template generation a watcher was spawned with so the boot reconcile can
-- rebuild live cron watchers whose script predates a template fix (issue
-- #1330). NULL = legacy row (pre-versioning) — treated as stale. Idempotent:
-- the squashed baseline already carries the column, so the delta must be a
-- no-op when replayed on top of it.
ALTER TABLE agent_watchers ADD COLUMN IF NOT EXISTS template_version INTEGER;
