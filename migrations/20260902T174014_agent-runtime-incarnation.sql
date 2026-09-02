-- Existing runtimes remain unknown. Only actual admission may assign ownership.
ALTER TABLE agents_meta
    ADD COLUMN runtime_generation UUID,
    ADD COLUMN runtime_kind TEXT CHECK (runtime_kind IN ('process', 'hosted')),
    ADD COLUMN runtime_owner UUID,
    ADD COLUMN runtime_protocol_version INTEGER NOT NULL DEFAULT 0
        CHECK (runtime_protocol_version >= 0);
