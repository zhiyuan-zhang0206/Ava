-- Index the resolved-history query (GET /api/notices/resolved and the
-- unified feed's resolved_page): ORDER BY resolved_at DESC, id DESC on the
-- resolved half of the table (Task #1814). agent_notices accumulates every
-- notice ever posted (2k+ rows and growing); without this the history page
-- degrades into a seq scan + sort as the table grows, and every mark-read
-- refetches it.
-- IF NOT EXISTS: the migration replays on a fresh bootstrap, where
-- db/schema.sql already carries the index (migration smoke replays on fresh).
CREATE INDEX IF NOT EXISTS agent_notices_resolved_idx
    ON agent_notices (resolved_at DESC, id DESC)
    WHERE resolved_at IS NOT NULL;
