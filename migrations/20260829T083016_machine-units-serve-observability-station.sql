-- machine-units-serve-observability-station: the machine_units table gains
-- the third capability flag, mirroring serve_gateway / serve_agent_runner
-- (WP2 gateway wrap-up, #1944). The composed machines.role derives from these
-- per-unit flags, so a pure observability-station unit (serve_gateway=false,
-- serve_agent_runner=false) must be able to contribute its capability or the
-- recompute would produce an empty role and the station host would never
-- appear in the roster.
--
-- IF NOT EXISTS keeps the body idempotent on a fresh bootstrap, where
-- db/schema.sql already carries the new shape (migration smoke replays on
-- fresh). Existing rows keep the DEFAULT false — the column is additive, no
-- data is rewritten.

ALTER TABLE machine_units
    ADD COLUMN IF NOT EXISTS serve_observability_station BOOLEAN NOT NULL DEFAULT false;
