-- One process dimension for the immutable event-class identity (task #4329 B5).
-- Every process's bare log lines collapsed into one class (category=log,
-- level, event_name=log, source=system), so a class-wide dismissal either hid
-- new incidents or was reopened by the ten-minute burst guard within seconds
-- (three attempts on 2026-09-05/06, burst counts 68-96). The emitting process
-- already rides every event body; grouping and dismissing by it makes an
-- operator dismissal targeted at the process whose known noise it accepts.
--
-- An empty ``process`` is a wildcard: it keeps every pre-dimension row's
-- class-wide meaning and also matches counted classes whose body has no
-- process (the mixed-version read).
ALTER TABLE event_dismissals
    ADD COLUMN IF NOT EXISTS process TEXT NOT NULL DEFAULT '';

-- The unique index grows the new dimension; the replaced shape cannot hold
-- two active per-process dismissals of one class.
DROP INDEX IF EXISTS event_dismissals_one_active_class_idx;
CREATE UNIQUE INDEX IF NOT EXISTS event_dismissals_one_active_class_idx
    ON event_dismissals (category, level, event_name, source, process, agent_id)
    NULLS NOT DISTINCT
    WHERE status = 'dismissed';
