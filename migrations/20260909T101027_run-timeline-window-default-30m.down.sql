-- Reverse the 2h -> 30m default-window migration. Rows at the migrated
-- 0.5 value go back to the old default; a row that was explicitly set to 0.5
-- before this migration cannot be distinguished and also reverts — accepted
-- best-effort reversal for a default-value migration.

UPDATE user_settings
SET value = '2'::jsonb
WHERE key = 'display.run_timeline_window_hours'
  AND value = '0.5'::jsonb;
