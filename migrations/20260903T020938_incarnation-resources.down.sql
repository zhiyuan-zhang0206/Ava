-- Serialize with registration/admission before checking: checking first would
-- allow a resource writer to commit evidence while DROP waits for its lock.
LOCK TABLE agents_meta IN ACCESS EXCLUSIVE MODE;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM agents_meta WHERE incarnation_resources IS NOT NULL) THEN
        RAISE EXCEPTION 'Cannot retire incarnation resource evidence after use; verified writer retirement is required';
    END IF;
END;
$$;
ALTER TABLE agents_meta DROP COLUMN incarnation_resources;
