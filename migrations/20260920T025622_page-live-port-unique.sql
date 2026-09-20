-- One live page per (host, port): an open, unexpired agent_pages row owns its
-- socket. Without this rule two pages could register the same (host, port) —
-- the page-server daemon hands the socket to whichever server binds first and
-- backs off forever for the other, while the gateway reverse-proxies the
-- second page's URL to the first page's content. The gateway now refuses such
-- a registration up front (ops.pages.assert_port_free); this index is what
-- holds the invariant against registrations that race that check.
--
-- Duplicates that got in before the rule existed are resolved first: the
-- newest registration per socket wins (it is the agent's current intent) and
-- the older rows are closed — the same net outcome the daemon's
-- foreign-occupant backoff already forces, since only one of the two could
-- ever serve.

UPDATE agent_pages SET closed_at = now()
WHERE id IN (
    SELECT id FROM (
        SELECT id,
               row_number() OVER (PARTITION BY host, port ORDER BY id DESC) AS rn
        FROM agent_pages
        WHERE closed_at IS NULL AND expired_at IS NULL AND host IS NOT NULL
    ) ranked
    WHERE rn > 1
);

-- Expired rows are excluded: they no longer serve and must not block a fresh
-- registration on the port.
CREATE UNIQUE INDEX IF NOT EXISTS agent_pages_unique_live_port
    ON agent_pages (host, port)
    WHERE closed_at IS NULL AND expired_at IS NULL;
