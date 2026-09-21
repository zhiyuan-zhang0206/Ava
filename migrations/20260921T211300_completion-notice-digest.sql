CREATE TABLE completion_notice_events (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('exit', 'missed')),
    exit_code INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    digest_inbound_id BIGINT REFERENCES inbound_messages(id),
    CONSTRAINT completion_notice_events_exit_code_check CHECK (
        (outcome = 'exit' AND exit_code IS NOT NULL) OR (outcome = 'missed' AND exit_code IS NULL)
    ),
    CONSTRAINT completion_notice_events_source_outcome_unique UNIQUE (agent_id, source, outcome)
);

CREATE INDEX completion_notice_events_pending_idx
    ON completion_notice_events (agent_id, created_at, id)
    WHERE digest_inbound_id IS NULL;

CREATE INDEX completion_notice_events_delivered_idx
    ON completion_notice_events (created_at)
    WHERE digest_inbound_id IS NOT NULL;

COMMENT ON TABLE completion_notice_events IS
    'Restart-safe hourly completion-notice buffer and canary conservation source. One row records each platform completion event admitted under an agent hourly policy; failure events remain individually delivered immediately and also appear in the hour count.';
