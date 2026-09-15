-- Reverse: drop the runner's session-trail grants. The table itself stays
-- (20260913T180056's down drops it). Guarded on the role like the up.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        REVOKE SELECT, INSERT ON agent_impersonation_entries FROM ava_runner;
    END IF;
END $$;
