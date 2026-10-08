CREATE TABLE upload_delivery_batches (
    batch_id text PRIMARY KEY CHECK (batch_id ~ '^[0-9a-f]{32}$'),
    operation_key text NOT NULL UNIQUE,
    agent_id bigint NOT NULL CHECK (agent_id > 0),
    request_hash text NOT NULL CHECK (length(request_hash)=64),
    manifest jsonb NOT NULL,
    source_unit jsonb NOT NULL,
    target_unit jsonb NOT NULL,
    storage_machine text NOT NULL,
    storage_directory text NOT NULL,
    provenance jsonb NOT NULL,
    acceptance jsonb,
    ready_at timestamptz,
    state text NOT NULL DEFAULT 'receiving' CHECK (state IN ('receiving','pending','accepted','hold')),
    reason text,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    copy_proof jsonb,
    inbound_id bigint,
    outcome jsonb,
    CHECK ((state='accepted') = (inbound_id IS NOT NULL AND outcome IS NOT NULL)),
    CHECK ((ready_at IS NULL) = (acceptance IS NULL))
);
CREATE INDEX upload_delivery_due ON upload_delivery_batches (next_attempt_at, batch_id) WHERE state='pending';
CREATE TABLE upload_delivery_copies (
    batch_id text NOT NULL,
    unit_home text NOT NULL,
    agent_id bigint NOT NULL CHECK (agent_id > 0),
    request jsonb NOT NULL,
    manifest jsonb NOT NULL,
    storage_machine text NOT NULL,
    storage_directory text NOT NULL,
    ready_at timestamptz,
    PRIMARY KEY (batch_id, storage_machine, unit_home)
);
COMMENT ON TABLE upload_delivery_batches IS 'Retained source acceptance, physical receiving quota and original inbound outcome; no TTL or mutable target FK.';
COMMENT ON TABLE upload_delivery_copies IS 'Retained immutable copy intent and physical-root reservation; AVA_HOME is target identity, not quota partition.';
