ALTER TABLE im_bridge_outbound_intents
    DROP CONSTRAINT im_bridge_outbound_intents_source_kind_check,
    ADD CONSTRAINT im_bridge_outbound_intents_source_kind_check
        CHECK (source_kind IN ('message', 'inbound', 'notice'));

CREATE TABLE im_bridge_notice_poll_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    legacy_floor BIGINT NOT NULL CHECK (legacy_floor >= 0),
    import_reason TEXT NOT NULL CHECK (import_reason IN ('legacy_cursor', 'no_history', 'legacy_history_unknown')),
    accepted_notice_id BIGINT NOT NULL DEFAULT 0 CHECK (accepted_notice_id >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE im_bridge_notice_poll_state IS
    'One-time normal Telegram notice cutover. The immutable legacy floor preserves imported skips or unknown old history; accepted_notice_id is diagnostic and never eligibility.';

CREATE TABLE im_bridge_notice_acceptances (
    notice_id BIGINT PRIMARY KEY CHECK (notice_id > 0),
    decision TEXT NOT NULL CHECK (decision IN ('queued', 'filtered')),
    request JSONB NOT NULL,
    intent_ids BIGINT[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((decision = 'filtered') = (cardinality(intent_ids) = 0))
);
COMMENT ON TABLE im_bridge_notice_acceptances IS
    'Normal-poll notice source receipts with immutable destination/rendering or deliberate filter decision. No foreign-key pin or expiry; explicit listing is a separate producer.';

COMMENT ON TABLE im_bridge_outbound_intents IS
    'Immutable IM timeline and normal notice intents. Producer acceptance commits with its receipt/cursor; sending is persisted before provider calls. Unresolved attempts are uncertain and never automatically replayed. No expiry.';
