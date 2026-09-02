-- Expand only. No legacy requests are reclassified or assigned a target.
ALTER TABLE inbound_messages
    ADD COLUMN IF NOT EXISTS target_generation UUID,
    ADD COLUMN IF NOT EXISTS target_owner UUID,
    ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE agents_meta ADD COLUMN IF NOT EXISTS lifecycle_command_id BIGINT;

-- Baseline replay is permitted; incompatible existing column types are not.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM (VALUES
            ('inbound_messages', 'target_generation', 'uuid'),
            ('inbound_messages', 'target_owner', 'uuid'),
            ('inbound_messages', 'applied_at', 'timestamp with time zone'),
            ('inbound_messages', 'observed_at', 'timestamp with time zone'),
            ('agents_meta', 'lifecycle_command_id', 'bigint')
        ) expected(table_name,column_name,type_name)
        LEFT JOIN pg_attribute a ON a.attrelid=expected.table_name::regclass
            AND a.attname=expected.column_name AND NOT a.attisdropped
        WHERE a.attname IS NULL OR format_type(a.atttypid,a.atttypmod)<>expected.type_name
            OR a.attnotnull
    ) THEN
        RAISE EXCEPTION 'incompatible lifecycle intent columns';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='inbound_messages'::regclass
        AND conname='inbound_agent_command_unique') THEN
        ALTER TABLE inbound_messages ADD CONSTRAINT inbound_agent_command_unique UNIQUE(agent_id,id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='inbound_messages'::regclass
        AND conname='inbound_lifecycle_target_check') THEN
        ALTER TABLE inbound_messages ADD CONSTRAINT inbound_lifecycle_target_check CHECK (
            (target_generation IS NULL AND target_owner IS NULL AND applied_at IS NULL AND observed_at IS NULL)
            OR (target_generation IS NOT NULL AND target_owner IS NOT NULL
                AND kind IN ('restart','terminate') AND claimed_at IS NOT NULL
                AND status IN ('claimed','done') AND (observed_at IS NULL OR applied_at IS NOT NULL))
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='agents_meta'::regclass
        AND conname='agents_meta_lifecycle_command_fk') THEN
        ALTER TABLE agents_meta ADD CONSTRAINT agents_meta_lifecycle_command_fk
            FOREIGN KEY(id,lifecycle_command_id) REFERENCES inbound_messages(agent_id,id);
    END IF;
END $$;
