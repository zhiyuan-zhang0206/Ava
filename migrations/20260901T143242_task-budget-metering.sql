-- Task-scoped LLM usage ceilings. Only explicitly task-tagged calls add to
-- these counters; ownership alone is deliberately not attribution.
ALTER TABLE agent_tasks
    ADD COLUMN IF NOT EXISTS token_budget BIGINT CHECK (token_budget IS NULL OR token_budget > 0),
    ADD COLUMN IF NOT EXISTS usd_budget DOUBLE PRECISION CHECK (
        usd_budget IS NULL OR (usd_budget > 0 AND usd_budget < 'Infinity'::double precision)
    ),
    ADD COLUMN IF NOT EXISTS token_used BIGINT NOT NULL DEFAULT 0 CHECK (token_used >= 0),
    ADD COLUMN IF NOT EXISTS usd_used DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (
        usd_used >= 0 AND usd_used < 'Infinity'::double precision
    ),
    ADD COLUMN IF NOT EXISTS token_budget_notified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS usd_budget_notified_at TIMESTAMPTZ;
