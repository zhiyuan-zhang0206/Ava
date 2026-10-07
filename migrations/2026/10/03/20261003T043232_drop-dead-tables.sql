-- Contract four tables whose readers and writers are gone. The previous release
-- shipped the code change first (expand-contract): nothing reads or writes them.
--
--   * rollup_day_state: the Loki-era rollup watermark; the rollup now reads
--     telemetry_events in place;
--   * agent_metric_scans: the scan-coverage ledger of the retired mirror backfill;
--   * agent_archive_stats: whole-life inspector values from the frozen events
--     archive; the inspector no longer consults it;
--   * llm_usage_hourly: the restored hourly usage curve; no reader remains.
--
-- Dropped rows are not carried anywhere: the operator saves them before the
-- rollout if they are wanted (see the PR's rollout notes).

DROP TABLE IF EXISTS rollup_day_state;
DROP TABLE IF EXISTS agent_metric_scans;
DROP TABLE IF EXISTS agent_archive_stats;
DROP TABLE IF EXISTS llm_usage_hourly;
