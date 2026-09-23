ALTER TABLE agents_meta
    ADD COLUMN last_launch_attempt_id UUID,
    ADD COLUMN last_launch_failure_reason TEXT
        CHECK (last_launch_failure_reason IN
            ('launch_unreachable', 'launch_rejected', 'launch_unknown')),
    ADD COLUMN last_launch_failure_at TIMESTAMPTZ,
    ADD CONSTRAINT agents_meta_launch_failure_pair_check
        CHECK ((last_launch_failure_reason IS NULL) = (last_launch_failure_at IS NULL));
