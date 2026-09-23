-- Restore schema shape; compact timestamps were unused and remain unknown.
ALTER TABLE agents_meta ADD COLUMN IF NOT EXISTS last_compact_at TIMESTAMPTZ;
ALTER TABLE deployment_state ADD COLUMN IF NOT EXISTS note TEXT;
-- The former settle sentence is fully reconstructible from the canonical field.
UPDATE deployment_state SET note = settle_note;
