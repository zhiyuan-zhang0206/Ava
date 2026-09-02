-- Existing runtimes remain unknown. Only actual admission may assign ownership.
ALTER TABLE agents_meta
    ADD COLUMN IF NOT EXISTS runtime_generation UUID,
    ADD COLUMN IF NOT EXISTS runtime_kind TEXT CHECK (runtime_kind IN ('process', 'hosted')),
    ADD COLUMN IF NOT EXISTS runtime_owner UUID,
    ADD COLUMN IF NOT EXISTS runtime_protocol_version INTEGER NOT NULL DEFAULT 0
        CHECK (runtime_protocol_version >= 0);

-- IF NOT EXISTS supports baseline replay, not an incompatible preexisting shape.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM (VALUES
            ('runtime_generation', 'uuid', false),
            ('runtime_kind', 'text', false),
            ('runtime_owner', 'uuid', false),
            ('runtime_protocol_version', 'integer', true)
        ) AS expected(name, type_name, required)
        LEFT JOIN pg_attribute a ON a.attrelid = 'agents_meta'::regclass
            AND a.attname = expected.name AND NOT a.attisdropped
        WHERE a.attname IS NULL OR format_type(a.atttypid, a.atttypmod) <> expected.type_name
            OR a.attnotnull <> expected.required
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_attrdef d JOIN pg_attribute a
            ON a.attrelid = d.adrelid AND a.attnum = d.adnum
        WHERE d.adrelid = 'agents_meta'::regclass AND a.attname = 'runtime_protocol_version'
            AND pg_get_expr(d.adbin, d.adrelid) = '0'
    ) THEN
        RAISE EXCEPTION 'incompatible existing agent runtime incarnation columns';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'agents_meta'::regclass
            AND pg_get_constraintdef(oid) =
                'CHECK ((runtime_kind = ANY (ARRAY[''process''::text, ''hosted''::text])))'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'agents_meta'::regclass
            AND pg_get_constraintdef(oid) = 'CHECK ((runtime_protocol_version >= 0))'
    ) THEN
        RAISE EXCEPTION 'incompatible existing agent runtime incarnation constraints';
    END IF;
END $$;
