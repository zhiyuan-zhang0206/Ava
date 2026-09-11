ALTER TABLE host_deploy_state
    DROP COLUMN IF EXISTS stranded_hold_recovery_note,
    DROP COLUMN IF EXISTS stranded_hold_attempted_at,
    DROP COLUMN IF EXISTS stranded_hold_attempts;
