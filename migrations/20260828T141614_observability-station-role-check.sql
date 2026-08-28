-- observability-station-role-check: the machines.role CHECK constraint gains
-- the observability-station capability token (WP1 deployment unit, #1902).
-- Python's MachineRole Literal is the source of truth
-- (shared/machine.py); the DB CHECK must accept every token it can emit or a
-- station-capable host's registration would violate the constraint. Re-run
-- safe: DROP/ADD of the same constraint is idempotent in effect.

ALTER TABLE machines DROP CONSTRAINT machines_role_check;
ALTER TABLE machines ADD CONSTRAINT machines_role_check
    CHECK (role <@ ARRAY['gateway', 'agent-runner', 'observability-station']::text[]
           AND cardinality(role) >= 1);
