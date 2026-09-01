ALTER TABLE agents_meta
    ADD COLUMN IF NOT EXISTS last_claim_loop_at TIMESTAMPTZ;
