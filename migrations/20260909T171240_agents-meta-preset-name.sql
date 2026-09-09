-- agents_meta.preset_name: the spawn-time preset reference for display.
-- config_overlay keeps the RESOLVED effective overlay (preset folded in); this
-- column only records WHICH preset supplied the base, so the inspector can show
-- "preset: X" plus the overlay fields that differ from the preset (diff display).
ALTER TABLE agents_meta ADD COLUMN preset_name TEXT;
