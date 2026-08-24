-- Restore the exact pre-backfill 2026-08-13/14 rows, revert the inferred May
-- model and price, then remove the rollback snapshot and audit marker.

UPDATE agent_model_tokens_daily d SET
    cost_usd = s.cost_usd,
    llm_calls = s.llm_calls,
    costed_calls = s.costed_calls,
    unpriced_calls = s.unpriced_calls,
    tokens_in = s.tokens_in,
    tokens_out = s.tokens_out,
    tokens_cached = s.tokens_cached,
    tokens_reasoning = s.tokens_reasoning
FROM ledger_unpriced_backfill_20260824 s
WHERE d.agent_id = s.agent_id
  AND d.day = s.day
  AND d.model = s.model;

UPDATE agent_model_tokens_daily SET
    model = '',
    cost_usd = 0,
    costed_calls = 0,
    unpriced_calls = llm_calls,
    estimated_calls = 0
WHERE day IN ('2026-05-24', '2026-05-25')
  AND model = 'deepseek-v4-pro'
  AND estimated_calls > 0;

DROP TABLE IF EXISTS ledger_unpriced_backfill_20260824;

ALTER TABLE agent_model_tokens_daily
    DROP COLUMN IF EXISTS estimated_calls;
