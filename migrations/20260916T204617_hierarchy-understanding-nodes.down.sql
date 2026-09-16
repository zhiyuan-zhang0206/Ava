-- Reverse: drop the understanding tree table. Dropping removes the table's
-- ACL entries and its owned id sequence, so no separate REVOKE is needed.
DROP TABLE IF EXISTS understanding_nodes;
