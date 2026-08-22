DROP INDEX IF EXISTS idx_inbound_messages_client_message_id;

ALTER TABLE inbound_messages
    DROP CONSTRAINT IF EXISTS inbound_messages_client_message_id_check,
    DROP COLUMN IF EXISTS client_message_id;
