-- The roster's read model: the last status_probe of every roster-visible agent-runner,
-- written by the heartbeat service's liveness pass. The gateway's roster and machines
-- reads render from these rows instead of dialing every runner on each read (a
-- blackholed host cost the whole-table read its full dial budget, task #3507); an
-- explicit `fresh=true` read still dials. machine_probe stays the agent-liveness and
-- alerting state of the rollout targets only; this table also covers staging and
-- intentionally stopped hosts, which the roster shows but liveness does not judge.
--
--   observed_at / reachable / consecutive_failures   the latest attempt
--   status / status_at   the last ClusterStatus a probe returned (kept across one
--       failed attempt so a single dropped probe does not blank a row; NULL when the
--       last reachable answer did not validate as ClusterStatus)
--
-- IF NOT EXISTS keeps the migration replayable on a baseline that already carries it.
CREATE TABLE IF NOT EXISTS machine_status_snapshot (
    machine_name         TEXT PRIMARY KEY,
    observed_at          TIMESTAMPTZ NOT NULL,
    reachable            BOOLEAN NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    status               JSONB,
    status_at            TIMESTAMPTZ
);

COMMENT ON TABLE machine_status_snapshot IS
    'Roster read model: the last status_probe per roster-visible agent-runner, written by the heartbeat liveness pass. status holds the last ClusterStatus payload and status_at when it was probed.';
