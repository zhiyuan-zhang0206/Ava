-- The bounded automatic recovery of an update-armed stranded maintenance hold
-- (task #3142): the per-episode attempt budget and the latest outcome note, so
-- a host may complete the official stop/start/resume path once and then stop
-- trying. Cleared together with the stranded-hold record (the episode's own
-- lifecycle), so every episode starts with a fresh budget.
ALTER TABLE host_deploy_state
    ADD COLUMN stranded_hold_attempts      INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN stranded_hold_attempted_at  TIMESTAMPTZ,
    ADD COLUMN stranded_hold_recovery_note TEXT;

COMMENT ON COLUMN host_deploy_state.stranded_hold_attempts IS
    'Automatic recovery attempts this stranded-hold episode has consumed (task '
    '#3142); reset when the record clears.';
COMMENT ON COLUMN host_deploy_state.stranded_hold_attempted_at IS
    'Postgres timestamp of the last reserved automatic recovery attempt (task '
    '#3142).';
COMMENT ON COLUMN host_deploy_state.stranded_hold_recovery_note IS
    'The latest recovery attempt''s outcome or error summary, for the alarm '
    'text and the operator; display context, never a judgment input (task '
    '#3142).';
