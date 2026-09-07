-- Add mandatory expire_at to agent_notices, backfill existing rows with 1 day TTL,
-- allow 'expired' resolution status, and add an index for the TTL reaper sweep.

ALTER TABLE agent_notices ADD COLUMN expire_at TIMESTAMPTZ;

UPDATE agent_notices SET expire_at = created_at + INTERVAL '1 day' WHERE expire_at IS NULL;

ALTER TABLE agent_notices ALTER COLUMN expire_at SET NOT NULL;

COMMENT ON COLUMN agent_notices.expire_at IS
    'Absolute expiration timestamp for the notice. When reached while unresolved, the notice is auto-closed as expired.';

ALTER TABLE agent_notices DROP CONSTRAINT IF EXISTS agent_notices_resolution_check;
ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_resolution_check
    CHECK (resolution IN ('answered', 'dismissed', 'read', 'withdrawn', 'superseded', 'expired'));

ALTER TABLE agent_notices DROP CONSTRAINT IF EXISTS agent_notices_resolution_legal;
ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_resolution_legal
    CHECK (resolution IS NULL
           OR (require_response AND resolution IN ('answered', 'dismissed', 'withdrawn', 'superseded', 'expired'))
           OR (NOT require_response AND resolution IN ('answered', 'read', 'withdrawn', 'superseded', 'expired')));

CREATE INDEX agent_notices_expire_at_idx
    ON agent_notices (expire_at)
    WHERE resolved_at IS NULL;
