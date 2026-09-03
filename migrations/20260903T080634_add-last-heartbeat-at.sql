ALTER TABLE agents_meta ADD COLUMN last_heartbeat_at TIMESTAMPTZ;

COMMENT ON COLUMN agents_meta.last_heartbeat_at IS
'When the heartbeat daemon last inserted a check-in inbound for this agent. The daemon uses it as a durable cadence floor, so a consumed heartbeat whose turn performed no LLM work cannot be reinserted every dispatch step.';
