-- Reverse of impersonation-relay-batch-window-default-zero: restore the 30s
-- column default (row values are untouched either way).

ALTER TABLE agent_impersonations
    ALTER COLUMN relay_batch_window_seconds SET DEFAULT 30;
