ALTER TABLE agents_meta
    ADD COLUMN heartbeat_backoff_level INTEGER NOT NULL DEFAULT 0
        CHECK (heartbeat_backoff_level BETWEEN 0 AND 16);

COMMENT ON COLUMN agents_meta.heartbeat_backoff_level IS
    'Platform-side heartbeat nudge backoff (B7): consecutive no-op nudges raise this
     level, stretching the reminder interval to heartbeat_interval * 2^level (cap 24h).
     Reset to 0 by the daemon on real inbound or an agent pause. 0 = default cadence.';
