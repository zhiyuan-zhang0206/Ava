CREATE INDEX agents_meta_live_roster_idx ON agents_meta (id) WHERE status <> 'terminated';
