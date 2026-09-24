-- The understanding-tree worker's regeneration circuit breaker (task #4674
-- guardrail): a singleton row recording the last trip and its operator reset,
-- so the 24h generated-node budget stops the worker persistently and resuming
-- is an explicit, auditable act.
--
--   trip (worker, services/hierarchy_worker/runner.py::_regen_budget_check):
--     INSERT ... ON CONFLICT (id) DO UPDATE ... (only while armed)
--   reset (operator):
--     UPDATE hierarchy_worker_breaker
--        SET reset_at = now(), reset_note = '<who/why>'
--      WHERE id = 1;
--   re-arm (worker): after a reset, the first window reading at or below
--     budget sets rearmed_at; only then may a new excursion trip again.
--
-- Active trip = reset_at IS NULL. The row exists only once a trip happened;
-- its absence (or a set rearmed_at) means armed.
CREATE TABLE hierarchy_worker_breaker (
    id             INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    tripped_at     TIMESTAMPTZ,
    tripped_reason TEXT,
    reset_at       TIMESTAMPTZ,
    reset_note     TEXT,
    rearmed_at     TIMESTAMPTZ
);

COMMENT ON TABLE hierarchy_worker_breaker IS
    'Regeneration circuit breaker (task #4674): singleton row; active trip = reset_at IS NULL; operators reset with reset_at + reset_note; re-arms (rearmed_at) only after a reset and a cooled window.';
