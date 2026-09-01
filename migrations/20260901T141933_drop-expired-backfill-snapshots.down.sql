-- Restore the retired snapshot shapes for a schema rollback. Their expired
-- retention data is intentionally not recoverable.

CREATE TABLE fork_lineage_fix_backfill_agents_meta (
    id       BIGINT,
    spawner  TEXT
);

CREATE TABLE fork_lineage_fix_backfill_events (
    id               BIGINT,
    target_agent_id  BIGINT
);

CREATE TABLE ledger_unpriced_backfill_20260824 (
    agent_id         BIGINT,
    day              DATE,
    model            TEXT,
    llm_calls        BIGINT,
    costed_calls     BIGINT,
    unpriced_calls   BIGINT,
    tokens_in        BIGINT,
    tokens_out       BIGINT,
    tokens_cached    BIGINT,
    tokens_reasoning BIGINT,
    cost_usd         DOUBLE PRECISION
);
