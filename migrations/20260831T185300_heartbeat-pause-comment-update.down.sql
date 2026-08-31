-- Restore the table comment used before the backoff reminder was removed.

COMMENT ON TABLE heartbeat_pause_log IS
    'Append-only heartbeat-pause trail: one row per ava.self.pause_heartbeat call. The previous-window lookup for the backoff reminder reads the latest row per agent; the telemetry `heartbeat_paused` event stays the display surface.';
