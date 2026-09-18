-- Relay merge-window default 30 -> 0 (2026-09-18 impersonation review):
-- a new lease delivers routine arrivals immediately unless a window is chosen
-- explicitly. The mechanism is unchanged and per-lease (0..300 seconds); chat,
-- cancel and reminder arrivals never wait in any case. Existing rows keep
-- their recorded value -- only the column default moves, and db/schema.sql
-- carries the same default.

ALTER TABLE agent_impersonations
    ALTER COLUMN relay_batch_window_seconds SET DEFAULT 0;
