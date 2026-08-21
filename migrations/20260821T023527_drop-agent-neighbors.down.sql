-- Reverse of the task #180 agent_neighbors() drop: restore the SQL
-- neighbor walk over the unified `events` table (pre-cutover semantics).
CREATE FUNCTION event_edge_weight(last_seen timestamptz, event_count bigint,
                                  k double precision)
RETURNS double precision
LANGUAGE sql STABLE AS $$
    SELECT EXP(-k * EXTRACT(EPOCH FROM (now() - last_seen)) / 86400.0)
         * LN(1 + event_count)
$$;

CREATE FUNCTION agent_neighbors(
    root bigint,
    max_depth int DEFAULT 1,
    k double precision DEFAULT 0.5,
    gamma double precision DEFAULT 0.5,
    lim int DEFAULT 20)
RETURNS TABLE(agent_id bigint, depth int, score double precision)
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE
    edge AS (
        SELECT a, b, SUM(w) AS w
        FROM (
            SELECT LEAST(e.agent_id, e.target_agent_id)    AS a,
                   GREATEST(e.agent_id, e.target_agent_id) AS b,
                   LN(1 + COUNT(*))                        AS w
            FROM events e
            WHERE e.category = 'audit'
              AND e.event_name IN ('spawn', 'fork', 'resurrect')
              AND e.target_agent_id IS NOT NULL
              AND e.agent_id <> e.target_agent_id
            GROUP BY LEAST(e.agent_id, e.target_agent_id),
                     GREATEST(e.agent_id, e.target_agent_id)
            UNION ALL
            SELECT LEAST(e.agent_id, e.target_agent_id)    AS a,
                   GREATEST(e.agent_id, e.target_agent_id) AS b,
                   event_edge_weight(MAX(e.ts), COUNT(*), k) AS w
            FROM events e
            WHERE e.category = 'audit'
              AND e.event_name = 'send_message'
              AND e.target_agent_id IS NOT NULL
              AND e.agent_id <> e.target_agent_id
            GROUP BY LEAST(e.agent_id, e.target_agent_id),
                     GREATEST(e.agent_id, e.target_agent_id)
        ) parts
        GROUP BY a, b
    ),
    adj AS (
        SELECT a AS src, b AS dst, w FROM edge
        UNION ALL
        SELECT b AS src, a AS dst, w FROM edge
    ),
    walk AS (
        SELECT root AS node, 0 AS depth,
               0.0::double precision AS hop_w, ARRAY[root] AS path
        UNION ALL
        SELECT adj.dst, w.depth + 1, adj.w, w.path || adj.dst
        FROM walk w
        JOIN adj ON adj.src = w.node
        WHERE w.depth < max_depth
          AND adj.dst <> ALL(w.path)
    )
    SELECT node AS agent_id,
           MIN(depth) AS depth,
           MAX(hop_w * power(gamma, depth - 1)) AS score
    FROM walk
    WHERE node <> root
    GROUP BY node
    ORDER BY score DESC
    LIMIT lim
$$;
