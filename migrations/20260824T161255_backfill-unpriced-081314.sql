-- backfill-unpriced-081314: clear the durable cost ledger's remaining
-- unpriced calls and mark the inferred May prices for audit.
--
-- Root cause for the 2026-08-13/14 gap: the Loki daily rollup replaced rows
-- restored by 20260818T142518_llm-cost-rollup-columns with snapshot-less Loki
-- aggregates while those days were still inside Loki retention. Recovery has
-- two sources: the frozen `events` archive restores the exact priced events
-- from the 2026-08-13 morning, and catalog period-1 rates price the Loki-era
-- token sums from the snapshot below. The verified price formula is:
--
--   ((in_total - cache_read) * miss
--      + cache_read * hit
--      + out_total * out) / 1e6
--
-- `out_total` already includes reasoning tokens, so reasoning is not added a
-- second time. Mixed 2026-08-14 rows price only the unpriced_calls/llm_calls
-- proportional share of their pre-merge token sums; the clobbered 2026-08-13
-- rows have a share of 1.0.
--
-- The empty-model 2026-05-24/25 rows are inferred as deepseek-v4-pro because
-- it was the exclusive model in the adjacent 2026-05-26..29 era, verified
-- against 200 May archive price snapshots. `estimated_calls` preserves that
-- inference as an audit marker.
--
-- Re-run contract: the snapshot is created only once and retains the original
-- pre-state; the archive merge requires the current row to remain wholly
-- uncosted, the rate backfill requires current unpriced calls, and the May
-- inference requires estimated_calls = 0. A successful re-run is therefore a
-- no-op, including after a direct production apply followed by cluster update.

ALTER TABLE agent_model_tokens_daily
    ADD COLUMN IF NOT EXISTS estimated_calls BIGINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS ledger_unpriced_backfill_20260824 AS
SELECT agent_id, day, model, llm_calls, costed_calls, unpriced_calls,
       tokens_in, tokens_out, tokens_cached, tokens_reasoning, cost_usd
FROM agent_model_tokens_daily
WHERE day IN ('2026-08-13', '2026-08-14') AND unpriced_calls > 0;

-- The events read is guarded: the frozen PG `events` archive was dropped by
-- migration 20260829T030000_drop-events-archive (task #1823), so a fresh-DB
-- replay (baseline without the table) must skip the archive merge.
DO $$
BEGIN
    IF to_regclass('public.events') IS NOT NULL THEN
        INSERT INTO agent_model_tokens_daily
            (agent_id, day, model, llm_calls, tokens_in, tokens_out, tokens_cached,
             tokens_reasoning, cost_usd, costed_calls, unpriced_calls)
        SELECT e.agent_id,
               (e.ts AT TIME ZONE 'UTC')::date AS day,
               COALESCE(e.attributes->>'model', '') AS model,
               COUNT(*),
               COALESCE(SUM((e.attributes->>'in_total')::bigint), 0),
               COALESCE(SUM((e.attributes->>'out_total')::bigint), 0),
               COALESCE(SUM((e.attributes->>'cache_read')::bigint), 0),
               COALESCE(SUM((e.attributes->>'reasoning')::bigint), 0),
               COALESCE(SUM((e.attributes->>'cost_usd')::float8), 0),
               COUNT(e.attributes->>'cost_usd'),
               COUNT(*) - COUNT(e.attributes->>'cost_usd')
        FROM events e
        JOIN ledger_unpriced_backfill_20260824 s
          ON s.agent_id = e.agent_id
         AND s.day = (e.ts AT TIME ZONE 'UTC')::date
         AND s.model = COALESCE(e.attributes->>'model', '')
         AND s.costed_calls = 0
        JOIN agent_model_tokens_daily d
          ON d.agent_id = e.agent_id
         AND d.day = (e.ts AT TIME ZONE 'UTC')::date
         AND d.model = COALESCE(e.attributes->>'model', '')
         AND d.costed_calls = 0
        WHERE e.event_name = 'llm_usage'
          AND e.agent_id IS NOT NULL
          AND e.ts >= '2026-08-13T00:00:00Z'
          AND e.ts < '2026-08-14T00:00:00Z'
        GROUP BY e.agent_id, (e.ts AT TIME ZONE 'UTC')::date,
                 COALESCE(e.attributes->>'model', '')
        ON CONFLICT (agent_id, day, model) DO UPDATE SET
            llm_calls      = agent_model_tokens_daily.llm_calls + EXCLUDED.llm_calls,
            tokens_in      = agent_model_tokens_daily.tokens_in + EXCLUDED.tokens_in,
            tokens_out     = agent_model_tokens_daily.tokens_out + EXCLUDED.tokens_out,
            tokens_cached  = agent_model_tokens_daily.tokens_cached + EXCLUDED.tokens_cached,
            tokens_reasoning = agent_model_tokens_daily.tokens_reasoning + EXCLUDED.tokens_reasoning,
            cost_usd       = agent_model_tokens_daily.cost_usd + EXCLUDED.cost_usd,
            costed_calls   = agent_model_tokens_daily.costed_calls + EXCLUDED.costed_calls,
            unpriced_calls = agent_model_tokens_daily.unpriced_calls + EXCLUDED.unpriced_calls;
    END IF;
END $$;

UPDATE agent_model_tokens_daily d SET
    cost_usd = d.cost_usd + (
        ((s.tokens_in - s.tokens_cached) * p.miss
         + s.tokens_cached * p.hit
         + s.tokens_out * p.out)
        * s.unpriced_calls / NULLIF(s.llm_calls, 0) / 1e6
    ),
    costed_calls = d.costed_calls + s.unpriced_calls,
    unpriced_calls = 0
FROM ledger_unpriced_backfill_20260824 s
JOIN (VALUES ('deepseek-v4-pro', 0.435, 0.003625, 0.87),
             ('deepseek-v4-flash', 0.14, 0.0028, 0.28))
     AS p(model, miss, hit, out) ON p.model = s.model
WHERE d.agent_id = s.agent_id
  AND d.day = s.day
  AND d.model = s.model
  AND d.unpriced_calls > 0;

UPDATE agent_model_tokens_daily d SET
    model = 'deepseek-v4-pro',
    cost_usd = ROUND((
        ((d.tokens_in - d.tokens_cached) * 0.435
         + d.tokens_cached * 0.003625
         + d.tokens_out * 0.87) / 1e6
    )::numeric, 12),
    costed_calls = d.llm_calls,
    unpriced_calls = 0,
    estimated_calls = d.llm_calls
WHERE d.day IN ('2026-05-24', '2026-05-25')
  AND d.model = ''
  AND d.estimated_calls = 0;
