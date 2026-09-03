-- Roll back only after stopping runtimes admitted by the new ownership protocol.
ALTER TABLE agents_meta
    DROP COLUMN runtime_protocol_version,
    DROP COLUMN runtime_owner,
    DROP COLUMN runtime_kind,
    DROP COLUMN runtime_generation;
