-- Reverse: drop the runner's pause-trail grants. The table itself stays
-- (the original migration's down drops it). Guarded on the role like the up.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        REVOKE SELECT, INSERT ON heartbeat_pause_log FROM ava_runner;
        REVOKE USAGE, SELECT ON SEQUENCE heartbeat_pause_log_id_seq FROM ava_runner;
    END IF;
END $$;
