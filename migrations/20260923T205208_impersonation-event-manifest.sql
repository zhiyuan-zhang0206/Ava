-- Protocol-v1 leases opt in at creation; old rows remain legacy and cannot be
-- inferred as empty manifests.
ALTER TABLE agent_impersonations
    ADD COLUMN event_delivery_protocol_version SMALLINT
        CHECK (event_delivery_protocol_version = 1),
    ADD COLUMN manifest_admission_closed_at TIMESTAMPTZ,
    ADD COLUMN manifest_frozen_at TIMESTAMPTZ,
    ADD COLUMN manifest_digest TEXT,
    ADD COLUMN manifest_item_count BIGINT,
    ADD COLUMN manifest_envelope_floor_at TIMESTAMPTZ,
    ADD COLUMN event_delivery_pending_reason TEXT
        CHECK (event_delivery_pending_reason IN (
            'awaiting_session_end', 'awaiting_participant_seal', 'capture_failed',
            'awaiting_indexed_ids', 'manifest_mismatch', 'retention_loss'
        )),
    ADD COLUMN event_delivery_retention_horizon_at TIMESTAMPTZ,
    ADD COLUMN event_delivery_integrity_alerted_at TIMESTAMPTZ,
    ADD CONSTRAINT agent_impersonations_manifest_frozen_check CHECK (
        manifest_frozen_at IS NULL OR (
            automatic AND event_delivery_protocol_version = 1
            AND manifest_admission_closed_at IS NOT NULL
            AND manifest_digest IS NOT NULL
            AND manifest_item_count IS NOT NULL
            AND manifest_envelope_floor_at IS NOT NULL
        )
    );

-- Local controller receipts have one open -> sealed/failed transition. Items
-- are append-only and cannot be changed after their event bytes are captured.
CREATE TABLE agent_impersonation_event_participants (
    lease_id UUID NOT NULL REFERENCES agent_impersonations(id) ON DELETE RESTRICT,
    source_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'sealed', 'failed')),
    opened_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    sealed_at TIMESTAMPTZ,
    failure_reason TEXT,
    item_count BIGINT,
    manifest_digest TEXT,
    PRIMARY KEY (lease_id, source_key),
    CHECK (
        (state = 'open' AND sealed_at IS NULL AND failure_reason IS NULL)
        OR (state = 'sealed' AND sealed_at IS NOT NULL AND failure_reason IS NULL)
        OR (state = 'failed' AND failure_reason IS NOT NULL)
    )
);
CREATE TABLE agent_impersonation_event_participant_items (
    lease_id UUID NOT NULL,
    source_key TEXT NOT NULL,
    event_key TEXT NOT NULL,
    event_kind TEXT NOT NULL CHECK (event_kind IN ('sdk_call', 'api_event')),
    event_at TIMESTAMPTZ NOT NULL,
    line_sha256 TEXT NOT NULL CHECK (line_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (lease_id, source_key, event_key),
    FOREIGN KEY (lease_id, source_key)
        REFERENCES agent_impersonation_event_participants(lease_id, source_key)
        ON DELETE RESTRICT
);

-- A central transaction is itself a sealed producer. Its origin key makes an
-- inbound/outbox retry idempotent before the post-commit telemetry enqueue.
CREATE TABLE agent_impersonation_event_expected_receipts (
    lease_id UUID NOT NULL REFERENCES agent_impersonations(id) ON DELETE RESTRICT,
    origin_kind TEXT NOT NULL,
    origin_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (lease_id, origin_kind, origin_id)
);
CREATE TABLE agent_impersonation_event_expected_items (
    lease_id UUID NOT NULL REFERENCES agent_impersonations(id) ON DELETE RESTRICT,
    event_key TEXT NOT NULL,
    event_kind TEXT NOT NULL CHECK (event_kind IN ('sdk_call', 'api_event')),
    event_at TIMESTAMPTZ NOT NULL,
    line_sha256 TEXT NOT NULL CHECK (line_sha256 ~ '^[0-9a-f]{64}$'),
    origin_kind TEXT NOT NULL,
    origin_id BIGINT NOT NULL,
    PRIMARY KEY (lease_id, event_key),
    FOREIGN KEY (lease_id, origin_kind, origin_id)
        REFERENCES agent_impersonation_event_expected_receipts(lease_id, origin_kind, origin_id)
        ON DELETE RESTRICT
);
CREATE INDEX agent_impersonation_event_participant_items_envelope
    ON agent_impersonation_event_participant_items(lease_id, event_at);
CREATE INDEX agent_impersonation_event_expected_items_envelope
    ON agent_impersonation_event_expected_items(lease_id, event_at);
CREATE INDEX agent_impersonations_manifest_pending
    ON agent_impersonations(machine, events_next_read_at)
    WHERE automatic AND event_delivery_protocol_version = 1
      AND events_completed_at IS NULL;

CREATE FUNCTION preserve_impersonation_event_protocol_version() RETURNS trigger AS $$
BEGIN
    IF NEW.event_delivery_protocol_version IS DISTINCT FROM OLD.event_delivery_protocol_version THEN
        RAISE EXCEPTION 'Impersonation event delivery protocol version is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER agent_impersonations_preserve_event_protocol_version
    BEFORE UPDATE OF event_delivery_protocol_version ON agent_impersonations
    FOR EACH ROW EXECUTE FUNCTION preserve_impersonation_event_protocol_version();

CREATE FUNCTION preserve_impersonation_event_participant() RETURNS trigger AS $$
BEGIN
    IF OLD.state <> 'open'
       OR NEW.lease_id <> OLD.lease_id
       OR NEW.source_key <> OLD.source_key
       OR NEW.opened_at <> OLD.opened_at
       OR NEW.state NOT IN ('sealed', 'failed') THEN
        RAISE EXCEPTION 'Impersonation event receipts are permanent after closure';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER agent_impersonation_event_participants_preserve_history
    BEFORE UPDATE OR DELETE ON agent_impersonation_event_participants
    FOR EACH ROW EXECUTE FUNCTION preserve_impersonation_event_participant();
CREATE TRIGGER agent_impersonation_event_participant_items_preserve_history
    BEFORE UPDATE OR DELETE ON agent_impersonation_event_participant_items
    FOR EACH ROW EXECUTE FUNCTION preserve_impersonation_history();
CREATE TRIGGER agent_impersonation_event_expected_receipts_preserve_history
    BEFORE UPDATE OR DELETE ON agent_impersonation_event_expected_receipts
    FOR EACH ROW EXECUTE FUNCTION preserve_impersonation_history();
CREATE TRIGGER agent_impersonation_event_expected_items_preserve_history
    BEFORE UPDATE OR DELETE ON agent_impersonation_event_expected_items
    FOR EACH ROW EXECUTE FUNCTION preserve_impersonation_history();

-- Runner callers may close admission but cannot write the gate, frozen
-- aggregate, protocol version, or completion stamp directly.
CREATE FUNCTION public.close_impersonation_event_manifest_admission(p_lease_id UUID)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    UPDATE public.agent_impersonations
    SET manifest_admission_closed_at = clock_timestamp()
    WHERE id = p_lease_id
      AND automatic
      AND event_delivery_protocol_version = 1
      AND manifest_admission_closed_at IS NULL;
    RETURN FOUND;
END;
$function$;

-- Receipts remain append-only to runners. This narrowly owns their one legal
-- open -> sealed/failed transition rather than granting table UPDATE.
CREATE FUNCTION public.seal_impersonation_event_participant(
    p_lease_id UUID,
    p_source_key TEXT,
    p_state TEXT,
    p_failure_reason TEXT,
    p_item_count BIGINT,
    p_digest TEXT
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE actual_count BIGINT;
BEGIN
    IF p_state NOT IN ('sealed', 'failed') THEN
        RAISE EXCEPTION 'Receipt transition must seal or fail';
    END IF;
    PERFORM 1 FROM public.agent_impersonation_event_participants
    WHERE lease_id=p_lease_id AND source_key=p_source_key AND state='open' FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Receipt is not open';
    END IF;
    IF p_state = 'failed' THEN
        IF p_failure_reason IS NULL THEN
            RAISE EXCEPTION 'Failed receipt requires a reason';
        END IF;
        UPDATE public.agent_impersonation_event_participants
        SET state='failed', failure_reason=p_failure_reason
        WHERE lease_id=p_lease_id AND source_key=p_source_key;
        RETURN;
    END IF;
    SELECT count(*) INTO actual_count
    FROM public.agent_impersonation_event_participant_items
    WHERE lease_id=p_lease_id AND source_key=p_source_key;
    IF actual_count <> p_item_count OR p_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'Receipt aggregate does not match its append-only items';
    END IF;
    UPDATE public.agent_impersonation_event_participants
    SET state='sealed', sealed_at=clock_timestamp(), item_count=p_item_count, manifest_digest=p_digest
    WHERE lease_id=p_lease_id AND source_key=p_source_key;
END;
$function$;

-- The caller derives the digest from the append-only rows while holding this
-- lease lock. This narrow writer validates the count and owns every frozen
-- aggregate field, so ordinary runner UPDATE grants cannot manufacture one.
CREATE FUNCTION public.freeze_impersonation_event_manifest(
    p_lease_id UUID,
    p_digest TEXT,
    p_count BIGINT,
    p_floor TIMESTAMPTZ
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE actual_count BIGINT;
BEGIN
    PERFORM 1 FROM public.agent_impersonations
    WHERE id = p_lease_id
      AND automatic
      AND event_delivery_protocol_version = 1
      AND manifest_admission_closed_at IS NOT NULL
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Manifest freeze requires a closed protocol-v1 lease';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.agent_impersonation_event_participants
        WHERE lease_id = p_lease_id AND state <> 'sealed'
    ) THEN
        RAISE EXCEPTION 'Manifest freeze requires every local receipt to seal';
    END IF;
    SELECT count(DISTINCT event_key) INTO actual_count FROM (
        SELECT event_key FROM public.agent_impersonation_event_participant_items
        WHERE lease_id = p_lease_id
        UNION ALL
        SELECT event_key FROM public.agent_impersonation_event_expected_items
        WHERE lease_id = p_lease_id
    ) expected;
    IF actual_count <> p_count OR p_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'Manifest freeze aggregate does not match the ledger';
    END IF;
    UPDATE public.agent_impersonations
    SET manifest_frozen_at = clock_timestamp(),
        manifest_digest = p_digest,
        manifest_item_count = p_count,
        manifest_envelope_floor_at = p_floor,
        event_delivery_pending_reason = 'awaiting_indexed_ids'
    WHERE id = p_lease_id;
END;
$function$;

-- This is the only completion-column writer. The agent host supplies its
-- machine through transaction-local `ava.impersonation_machine`; external
-- controllers never receive the function surface.
CREATE FUNCTION public.certify_impersonation_event_delivery(p_lease_id UUID)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE lease public.agent_impersonations%ROWTYPE;
DECLARE entry_no BIGINT;
DECLARE participants BIGINT;
DECLARE sdk_count BIGINT;
DECLARE api_count BIGINT;
BEGIN
    SELECT * INTO lease FROM public.agent_impersonations WHERE id = p_lease_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Impersonation lease does not exist';
    END IF;
    IF lease.events_completed_at IS NOT NULL THEN
        RETURN TRUE;
    END IF;
    IF current_setting('ava.impersonation_machine', true) IS DISTINCT FROM lease.machine THEN
        RAISE EXCEPTION 'Only the lease-owning agent host may certify event delivery';
    END IF;
    IF NOT lease.automatic OR lease.event_delivery_protocol_version <> 1
       OR lease.ended_at IS NULL OR lease.manifest_admission_closed_at IS NULL
       OR lease.manifest_frozen_at IS NULL THEN
        RAISE EXCEPTION 'Event delivery certification requires a frozen protocol-v1 lease';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.agent_impersonation_event_participants
        WHERE lease_id = p_lease_id AND state <> 'sealed'
    ) THEN
        RAISE EXCEPTION 'Event delivery certification requires sealed local receipts';
    END IF;
    IF EXISTS (
        SELECT event_key FROM (
            SELECT event_key, line_sha256, event_kind
            FROM public.agent_impersonation_event_participant_items WHERE lease_id = p_lease_id
            UNION ALL
            SELECT event_key, line_sha256, event_kind
            FROM public.agent_impersonation_event_expected_items WHERE lease_id = p_lease_id
        ) expected
        GROUP BY event_key HAVING count(DISTINCT line_sha256 || ':' || event_kind) <> 1
    ) THEN
        RAISE EXCEPTION 'Manifest contains conflicting duplicate event identities';
    END IF;
    IF EXISTS (
        WITH expected AS (
            SELECT event_key, min(event_kind) AS event_kind FROM (
                SELECT event_key, event_kind FROM public.agent_impersonation_event_participant_items
                WHERE lease_id = p_lease_id
                UNION ALL
                SELECT event_key, event_kind FROM public.agent_impersonation_event_expected_items
                WHERE lease_id = p_lease_id
            ) all_expected GROUP BY event_key
        ), actual AS (
            SELECT event_key, kind AS event_kind FROM public.agent_impersonation_entries
            WHERE lease_id = p_lease_id AND kind IN ('sdk_call', 'api_event')
        )
        (SELECT event_key, event_kind FROM expected EXCEPT SELECT event_key, event_kind FROM actual)
        UNION ALL
        (SELECT event_key, event_kind FROM actual EXCEPT SELECT event_key, event_kind FROM expected)
    ) THEN
        RAISE EXCEPTION 'Manifest differs from durable consumed events';
    END IF;
    UPDATE public.agent_impersonations
    SET events_completed_at = clock_timestamp(),
        events_cursor = NULL,
        handoff_document = NULL,
        event_delivery_pending_reason = NULL
    WHERE id = p_lease_id;
    UPDATE public.agent_impersonations SET next_entry = next_entry + 1
    WHERE id = p_lease_id RETURNING next_entry - 1 INTO entry_no;
    SELECT count(*) INTO participants FROM public.agent_impersonation_event_participants
    WHERE lease_id = p_lease_id;
    SELECT count(*) FILTER (WHERE event_kind = 'sdk_call'),
           count(*) FILTER (WHERE event_kind = 'api_event')
      INTO sdk_count, api_count
    FROM (
        SELECT event_key, min(event_kind) AS event_kind FROM (
            SELECT event_key, event_kind FROM public.agent_impersonation_event_participant_items
            WHERE lease_id = p_lease_id
            UNION ALL
            SELECT event_key, event_kind FROM public.agent_impersonation_event_expected_items
            WHERE lease_id = p_lease_id
        ) all_expected GROUP BY event_key
    ) expected;
    INSERT INTO public.agent_impersonation_entries(lease_id,seq,kind,payload)
    VALUES(p_lease_id,entry_no,'lifecycle',jsonb_build_object(
        'event','event_delivery_complete',
        'manifest_digest',lease.manifest_digest,
        'event_count',lease.manifest_item_count,
        'participant_count',participants,
        'sdk_call_count',sdk_count,
        'api_event_count',api_count
    ));
    RETURN TRUE;
END;
$function$;

REVOKE ALL ON FUNCTION public.close_impersonation_event_manifest_admission(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.seal_impersonation_event_participant(UUID, TEXT, TEXT, TEXT, BIGINT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.freeze_impersonation_event_manifest(UUID, TEXT, BIGINT, TIMESTAMPTZ) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.certify_impersonation_event_delivery(UUID) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        REVOKE INSERT, UPDATE ON agent_impersonations FROM ava_runner;
        REVOKE UPDATE ON agent_impersonation_event_participants FROM ava_runner;
        GRANT INSERT (
            id,agent_id,source,machine,reason,status,ttl_seconds,expires_at,
            relay_provider,relay_thread_id,relay_codex_remote,relay_token_hash,
            relay_batch_window_seconds,name,executor_name,process_metadata,automatic,
            ack_window_seconds,max_delivery_attempts,event_delivery_protocol_version
        ) ON agent_impersonations TO ava_runner;
        GRANT UPDATE (
            status,expires_at,rejection_reason,summary_inbound_id,summary,
            accepted_generation,accepted_owner,consent_version,activated_at,ended_at,
            plugin_delta,delta_version,applied_version,relay_token_hash,relay_heartbeat_at,
            relay_last_failure_at,relay_minted_at,relay_minted_generation,relay_minted_owner,
            events_cursor,events_next_read_at,handoff_document,handoff_path,handoff_applied_at,
            next_entry,event_delivery_pending_reason
        ) ON agent_impersonations TO ava_runner;
        GRANT SELECT, INSERT ON agent_impersonation_event_participants TO ava_runner;
        GRANT SELECT, INSERT ON agent_impersonation_event_participant_items TO ava_runner;
        GRANT EXECUTE ON FUNCTION public.close_impersonation_event_manifest_admission(UUID) TO ava_runner;
        GRANT EXECUTE ON FUNCTION public.seal_impersonation_event_participant(UUID, TEXT, TEXT, TEXT, BIGINT, TEXT) TO ava_runner;
        GRANT EXECUTE ON FUNCTION public.freeze_impersonation_event_manifest(UUID, TEXT, BIGINT, TIMESTAMPTZ) TO ava_runner;
        GRANT EXECUTE ON FUNCTION public.certify_impersonation_event_delivery(UUID) TO ava_runner;
    END IF;
END $$;
