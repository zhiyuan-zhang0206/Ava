ALTER TABLE inbound_messages
    ADD COLUMN IF NOT EXISTS client_message_id TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'inbound_messages'::regclass
          AND conname = 'inbound_messages_client_message_id_check'
    ) THEN
        ALTER TABLE inbound_messages
            ADD CONSTRAINT inbound_messages_client_message_id_check
            CHECK (
                client_message_id IS NULL
                OR char_length(client_message_id) BETWEEN 1 AND 128
            );
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_inbound_messages_client_message_id
    ON inbound_messages (client_message_id)
    WHERE client_message_id IS NOT NULL;

COMMENT ON COLUMN inbound_messages.client_message_id IS
    'Caller-generated id for one logical chat delivery. Non-NULL values are cluster-wide unique; same-id retries must match the original agent, content, source, kind, and payload.';
