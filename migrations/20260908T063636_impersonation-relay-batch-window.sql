-- Routine-message merge window: the relay coalesces routine inbox
-- arrivals (anything that is neither a user chat nor a cancel) into one hint
-- per window, so a burst of peer/system notifications cannot burn one host
-- turn each. 0 disables merging and keeps the pre-window behaviour (every
-- arrival hints immediately). Bounded at 300s so a misconfiguration cannot
-- stall delivery indefinitely.
ALTER TABLE agent_impersonations
    ADD COLUMN relay_batch_window_seconds INTEGER NOT NULL DEFAULT 30
        CONSTRAINT agent_impersonations_relay_batch_window_check
        CHECK (relay_batch_window_seconds BETWEEN 0 AND 300);
