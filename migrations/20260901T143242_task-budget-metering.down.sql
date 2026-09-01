ALTER TABLE agent_tasks
    DROP COLUMN IF EXISTS usd_budget_notified_at,
    DROP COLUMN IF EXISTS token_budget_notified_at,
    DROP COLUMN IF EXISTS usd_used,
    DROP COLUMN IF EXISTS token_used,
    DROP COLUMN IF EXISTS usd_budget,
    DROP COLUMN IF EXISTS token_budget;
