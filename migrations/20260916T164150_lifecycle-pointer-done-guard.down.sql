-- Reverses the guard only; the row repair in the up migration is intentionally not
-- reversed — restoring a torn pointer would recreate the incoherent state the guard
-- exists to prevent (the repaired rows stay settled, which is their correct shape).
DROP TRIGGER IF EXISTS inbound_messages_lifecycle_pointer_done_guard ON inbound_messages;
DROP FUNCTION IF EXISTS reject_inbound_done_with_lifecycle_pointer();
