-- Revert the work-failed text column bounds (task #2531).
ALTER TABLE work_failed_events
    ALTER COLUMN repo TYPE TEXT,
    ALTER COLUMN ref TYPE TEXT,
    ALTER COLUMN commit_sha TYPE TEXT,
    ALTER COLUMN summary TYPE TEXT,
    ALTER COLUMN dedup_key TYPE TEXT;
