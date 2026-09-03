-- llm-usage-hourly: the companion table holding the restored historical LLM
-- usage/cost curve. Rows before 2026-08-13 exist only in the frozen 2026-08-28
-- cold PG events archive (Loki's 7d retention already lost that window), so the
-- curve is re-derived once from that archive's JSONL extract by
-- `scripts/backfill_llm_usage_hourly.py` and stored here. The table is a
-- derived artifact, never a write path: every column is recomputable by
-- re-running the backfill over the same source.
CREATE TABLE IF NOT EXISTS llm_usage_hourly (
    ts_hour          TIMESTAMPTZ NOT NULL,
    model            TEXT NOT NULL,
    in_total         BIGINT NOT NULL DEFAULT 0,
    cache_read       BIGINT NOT NULL DEFAULT 0,
    out_total        BIGINT NOT NULL DEFAULT 0,
    reasoning        BIGINT NOT NULL DEFAULT 0,
    cost_peak_usd    DOUBLE PRECISION NOT NULL DEFAULT 0,
    cost_offpeak_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (ts_hour, model)
);

COMMENT ON TABLE llm_usage_hourly IS
    'Hourly model-level LLM usage/cost, restored historical curve from the 2026-08-28 cold archive; recomputable from the source JSONL.';
