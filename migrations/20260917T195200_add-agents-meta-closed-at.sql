-- Closure marker: a termination the user marked as final ("close it — never
-- bring it back"). Non-NULL = closed: every automatic resurrection path skips
-- the agent (delivery chat, compact, the delivery watchdog's terminated-owner
-- retry, hosted-turn recovery); its queued work stays pending and dead-letters
-- on the existing thresholds. An explicit manual resurrect clears the column
-- (reopening, audited on the resurrect event). Stamped by every terminate path
-- carrying final=true; the first closure time is kept.
ALTER TABLE agents_meta ADD COLUMN closed_at TIMESTAMPTZ;

COMMENT ON COLUMN agents_meta.closed_at IS
    'When the user closed this agent: a termination that never auto-resurrects. '
    'Non-NULL = closed — every automatic resurrection path skips the agent and '
    'its queued work dead-letters on the existing thresholds; only an explicit '
    'manual resurrect clears the column (reopening, audited via the resurrect '
    'event payload). Stamped by every terminate path carrying final=true; keeps '
    'the first closure time. NULL = open.';
