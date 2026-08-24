-- Class-level resolution state for Loki's immutable event stream (task #1468).
-- A NULL agent_id means the dismissal covers every agent. `NULLS NOT DISTINCT`
-- keeps that all-agent class unique while the partial predicate preserves the
-- reopened history and permits a later fresh dismissal.
CREATE TABLE IF NOT EXISTS event_dismissals (
    id           BIGSERIAL PRIMARY KEY,
    category     TEXT NOT NULL,
    level        TEXT NOT NULL,
    event_name   TEXT NOT NULL,
    source       TEXT NOT NULL,
    agent_id     INTEGER,
    dismissed_by INTEGER NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'dismissed'
                 CHECK (status IN ('dismissed', 'reopened')),
    dismissed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reopened_at  TIMESTAMPTZ,
    burst_count  INTEGER,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN event_dismissals.dismissed_by IS
    'Acting agent id; 0 means a user or operator through the gateway UI/API, -1 is the auto-dismiss system.';

CREATE UNIQUE INDEX IF NOT EXISTS event_dismissals_one_active_class_idx
    ON event_dismissals (category, level, event_name, source, agent_id) NULLS NOT DISTINCT
    WHERE status = 'dismissed';
