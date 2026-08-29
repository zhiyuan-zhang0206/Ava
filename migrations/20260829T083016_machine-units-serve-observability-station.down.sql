-- Drop the third capability flag (rollback of
-- machine-units-serve-observability-station). Station capability records are
-- lost on rollback — the intended fail-fast for an un-rolled-back station
-- declaration, mirroring the machines.role CHECK rollback.

ALTER TABLE machine_units DROP COLUMN serve_observability_station;
