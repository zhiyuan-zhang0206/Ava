-- Task #180 (LGTM cutover sweep): drop the neighbor-graph SQL function.
-- agent_neighbors() read the unified `events` table, which stopped being
-- written at the LGTM cutover (task #1197) — the walk silently returned no
-- peers for every agent. The read moved to the event stream
-- (gateway/neighbors.py: frozen archive stitch + Loki live tail, walk in
-- Python); the function and its one-caller helper event_edge_weight are
-- dead and must not linger as a live-looking read of the frozen table.
DROP FUNCTION IF EXISTS agent_neighbors(bigint, int, double precision, double precision, int);
DROP FUNCTION IF EXISTS event_edge_weight(timestamptz, bigint, double precision);
