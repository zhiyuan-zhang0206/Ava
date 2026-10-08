ALTER TABLE im_bridge_cursors ADD COLUMN push_account_id TEXT,
    ADD COLUMN push_initialized BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE im_bridge_cursors
    DROP CONSTRAINT im_bridge_cursors_push_pair,
    ADD CONSTRAINT im_bridge_cursors_push_pair CHECK (push_item_id IS NULL OR push_agent_id IS NOT NULL);

CREATE TABLE im_bridge_outbound_intents (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    agent_id BIGINT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('message', 'inbound')),
    source_id TEXT NOT NULL,
    block_idx INTEGER NOT NULL CHECK (block_idx >= 0),
    replay_id TEXT NOT NULL DEFAULT '',
    request JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'sending', 'sent', 'uncertain', 'failed')),
    attempt_id UUID,
    outcome_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE (channel, account_id, chat_id, agent_id, source_kind, source_id, block_idx, replay_id),
    CHECK ((status = 'queued') = (attempt_id IS NULL))
);
CREATE INDEX im_bridge_outbound_pending ON im_bridge_outbound_intents (id)
    WHERE status IN ('queued', 'sending');
COMMENT ON TABLE im_bridge_outbound_intents IS
    'Immutable timeline outbound acceptance; cursor advancement commits with intent insertion. Sending is persisted before provider calls; unresolved attempts are uncertain and never automatically replayed. No expiry.';
COMMENT ON COLUMN im_bridge_cursors.push_account_id IS
    'Nonsecret adapter account owning the accepted push cursor; legacy NULL binds once without backfill. Explicit switch replay alone may rebind another account.';

CREATE TABLE im_bridge_outbound_replays (
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    replay_id TEXT NOT NULL,
    switch_arg TEXT NOT NULL,
    agent_id BIGINT NOT NULL,
    intent_ids BIGINT[] NOT NULL,
    push_item_id TEXT,
    push_created_at TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, account_id, chat_id, replay_id)
);
COMMENT ON TABLE im_bridge_outbound_replays IS
    'Immutable switch-replay batch acceptance receipts; repeated platform invocations recover the original selected intent IDs and cursor. No expiry or foreign-key pin to delivery rows.';

COMMENT ON COLUMN im_bridge_cursors.push_agent_id IS
    'Canonical recipient selection after initialization/binding; push_item_id belongs to this agent and may be NULL after explicit empty acceptance or clear.';
COMMENT ON COLUMN im_bridge_cursors.push_initialized IS
    'Explicitly accepted initial history, including empty switch batches. FALSE legacy NULL positions remain held without implicit backfill.';
