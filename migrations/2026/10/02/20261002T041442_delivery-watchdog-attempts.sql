-- Durable per-agent attempt clocks and failure counts for the delivery
-- watchdog's three recovery loops (terminated-owner resurrect, stalled
-- crash-marked harvest, hosted-turn wedge recovery). They were process-local
-- dicts, so a restart zeroed every cooldown and every failure/suppression
-- escalation count; the loops claim an attempt here before acting instead.
--
--   claim:  INSERT ... ON CONFLICT DO UPDATE ... WHERE the cooldown elapsed
--           (services/delivery_watchdog/attempts.py::claim_attempts)
--   finish: UPDATE last_attempt_at when the attempt completes, so the
--           cooldown counts from the end of a slow RPC
--   resurrect only: consecutive_failures / suppress_count drive the
--           wake-suppression escalation ladder (resurrect_guard.py)
--
-- IF NOT EXISTS keeps the migration replayable on a baseline that already
-- carries the table.
CREATE TABLE IF NOT EXISTS delivery_watchdog_attempts (
    kind                 TEXT NOT NULL CHECK (kind IN ('resurrect', 'harvest', 'hosted_turn')),
    agent_id             BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    last_attempt_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    consecutive_failures INT NOT NULL DEFAULT 0,
    suppress_count       INT NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, agent_id)
);

COMMENT ON TABLE delivery_watchdog_attempts IS
    'Delivery watchdog recovery-loop state, one row per (loop kind, agent): last_attempt_at is the cooldown clock; consecutive_failures and suppress_count (resurrect only) are the wake-suppression escalation counters. Survives watchdog restarts.';
