-- Reverse: drop the runner's alerts grants, by the same set the up
-- re-affirmed. The table itself long predates this migration and stays.
-- Guarded on the role like the up.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        REVOKE SELECT, INSERT, UPDATE ON alerts FROM ava_runner;
    END IF;
END $$;
