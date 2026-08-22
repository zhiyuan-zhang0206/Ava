UPDATE agents_meta
SET status = 'idling'
WHERE status IN ('allocated', 'starting');

ALTER TABLE agents_meta
    DROP CONSTRAINT agents_meta_status_check;

ALTER TABLE agents_meta
    ADD CONSTRAINT agents_meta_status_check
    CHECK (status IN ('running', 'idling', 'restarting', 'terminated', 'hibernating'));
