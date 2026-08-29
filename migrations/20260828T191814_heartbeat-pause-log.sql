-- Per-agent heartbeat-pause history — the data source for the exponential
-- backoff reminder (user ruling 2026-08-29): ava.self.pause_heartbeat writes
-- one row per call; the next call reads the previous window from this table
-- and, when it repeats or shortens the window, buffers a reminder system note
-- that the exec node injects into the agent's conversation.
--
-- IF NOT EXISTS keeps the body idempotent on a fresh bootstrap, where
-- db/schema.sql already carries the new shape (migration smoke replays on
-- fresh).

CREATE TABLE IF NOT EXISTS heartbeat_pause_log (
    id          BIGSERIAL PRIMARY KEY,
    agent_id    BIGINT NOT NULL REFERENCES agents(id),
    duration_s  DOUBLE PRECISION NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE heartbeat_pause_log IS
    'Append-only heartbeat-pause trail: one row per ava.self.pause_heartbeat call. The previous-window lookup for the backoff reminder reads the latest row per agent; the telemetry `heartbeat_paused` event stays the display surface.';

CREATE INDEX IF NOT EXISTS heartbeat_pause_log_agent_created_idx
    ON heartbeat_pause_log (agent_id, created_at DESC, id DESC);

-- ava_runner surface for the pause trail (task #1932): the runner process
-- (ava.self.pause_heartbeat) SELECTs the previous window and INSERTs the new
-- row; the BIGSERIAL id draws from the owning sequence. UPDATE/DELETE stay
-- out — the trail is append-only and no runner path rewrites rows.
--
-- Gated on the role's existence: on a fresh bootstrap schema.sql runs (and
-- the migration smoke replays this file) BEFORE install birth creates
-- ava_runner — shared/cluster/provision.py's ensure_runner_role owns the
-- grant at birth (and re-affirms it on every `ava start` that applied a
-- migration). Existing clusters pick the grant up from the paired
-- 20260829T043200 migration, which is what trips that refresh.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT ON heartbeat_pause_log TO ava_runner;
        GRANT USAGE, SELECT ON SEQUENCE heartbeat_pause_log_id_seq TO ava_runner;
    END IF;
END $$;
