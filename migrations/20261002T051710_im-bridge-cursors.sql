-- im-bridge durable cursors. The push watermark (newest agent item already sent
-- to a chat) and Feishu's inbound poll cursor (the platform keeps no offset for
-- polling) were process memory, so a restart lost the position. One row per
-- (channel, chat_id) as the writing code names the chat: the push columns
-- belong to the chat a user talks in, the poll columns to the platform
-- conversation the poller lists; a row only ever carries one of the two.
CREATE TABLE IF NOT EXISTS im_bridge_cursors (
    channel         TEXT        NOT NULL,
    chat_id         TEXT        NOT NULL,
    push_agent_id   BIGINT,
    push_item_id    TEXT,
    poll_message_id TEXT,
    poll_create_ms  BIGINT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, chat_id),
    CONSTRAINT im_bridge_cursors_push_pair
        CHECK ((push_agent_id IS NULL) = (push_item_id IS NULL))
);

COMMENT ON TABLE im_bridge_cursors IS
    'Durable im-bridge positions: push_* = newest agent item pushed to the chat (for push_agent_id), poll_* = newest handled platform message of a polled conversation (message id, create time in ms; id NULL = time-only position). A restart resumes from here instead of losing what happened while the bridge was down.';
