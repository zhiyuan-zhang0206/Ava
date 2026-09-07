-- Add mandatory expire_at to agent_notices, backfill existing rows,
-- allow 'expired' resolution status, and add an index for the TTL reaper sweep.

ALTER TABLE agent_notices ADD COLUMN expire_at TIMESTAMPTZ;

-- Backfill: resolved historical notices get created_at + 1 day; still-open notices
-- get a full 24h grace window from deployment (now() + 1 day) so older open notices
-- are not immediately expired on rollout.
UPDATE agent_notices
SET expire_at = CASE
    WHEN resolved_at IS NOT NULL THEN created_at + INTERVAL '1 day'
    ELSE now() + INTERVAL '1 day'
END
WHERE expire_at IS NULL;

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
