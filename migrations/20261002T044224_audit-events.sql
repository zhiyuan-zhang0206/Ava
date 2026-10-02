-- The system of record for category=audit events: who did what to whom, kept
-- permanently. Rows are written by the producer, in the business transaction
-- that made the fact (or in its own short transaction when the producer owns
-- none); Loki receives the same event afterwards as an observation copy.
--
-- Columns follow the unified event model (decisions/2026-08-04-event-system-design.md).
-- event_uid is the surrogate id the event stream already carries for the same
-- event (base.telemetry.emitter.event_id, a 64-bit blake2b) reinterpreted as a
-- signed 64-bit integer, so a redelivered event is idempotent. id only orders
-- ties; identity order is not commit order, so nothing may tail by it.
-- imported_from is set on rows backfilled from the pre-cutover stores and is
-- NULL on every live row.
CREATE TABLE audit_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_uid       BIGINT NOT NULL UNIQUE,
    ts              TIMESTAMPTZ NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    trace_id        TEXT,
    span_id         TEXT,
    agent_id        BIGINT,
    machine         TEXT NOT NULL,
    process         TEXT NOT NULL,
    event_name      TEXT NOT NULL,
    level           TEXT NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error', 'critical')),
    source          TEXT NOT NULL,
    target_agent_id BIGINT,
    attributes      JSONB NOT NULL DEFAULT '{}'::jsonb,
    imported_from   TEXT
);

CREATE INDEX audit_events_ts ON audit_events (ts);
CREATE INDEX audit_events_agent_ts ON audit_events (agent_id, ts);
CREATE INDEX audit_events_name_ts ON audit_events (event_name, ts);
CREATE INDEX audit_events_target_ts ON audit_events (target_agent_id, ts)
    WHERE target_agent_id IS NOT NULL;

COMMENT ON TABLE audit_events IS
    'Append-only record of category=audit events; Loki holds only a projection. Permanent: no UPDATE, DELETE or TRUNCATE.';

CREATE FUNCTION reject_audit_events_rewrite() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only; % is forbidden', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_events_append_only
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION reject_audit_events_rewrite();
CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_events_rewrite();

-- Application surface: both groups read and append. The gateway group's blanket
-- DML grant (and the default privileges for new tables) would also give it
-- UPDATE and DELETE, so those are revoked here and again wherever
-- base/cluster/authority/groups.py converges the grants; the triggers above
-- stay the guarantee for every other role. Gated on the roles' existence: fresh
-- bootstrap applies this before install birth creates them.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT ON audit_events TO ava_runner;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_gateway') THEN
        REVOKE UPDATE, DELETE ON audit_events FROM ava_gateway;
    END IF;
END $$;
