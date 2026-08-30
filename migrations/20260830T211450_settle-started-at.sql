-- The settle hold's start time (C3, task #2189): the deploy lease converts
-- to a bounded settle hold via settle_update_lock, and the hold's DURATION is
-- exactly what the rollout telemetry was missing — settle was the one phase with
-- no timing record (rollout timing report §4.4). Written by settle_update_lock
-- (same statement that sets phase='settling'), cleared by every lease-clearing
-- transition, read by ops.deploy_window when it releases the hold early: the
-- release path prints [rollout-telemetry] with the elapsed seconds, computed
-- server-side (now() - settle_started_at) so cross-host clock skew never
-- distorts the number. NULL on every non-settle row and on rows that entered a
-- settle hold before this migration shipped (their duration is unknowable, and
-- the release path reports it as such rather than guessing).
--
-- IF NOT EXISTS keeps the body idempotent on a fresh bootstrap, where
-- db/schema.sql already carries the new shape (migration smoke replays every
-- migration on a fresh schema.sql database).

ALTER TABLE deployment_state ADD COLUMN IF NOT EXISTS settle_started_at TIMESTAMPTZ;
