-- Reverse: drop the runner's hierarchy_jobs INSERT surface. The table itself
-- stays (it predates this grant; 20260924T070003's down drops only the
-- breaker). Guarded on the role like the up.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        REVOKE INSERT ON hierarchy_jobs FROM ava_runner;
    END IF;
END $$;
