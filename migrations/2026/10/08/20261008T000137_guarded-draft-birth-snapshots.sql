-- Only new guarded drafts opt in; no guessed backfill of mutable birth history.
CREATE TABLE agent_creation_snapshots (
    creation_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    agent_id BIGINT NOT NULL,
    machine TEXT NOT NULL,
    config_overlay JSONB,
    birth_config JSONB NOT NULL,
    launch_attempt_id UUID NOT NULL,
    prompt_inbound_id BIGINT,
    prompt_content TEXT,
    prompt_source TEXT,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
