-- Retire the dead log-era event dismissals: their classes no longer arrive
-- with category = 'log' (task #4964).
--
-- Dismissal matching stops depending on the emission category in the same
-- change (services/events_maintenance/resolution.py), because the 2026-08
-- log -> telemetry reclassification of several events left whole rows inert.
-- These seven rows must not come back alive against the telemetry classes:
--
--   * sse_drop / db_pool_acquire_slow: the telemetry dismissal of each class
--     was deliberately reopened by the burst safety valve (ids 18/32/60/33/68)
--     while the stale log row stayed 'dismissed' — resurrecting it would
--     re-hide a class the valve surfaced;
--   * node_exit: its telemetry twin (id 1) keeps the dismissal, this row is
--     redundant;
--   * gateway_latency: no telemetry row exists; a future warning-level
--     occurrence must stay visible;
--   * loki_query_budget (x2) / send_message: no emitter exists in the source.
--
-- Verified on the production cluster as event_dismissals ids 2, 3, 4, 7, 10,
-- 12, 15 (all status 'dismissed', agent_id NULL, source 'system'): the
-- identity below matches exactly those rows. Reopening (not deleting) keeps
-- them in the ops review cycle's reopened history, the table's own state for
-- "this dismissal no longer applies".
UPDATE event_dismissals
SET status = 'reopened', reopened_at = now(), updated_at = now()
WHERE status = 'dismissed'
  AND agent_id IS NULL
  AND category = 'log'
  AND source = 'system'
  AND (level, event_name, process) IN (
      ('warning', 'node_exit', ''),
      ('warning', 'loki_query_budget', ''),
      ('error', 'loki_query_budget', ''),
      ('warning', 'sse_drop', ''),
      ('warning', 'gateway_latency', ''),
      ('warning', 'send_message', ''),
      ('warning', 'db_pool_acquire_slow', '')
  );
