ALTER TABLE agents_meta
    DROP CONSTRAINT agents_meta_launch_failure_pair_check,
    DROP COLUMN last_launch_failure_at,
    DROP COLUMN last_launch_failure_reason,
    DROP COLUMN last_launch_attempt_id;
