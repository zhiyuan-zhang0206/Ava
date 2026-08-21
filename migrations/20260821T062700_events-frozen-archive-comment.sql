-- The events table is a frozen archive since the LGTM cutover (task #1197,
-- 2026-08-12): the live event stream ships to Loki via the unified OTLP
-- emitter, and reads over recent windows must go through LogQL
-- (gateway/loki_events.py). The comment makes that structural to the next DB
-- reader (issue #180 deliverable 4 — a live-looking table quietly returning
-- empty was the whole incident class); nothing about the table's storage
-- changes. COMMENT replaces idempotently.
COMMENT ON TABLE events IS
    'Frozen archive since the LGTM cutover (task #1197): the live event stream ships to Loki via the unified OTLP emitter; reads over recent windows go through LogQL (gateway/loki_events.py). This copy serves retention/rollup and deliberate historical reads only.';
