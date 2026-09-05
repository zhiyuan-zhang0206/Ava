-- failure-feedback: persist work failures for idempotent delivery and record
-- server-owned credential facts beside gateway-created inbound messages.
ALTER TABLE inbound_messages
    ADD COLUMN source_verified_by TEXT,
    ADD COLUMN source_transport TEXT,
    ADD COLUMN content_hash TEXT,
    ADD COLUMN source_assertion_match BOOLEAN;

CREATE TABLE work_failed_events (
    id                  BIGSERIAL PRIMARY KEY,
    repo                TEXT NOT NULL,
    ref                 TEXT NOT NULL,
    commit_sha          TEXT NOT NULL,
    stage               TEXT NOT NULL CHECK (stage IN ('ci', 'qa', 'merge')),
    summary             TEXT NOT NULL,
    author_agent_id     BIGINT NOT NULL CHECK (author_agent_id > 0),
    dedup_key           TEXT NOT NULL UNIQUE,
    delivered_to        TEXT,
    delivery_kind       TEXT CHECK (
        delivery_kind IN ('author', 'author_resurrected', 'delegator', 'task_alert')
    ),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at        TIMESTAMPTZ,
    CONSTRAINT work_failed_events_delivery_complete CHECK (
        (delivered_to IS NULL AND delivery_kind IS NULL AND delivered_at IS NULL)
        OR (delivered_to IS NOT NULL AND delivery_kind IS NOT NULL AND delivered_at IS NOT NULL)
    )
);

COMMENT ON TABLE work_failed_events IS
    'Idempotent CI, QA, and merge failure events routed to the author, nearest live delegator, or task registry.';

COMMENT ON COLUMN inbound_messages.source_verified_by IS
    'Server-owned credential identity that admitted the gateway inbound; NULL is unauthenticated or legacy.';

COMMENT ON COLUMN inbound_messages.source_transport IS
    'Server-owned ingress transport for a gateway inbound; NULL is legacy.';

COMMENT ON COLUMN inbound_messages.content_hash IS
    'Lowercase SHA-256 of inbound content at gateway persistence time; NULL is legacy.';

COMMENT ON COLUMN inbound_messages.source_assertion_match IS
    'Whether an agent:N source assertion matches a verified agent_token:M credential; NULL when either side is unknown. Informational only.';
