-- Keep the replayed migration history aligned with the squashed baseline.

COMMENT ON TABLE heartbeat_pause_log IS
    'Append-only heartbeat-pause trail: one row per ava.self.pause_heartbeat call. The telemetry `heartbeat_paused` event stays the display surface.';
