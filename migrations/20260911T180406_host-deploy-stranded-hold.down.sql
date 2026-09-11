ALTER TABLE host_deploy_state
    DROP COLUMN IF EXISTS stranded_hold_reason,
    DROP COLUMN IF EXISTS stranded_hold_since;
