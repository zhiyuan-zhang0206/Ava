-- Durable record of category=telemetry and category=log events. Loki keeps an
-- observation copy for Grafana and short windows; its 84-hour retention is not
-- a record. Rows are appended by the emitter's drain thread in batches.
--
-- Same columns as audit_events plus the two the audit table does not need:
-- category and cluster. event_uid is the surrogate id the stream already
-- carries for the event (base.telemetry.emitter.event_id) as a signed 64-bit
-- integer. The table is partitioned by month on ts so old months can be
-- dropped one partition at a time later; there is no retention policy and no
-- delete path today. A partitioned table's unique key must contain the
-- partition key, so identity is (event_uid, ts); ts is part of the id's input,
-- so a redelivered event always repeats both.
-- imported_from is set on rows backfilled from the pre-cutover stores and is
-- NULL on every live row.
CREATE TABLE telemetry_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY,
    event_uid       BIGINT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    trace_id        TEXT,
    span_id         TEXT,
    agent_id        BIGINT,
    machine         TEXT NOT NULL,
    cluster         TEXT NOT NULL,
    process         TEXT NOT NULL,
    category        TEXT NOT NULL CHECK (category IN ('telemetry', 'log')),
    event_name      TEXT NOT NULL,
    level           TEXT NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error', 'critical')),
    source          TEXT NOT NULL,
    target_agent_id BIGINT,
    attributes      JSONB NOT NULL DEFAULT '{}'::jsonb,
    imported_from   TEXT,
    PRIMARY KEY (event_uid, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX telemetry_events_ts ON telemetry_events (ts);
CREATE INDEX telemetry_events_agent_ts ON telemetry_events (agent_id, ts);
CREATE INDEX telemetry_events_name_ts ON telemetry_events (event_name, ts);
CREATE INDEX telemetry_events_trace ON telemetry_events (trace_id) WHERE trace_id IS NOT NULL;

COMMENT ON TABLE telemetry_events IS
    'Append-only record of category=telemetry and category=log events, partitioned by month on ts; Loki holds only an observation copy. No UPDATE, DELETE or TRUNCATE; old months leave by dropping a partition.';

CREATE FUNCTION reject_telemetry_events_rewrite() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'telemetry_events is append-only; % is forbidden', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER telemetry_events_append_only
    BEFORE UPDATE OR DELETE ON telemetry_events
    FOR EACH ROW EXECUTE FUNCTION reject_telemetry_events_rewrite();
CREATE TRIGGER telemetry_events_no_truncate
    BEFORE TRUNCATE ON telemetry_events
    FOR EACH STATEMENT EXECUTE FUNCTION reject_telemetry_events_rewrite();

-- Monthly partitions (UTC boundaries) from the previous month through
-- p_months_ahead months ahead, created idempotently. The application logins
-- have no DDL, so the writer calls this SECURITY DEFINER function when the
-- month changes. A DEFAULT partition catches an event outside every month so a
-- write never fails; rows left there block creating the month that covers them,
-- so the function's failure is loud, not silent.
CREATE FUNCTION public.ensure_telemetry_event_partitions(p_months_ahead INT DEFAULT 3)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
DECLARE
    month_start TIMESTAMPTZ;
    offset_months INT;
BEGIN
    EXECUTE 'create table if not exists public.telemetry_events_default '
            'partition of public.telemetry_events default';
    FOR offset_months IN -1..p_months_ahead LOOP
        month_start := (date_trunc('month', now() AT TIME ZONE 'UTC')
                        + make_interval(months => offset_months)) AT TIME ZONE 'UTC';
        EXECUTE format(
            'create table if not exists %s partition of public.telemetry_events '
            'for values from (%L) to (%L)',
            'public.telemetry_events_' || to_char(month_start AT TIME ZONE 'UTC', 'YYYYMM'),
            month_start,
            month_start + interval '1 month'
        );
    END LOOP;
END;
$function$;

REVOKE ALL ON FUNCTION public.ensure_telemetry_event_partitions(INT) FROM PUBLIC;
SELECT public.ensure_telemetry_event_partitions(3);

-- Application surface: both groups read and append, as for audit_events (see
-- there for why the gateway group's UPDATE and DELETE are revoked and the
-- role-existence gate).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT ON telemetry_events TO ava_runner;
        GRANT EXECUTE ON FUNCTION public.ensure_telemetry_event_partitions(INT) TO ava_runner;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_gateway') THEN
        REVOKE UPDATE, DELETE ON telemetry_events FROM ava_gateway;
    END IF;
END $$;
