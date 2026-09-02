-- Never erase evidence which a running consumer may still rely upon.
-- An explicit verified retirement must clear it before rolling back this schema.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM deployment_state WHERE managed_writer_evidence IS NOT NULL) THEN
        RAISE EXCEPTION 'managed-writer evidence must be explicitly retired before rollback';
    END IF;
END
$$;

ALTER TABLE deployment_state DROP COLUMN IF EXISTS managed_writer_evidence;
