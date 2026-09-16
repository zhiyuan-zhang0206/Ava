-- The recovery circuit breaker's durable counter (task #3617, design #3610
-- section 12): consecutive PERMANENT-class provider rejections since the last
-- completed LLM turn. The second consecutive rejection with no successful turn
-- between halts every automatic recovery path for the agent -- event-path
-- resurrect, delivery-watchdog re-dispatch/retry, the stalled crash-marked
-- harvest op (A), and the relaxed reaper-marked trigger (T2) -- until a turn
-- succeeds; a manual resurrect stays exempt (explicit human override).
-- Cleared to 0 by the completed-turn UPDATE that clears last_turn_fatal_at
-- (agent/graph/_llm.py::_persist_last_active). See shared/recovery_breaker.py.
ALTER TABLE agents_meta
    ADD COLUMN permanent_reject_streak INTEGER NOT NULL DEFAULT 0
        CHECK (permanent_reject_streak >= 0);

-- Manual recovery:
-- UPDATE agents_meta SET permanent_reject_streak = 0 WHERE id = <agent_id>;
