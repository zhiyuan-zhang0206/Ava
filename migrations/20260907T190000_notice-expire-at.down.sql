DROP INDEX IF EXISTS agent_notices_expire_at_idx;

-- Map any 'expired' rows back to legal values before restoring strict check constraints
UPDATE agent_notices SET resolution = 'withdrawn' WHERE resolution = 'expired';

ALTER TABLE agent_notices DROP CONSTRAINT IF EXISTS agent_notices_resolution_legal;
ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_resolution_legal
    CHECK (resolution IS NULL
           OR (require_response AND resolution IN ('answered', 'dismissed', 'withdrawn', 'superseded'))
           OR (NOT require_response AND resolution IN ('answered', 'read', 'withdrawn', 'superseded')));

ALTER TABLE agent_notices DROP CONSTRAINT IF EXISTS agent_notices_resolution_check;
ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_resolution_check
    CHECK (resolution IN ('answered', 'dismissed', 'read', 'withdrawn', 'superseded'));

ALTER TABLE agent_notices DROP COLUMN IF EXISTS expire_at;
