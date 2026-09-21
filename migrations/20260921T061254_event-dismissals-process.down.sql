-- Reverse of event-dismissals-process: restore the four-dimension index and
-- drop the column. Per-process dismissals have no home in the old unique
-- shape, so the newest active row of each class stays dismissed and the older
-- ones become history ('reopened') with their process kept in the note —
-- lossy by design, recorded, never silently dropped.
UPDATE event_dismissals AS older
SET status = 'reopened',
    reopened_at = now(),
    updated_at = now(),
    note = CASE
        WHEN older.note = '' THEN 'process:' || older.process
        ELSE older.note || ' (process:' || older.process || ')'
    END
WHERE older.status = 'dismissed'
  AND EXISTS (
      SELECT 1
      FROM event_dismissals AS newer
      WHERE newer.status = 'dismissed'
        AND newer.id > older.id
        AND newer.category = older.category
        AND newer.level = older.level
        AND newer.event_name = older.event_name
        AND newer.source = older.source
        AND newer.agent_id IS NOT DISTINCT FROM older.agent_id
  );

DROP INDEX IF EXISTS event_dismissals_one_active_class_idx;
CREATE UNIQUE INDEX IF NOT EXISTS event_dismissals_one_active_class_idx
    ON event_dismissals (category, level, event_name, source, agent_id)
    NULLS NOT DISTINCT
    WHERE status = 'dismissed';

ALTER TABLE event_dismissals DROP COLUMN IF EXISTS process;
