-- The whole-life sums per agent and model, folded from the day-grain ledger
-- (agent_model_tokens_daily) once its days can no longer change. A reader of "all time" adds this
-- to the days after the fold and to the newest raw rows instead of scanning the ledger or the
-- event table, whose size grows without bound. `agent_model_tokens_total_through` holds the single
-- watermark: the last UTC day folded in.
CREATE TABLE IF NOT EXISTS agent_model_tokens_total (
    agent_id         BIGINT NOT NULL REFERENCES agents(id),
    model            TEXT   NOT NULL,
    llm_calls        BIGINT NOT NULL DEFAULT 0,
    tokens_in        BIGINT NOT NULL DEFAULT 0,
    tokens_out       BIGINT NOT NULL DEFAULT 0,
    tokens_cached    BIGINT NOT NULL DEFAULT 0,
    tokens_reasoning BIGINT NOT NULL DEFAULT 0,
    cost_usd         DOUBLE PRECISION NOT NULL DEFAULT 0,
    costed_calls     BIGINT NOT NULL DEFAULT 0,
    unpriced_calls   BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_id, model)
);

CREATE TABLE IF NOT EXISTS agent_model_tokens_total_through (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    day       DATE NOT NULL
);
