ALTER TABLE agents_meta
    DROP CONSTRAINT agents_meta_status_check;

ALTER TABLE agents_meta
    ADD CONSTRAINT agents_meta_status_check
    CHECK (status IN ('allocated', 'starting', 'running', 'idling', 'restarting', 'terminated', 'hibernating'));
