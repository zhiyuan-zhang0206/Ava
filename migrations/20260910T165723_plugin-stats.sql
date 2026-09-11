-- plugin_stats — the runtime values behind declared statistics-panel cards.
--
-- A plugin declares its cards in `contributions.ui.stats` (manifest: identity
-- and label only). The value is runtime data keyed by `(plugin, id)`, written
-- by the plugin's own code through `shared/plugin_stats.py` and read into
-- `GET /api/stats/dashboard` for the console to join against the declaration.
-- Upsert-only, last write wins: the table holds at most one row per card.
--
-- IF NOT EXISTS keeps the body idempotent on a fresh bootstrap, where
-- db/schema.sql already carries the new shape (migration smoke replays on
-- fresh).

CREATE TABLE IF NOT EXISTS plugin_stats (
    plugin      TEXT        NOT NULL,
    id          TEXT        NOT NULL,
    value       TEXT        NOT NULL,
    detail      TEXT,
    status      TEXT        NOT NULL DEFAULT 'ok'
                            CHECK (status IN ('ok', 'warn', 'error')),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT,
    PRIMARY KEY (plugin, id)
);

COMMENT ON TABLE plugin_stats IS
    'Runtime values behind declared statistics-panel cards (contributions.ui.stats): one upsert-only row per (plugin, id). A declared card with no row is the console''s empty state; a failed refresh writes status=error with the reason in detail.';

-- ava_runner surface: a plugin''s refresh code runs in the runner process
-- (agent-process hook or daemon) and upserts its own cards — INSERT+UPDATE+
-- SELECT, and no DELETE (a card that stops being reported keeps its last
-- value + updated_at, which is what makes staleness visible instead of the
-- row silently vanishing). Full grant pattern, including fresh bootstraps:
-- shared/cluster/provision.py ensure_runner_role.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT, UPDATE ON plugin_stats TO ava_runner;
    END IF;
END $$;
