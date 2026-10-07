-- Keep current cursor comments after replay-safe historical migrations refresh
-- their old comments over a fresh baseline. This metadata delta remains unseeded.
COMMENT ON COLUMN im_bridge_cursors.push_created_at IS
    'Timestamp of newest durably accepted timeline item, not proof of provider delivery. NULL legacy positions compare item_id alone.';
COMMENT ON TABLE im_bridge_cursors IS
    'Durable im-bridge selection and positions: push_* = timeline acceptance, poll_* = independently handled platform inbound. Acceptance and provider intents commit atomically; cursor advancement is not proof of delivery.';
