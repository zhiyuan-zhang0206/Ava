-- llm-cost-rollup-columns: agent_model_tokens_daily becomes the durable cost
-- ledger.
--
-- The daily rollup gains the usage-time cost columns (user principle: cost is
-- billed at the price in force at the call, never re-priced — so the ledger
-- stores summed snapshots, not tokens-to-be-priced-later):
--   cost_usd       — sum of the day's stored `cost_usd` price snapshots
--   costed_calls   — calls that carried a snapshot
--   unpriced_calls — calls without one (unpriced model / pre-snapshot row);
--                    they contribute 0 cost, by design
--
-- The backfill below is the LAST read of the frozen pre-LGTM `events`
-- archive: it re-derives every (agent, day, model) group so rows missing
-- from the rollup (a cluster whose maintenance daemon never completed the
-- initial backfill) are inserted, and existing rows get their cost columns
-- filled. After this migration the archive has no remaining code reader —
-- its export/removal is a separate operator step.
-- Idempotent: the aggregate is a deterministic function of the frozen rows.

ALTER TABLE agent_model_tokens_daily
    ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS costed_calls BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS unpriced_calls BIGINT NOT NULL DEFAULT 0;

-- The events read is guarded: the frozen PG `events` archive was dropped by
-- migration 20260829T030000_drop-events-archive (task #1823), so a fresh-DB
-- replay (baseline without the table) must skip the backfill — the archive
-- data lives in the Loki archive stream + cold pg_dump instead.
DO $$
BEGIN
    IF to_regclass('public.events') IS NOT NULL THEN
        INSERT INTO agent_model_tokens_daily
            (agent_id, day, model, llm_calls, tokens_in, tokens_out, tokens_cached,
             tokens_reasoning, cost_usd, costed_calls, unpriced_calls)
        SELECT agent_id,
               (ts AT TIME ZONE 'UTC')::date AS day,
               COALESCE(attributes->>'model', '') AS model,
               COUNT(*),
               COALESCE(SUM((attributes->>'in_total')::bigint), 0),
               COALESCE(SUM((attributes->>'out_total')::bigint), 0),
               COALESCE(SUM((attributes->>'cache_read')::bigint), 0),
               COALESCE(SUM((attributes->>'reasoning')::bigint), 0),
               COALESCE(SUM((attributes->>'cost_usd')::float8), 0),
               COUNT(attributes->>'cost_usd'),
               COUNT(*) - COUNT(attributes->>'cost_usd')
        FROM events
        WHERE event_name = 'llm_usage'
          AND agent_id IS NOT NULL
        GROUP BY agent_id, (ts AT TIME ZONE 'UTC')::date, COALESCE(attributes->>'model', '')
        ON CONFLICT (agent_id, day, model) DO UPDATE SET
            llm_calls        = EXCLUDED.llm_calls,
            tokens_in        = EXCLUDED.tokens_in,
            tokens_out       = EXCLUDED.tokens_out,
            tokens_cached    = EXCLUDED.tokens_cached,
            tokens_reasoning = EXCLUDED.tokens_reasoning,
            cost_usd         = EXCLUDED.cost_usd,
            costed_calls     = EXCLUDED.costed_calls,
            unpriced_calls   = EXCLUDED.unpriced_calls;
    END IF;
END $$;
