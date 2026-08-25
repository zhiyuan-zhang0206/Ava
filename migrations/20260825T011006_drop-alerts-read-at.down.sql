ALTER TABLE alerts ADD COLUMN read_at TIMESTAMPTZ;

CREATE INDEX alerts_unread_idx ON alerts (starts_at DESC) WHERE read_at IS NULL;
