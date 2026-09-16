-- Reverse of permanent-reject-streak: drop the counter. The value is
-- transient recovery state, safe to discard on rollback; the wake suppression
-- a tripped breaker may have written is bounded and expires on its own.
ALTER TABLE agents_meta
    DROP COLUMN IF EXISTS permanent_reject_streak;
