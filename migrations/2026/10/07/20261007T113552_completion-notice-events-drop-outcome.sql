-- Contract step: completion_notice_events no longer carries an outcome or exit
-- code. No code reads or writes them since the expand migration
-- 20261006T181011_completion-notice-events-stop-outcome; dropping the columns
-- also drops the old (agent_id, source, outcome) unique constraint.
ALTER TABLE completion_notice_events DROP CONSTRAINT IF EXISTS completion_notice_events_source_outcome_unique;
ALTER TABLE completion_notice_events DROP COLUMN IF EXISTS outcome;
ALTER TABLE completion_notice_events DROP COLUMN IF EXISTS exit_code;
