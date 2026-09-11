-- The stranded-hold record: a maintenance hold that has lost its owner — the
-- shape a failed updater leg leaves behind (its run exited non-zero, the hold
-- was never released, and nothing is executing that could release it). The
-- pause controller declares it once the verdict has held past its grace; it
-- clears when the hold does. It exists so the alarm and every roster surface
-- can state the failure from the DB alone — the held host's own ops server is
-- typically down with it, so no live probe can carry the fact (task #3132).
ALTER TABLE host_deploy_state
    ADD COLUMN stranded_hold_since  TIMESTAMPTZ,
    ADD COLUMN stranded_hold_reason TEXT;

COMMENT ON COLUMN host_deploy_state.stranded_hold_since IS
    'When this host''s pause became a STRANDED maintenance hold — an ownerless '
    'hold left by a failed updater leg; NULL when no such record. Stamped once '
    'by the pause controller and preserved until the verdict clears (task #3132).';
COMMENT ON COLUMN host_deploy_state.stranded_hold_reason IS
    'The updater verdict that left the stranded hold (e.g. "updater exited '
    'rc=1"); display/alert context, never a judgment input (task #3132).';
