-- Existing runtimes remain unknown. Only actual admission may assign ownership.
ALTER TABLE agents_meta
    ADD COLUMN IF NOT EXISTS runtime_generation UUID,
    ADD COLUMN IF NOT EXISTS runtime_kind TEXT CHECK (runtime_kind IN ('process', 'hosted')),
    ADD COLUMN IF NOT EXISTS runtime_owner UUID,
    ADD COLUMN IF NOT EXISTS runtime_protocol_version INTEGER NOT NULL DEFAULT 0
        CHECK (runtime_protocol_version >= 0);
