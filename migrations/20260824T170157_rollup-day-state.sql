CREATE TABLE IF NOT EXISTS rollup_day_state (
    day date PRIMARY KEY,
    status text NOT NULL DEFAULT 'rolled' CHECK (status IN ('rolled', 'failed')),
    source_count bigint NOT NULL,
    rolled_at timestamptz NOT NULL DEFAULT now(),
    error text
);
