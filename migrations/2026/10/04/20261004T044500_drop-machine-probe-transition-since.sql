-- Contract machine_probe.transition_since (expand-contract: the code removal
-- shipped first). It recorded the first failed probe of an episode so the
-- liveness pass could grade the "machine offline" alert; the alert is now a
-- Grafana rule over the machine_probe_failed event (carrying
-- consecutive_failures), and nothing writes or reads the column.

ALTER TABLE machine_probe DROP COLUMN IF EXISTS transition_since;
