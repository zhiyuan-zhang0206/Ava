-- The whole-life token sum per agent, folded from the day-grain ledger once its days can no
-- longer change. A reader of "all time" adds this to the days after the fold and to the newest
-- raw rows instead of scanning the ledger or the event table, whose size grows without bound.
-- `agent_token_totals_through` holds the single watermark: the last UTC day folded in.
CREATE TABLE IF NOT EXISTS agent_token_totals (
    agent_id   BIGINT PRIMARY KEY REFERENCES agents(id),
    tokens_in  BIGINT NOT NULL DEFAULT 0,
    tokens_out BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agent_token_totals_through (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    day       DATE NOT NULL
);
