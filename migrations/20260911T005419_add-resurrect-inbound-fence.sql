-- resurrection epoch fence: the id of the kind='resurrect' inbound every
-- resurrection enqueues. Lifecycle commands (restart/terminate) whose intent
-- predates the fence are superseded by that resurrection: acceptance settles
-- them as done, carrying a payload that names the resurrect inbound, instead
-- of adopting them. A delayed terminate created before a resurrect can
-- therefore never kill the incarnation that resurrect just admitted (#2158).
ALTER TABLE agents_meta ADD COLUMN last_resurrect_inbound_id BIGINT;

-- Preserve the epoch boundary for rows resurrected before this fence existed:
-- the newest retained resurrect inbound is that boundary. Commands below it
-- are settled by the next acceptance instead of being replayed; commands above
-- it remain current intent.
UPDATE agents_meta a
SET last_resurrect_inbound_id = latest.inbound_id
FROM (
    SELECT agent_id, MAX(id) AS inbound_id
    FROM inbound_messages
    WHERE kind = 'resurrect'
    GROUP BY agent_id
) latest
WHERE a.id = latest.agent_id;

COMMENT ON COLUMN agents_meta.last_resurrect_inbound_id IS
    'Monotonic inbound id fence written by every resurrection: the id of the '
    'kind=''resurrect'' inbound it enqueued. A lifecycle command whose intent '
    'predates the fence is superseded by that resurrection; acceptance settles '
    'it instead of dispatching it. Deliberately no foreign key: inbound '
    'retention must not erase lifecycle intent.';
