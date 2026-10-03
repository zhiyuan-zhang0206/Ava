-- The push watermark's primary key: the created_at of the newest item already
-- pushed to a chat. item_id is f"{msg_idx}.{block_idx}" — a POSITION in the
-- session's message list — so a compact truncates it and an item_id-only
-- watermark can strand above the whole live numbering: on 2026-10-03 two chats
-- (Telegram, Feishu) froze silently for hours, fresh perpetually empty
-- (#4932/#4933). created_at is monotone across the session's life; item_id
-- stays as the tie-break between one message's blocks and for rows written
-- before this column (NULL stamp = compare by item_id, recovered by the
-- daemon's rollback reset). Stored verbatim in the wire format so the daemon
-- compares the exact ISO string the timeline sent.
ALTER TABLE im_bridge_cursors ADD COLUMN IF NOT EXISTS push_created_at TEXT;

COMMENT ON COLUMN im_bridge_cursors.push_created_at IS
    'created_at of the newest pushed item (ISO-8601, wire format); primary push watermark, immune to post-compact item_id renumbering. NULL = row written before the column, compared by push_item_id alone.';
