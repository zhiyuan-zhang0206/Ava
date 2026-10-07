-- Both markers have no writers or consumers. Settle identity lives in settle_hosts.
ALTER TABLE agents_meta DROP COLUMN IF EXISTS last_compact_at;
ALTER TABLE deployment_state DROP COLUMN IF EXISTS note;
