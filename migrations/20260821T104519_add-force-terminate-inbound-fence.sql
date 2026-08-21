ALTER TABLE agents_meta
    ADD COLUMN IF NOT EXISTS last_force_terminate_inbound_id BIGINT;

-- Preserve force intent created by the pre-fence binary. Its force path wrote a
-- terminate inbound but had nowhere to store the id. Prefer that exact evidence;
-- for a user-terminated row with no marker (the old crash window), fence every
-- inbound already present and require genuinely new post-upgrade work.
WITH legacy_force AS (
    SELECT
        a.id,
        COALESCE(
            MAX(m.id) FILTER (
                WHERE m.kind = 'terminate' AND m.created_at > a.status_changed_at
            ),
            MAX(m.id),
            0
        ) AS fence_id
    FROM agents_meta a
    LEFT JOIN inbound_messages m ON m.agent_id = a.id
    WHERE a.status = 'terminated' AND a.termination_source = 'user'
    GROUP BY a.id
)
UPDATE agents_meta a
SET last_force_terminate_inbound_id = legacy_force.fence_id
FROM legacy_force
WHERE a.id = legacy_force.id;

COMMENT ON COLUMN agents_meta.last_force_terminate_inbound_id IS
    'Monotonic inbound id fence written by every explicit force termination. '
    'Pending work may auto-resurrect this agent only when its inbound id is greater '
    'than this fence. Deliberately no foreign key: inbound retention must not '
    'erase lifecycle intent.';
