-- The table carries no origin-of-record data: it is recomputable from the
-- archive JSONL by `scripts/backfill_llm_usage_hourly.py`, so a plain drop
-- loses nothing that cannot be rebuilt.
DROP TABLE IF EXISTS llm_usage_hourly;
