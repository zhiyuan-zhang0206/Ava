-- Durable cadence clock for the slow phases of the ttl-reaper service: the
-- schedule_fire_log retention prune (daily), the torn lifecycle-pointer scan
-- and the absent-machine fence settle (hourly). The stamps were process-local
-- monotonic floats, so every restart ran all three at once; the service
-- claims a phase here before running it instead.
--
--   claim: INSERT ... ON CONFLICT DO UPDATE ... WHERE the interval elapsed
--          (services/ttl_reaper/cadence.py::claim_due)
--
-- IF NOT EXISTS keeps the migration replayable on a baseline that already
-- carries the table.
CREATE TABLE IF NOT EXISTS maintenance_state (
    kind        TEXT PRIMARY KEY,
    last_run_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE maintenance_state IS
    'ttl-reaper cadence clocks, one row per slow maintenance phase (kind): last_run_at is when the phase was last claimed. Survives service restarts.';
