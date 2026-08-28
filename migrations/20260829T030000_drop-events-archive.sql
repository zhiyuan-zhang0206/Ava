-- Task #1281/#1823 (user ruling 2026-08-29): drop the frozen PG `events`
-- archive. The table stopped receiving writes at the LGTM cutover (task
-- #1197) and every pre-cutover row now lives in the Loki archive stream
-- (parity-verified import, 365d retention); the readers moved there in
-- PR #886, and the cold pg_dump archive (taken before this migration is
-- deployed) is the long-term archive. `agent_archive_stats` (materialized
-- whole-life inspector values) is deliberately NOT dropped — inspect reads
-- it directly, independent of the events table.
--
-- CASCADE drops the month partitions, their indexes, and the owned
-- events_id_seq with the parent.
DROP TABLE IF EXISTS events CASCADE;
-- Explicit (the CASCADE above already drops it): the truncate-isolation lint
-- derives the table set from SQL text, and `events_default` is a baseline
-- declaration that must read as dropped.
DROP TABLE IF EXISTS events_default;
