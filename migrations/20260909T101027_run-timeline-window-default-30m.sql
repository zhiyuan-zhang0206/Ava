-- Run timeline default window 2h -> 30m (user ruling 2026-09-09, task #2591).
-- Only rows still carrying the OLD DEFAULT (2 hours) migrate; users who picked
-- an explicit window keep their value. The frontend default changes in the same
-- release, so rows holding 2 are exactly the "never changed the preset" set.

UPDATE user_settings
SET value = '0.5'::jsonb
WHERE key = 'display.run_timeline_window_hours'
  AND value = '2'::jsonb;
