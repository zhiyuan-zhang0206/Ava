-- Reversible: drop the display-only preset reference column.
ALTER TABLE agents_meta DROP COLUMN preset_name;
