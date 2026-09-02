-- Never erase evidence which a running consumer may still rely upon.
-- An explicit verified retirement must clear it before rolling back this schema.
-- Serialize the check with both adoption and DROP; a read lock alone permits
-- evidence to commit between the check and ALTER's eventual exclusive lock.
LOCK TABLE deployment_state IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM deployment_state WHERE managed_writer_evidence IS NOT NULL) THEN
        RAISE EXCEPTION 'managed-writer evidence must be explicitly retired before rollback';
    END IF;
END
$$;

ALTER TABLE deployment_state DROP COLUMN IF EXISTS managed_writer_evidence;
