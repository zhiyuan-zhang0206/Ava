ALTER TABLE cluster_pin ADD COLUMN IF NOT EXISTS pending_known_good_sha TEXT;
ALTER TABLE cluster_pin ADD COLUMN IF NOT EXISTS pending_known_good_at TIMESTAMPTZ;
