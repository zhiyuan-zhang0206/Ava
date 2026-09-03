-- Unknown for legacy incarnations; only actual fenced admission may establish
-- a complete empty resource set. Configuration payloads are not resource owners.
ALTER TABLE agents_meta ADD COLUMN IF NOT EXISTS incarnation_resources JSONB;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid='agents_meta'::regclass AND attname='incarnation_resources'
        AND atttypid='jsonb'::regtype AND NOT attnotnull AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION 'incompatible existing incarnation_resources column';
    END IF;
END;
$$;
