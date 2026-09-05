-- Bound the work-failed webhook text columns (task #2531): repo/ref/
-- commit_sha/summary/dedup_key had no length limit, so an oversized payload
-- could inject megabyte-scale content into agent chat / task descriptions.
-- Application-side Field(max_length=...) rejects oversize input first; the
-- VARCHAR bounds are the durable backstop.
ALTER TABLE work_failed_events
    ALTER COLUMN repo TYPE VARCHAR(200),
    ALTER COLUMN ref TYPE VARCHAR(255),
    ALTER COLUMN commit_sha TYPE VARCHAR(64),
    ALTER COLUMN summary TYPE VARCHAR(2000),
    ALTER COLUMN dedup_key TYPE VARCHAR(255);
