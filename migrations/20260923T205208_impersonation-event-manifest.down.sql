DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM agent_impersonations
        WHERE event_delivery_protocol_version IS NOT NULL
           OR manifest_admission_closed_at IS NOT NULL
           OR manifest_frozen_at IS NOT NULL
    ) OR EXISTS (SELECT 1 FROM agent_impersonation_event_participants)
      OR EXISTS (SELECT 1 FROM agent_impersonation_event_expected_receipts) THEN
        RAISE EXCEPTION 'Cannot remove impersonation event manifests with recorded protocol evidence';
    END IF;
END $$;

DROP FUNCTION IF EXISTS certify_impersonation_event_delivery(UUID);
DROP FUNCTION IF EXISTS freeze_impersonation_event_manifest(UUID, TEXT, BIGINT, TIMESTAMPTZ);
DROP FUNCTION IF EXISTS seal_impersonation_event_participant(UUID, TEXT, TEXT, TEXT, BIGINT, TEXT);
DROP FUNCTION IF EXISTS close_impersonation_event_manifest_admission(UUID);
DROP TABLE IF EXISTS agent_impersonation_event_expected_items;
DROP TABLE IF EXISTS agent_impersonation_event_expected_receipts;
DROP TABLE IF EXISTS agent_impersonation_event_participant_items;
DROP TABLE IF EXISTS agent_impersonation_event_participants;
ALTER TABLE agent_impersonations
    DROP CONSTRAINT agent_impersonations_manifest_frozen_check,
    DROP COLUMN event_delivery_integrity_alerted_at,
    DROP COLUMN event_delivery_retention_horizon_at,
    DROP COLUMN event_delivery_pending_reason,
    DROP COLUMN manifest_envelope_floor_at,
    DROP COLUMN manifest_item_count,
    DROP COLUMN manifest_digest,
    DROP COLUMN manifest_frozen_at,
    DROP COLUMN manifest_admission_closed_at,
    DROP COLUMN event_delivery_protocol_version;
