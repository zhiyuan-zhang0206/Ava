-- Down: drop the cost columns. The token columns (and every pre-existing
-- row) survive; the cost sums are re-derivable — post-cutover days by the
-- Loki-sourced rollup pass, archive days by re-running the up migration
-- while the frozen `events` archive still exists. After the archive is
-- exported and removed, the archive days' cost columns become
-- dump-restorable only — which is the documented trade of the export step,
-- not of this down.

ALTER TABLE agent_model_tokens_daily
    DROP COLUMN IF EXISTS cost_usd,
    DROP COLUMN IF EXISTS costed_calls,
    DROP COLUMN IF EXISTS unpriced_calls;
