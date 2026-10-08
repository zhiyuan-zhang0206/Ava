ALTER TABLE alerts ADD COLUMN notified_revision BIGINT NOT NULL DEFAULT 0 CHECK (notified_revision >= 0);
COMMENT ON COLUMN alerts.notified_revision IS
    'Native revision completed by at least one real SENT channel; legacy notified_at never populates this fact.';
ALTER TABLE alert_notification_groups DROP CONSTRAINT alert_notification_groups_origin_check;
ALTER TABLE alert_notification_groups ADD CONSTRAINT alert_notification_groups_origin_check
    CHECK (origin IN ('shadow','native-v1'));
COMMENT ON TABLE alert_notification_groups IS
    'Immutable ingest groups. Only native-v1 creation origin qualifies for new acceptance; shadow history is never promoted or automatically dispatched.';
ALTER TABLE im_bridge_outbound_intents ALTER COLUMN agent_id DROP NOT NULL;
ALTER TABLE im_bridge_outbound_intents DROP CONSTRAINT im_bridge_outbound_intents_source_kind_check;
ALTER TABLE im_bridge_outbound_intents ADD CONSTRAINT im_bridge_outbound_intents_source_kind_check
    CHECK (source_kind IN ('message','inbound','notice','alert_group'));
ALTER TABLE im_bridge_outbound_intents ADD CONSTRAINT im_bridge_outbound_context
    CHECK ((source_kind='alert_group' AND agent_id IS NULL AND block_idx=0 AND replay_id='')
           OR (source_kind<>'alert_group' AND agent_id IS NOT NULL AND agent_id>0));
DO $$ DECLARE identity_name TEXT;
BEGIN
    SELECT conname INTO identity_name FROM pg_constraint
    WHERE conrelid='im_bridge_outbound_intents'::regclass AND contype='u';
    EXECUTE format('ALTER TABLE im_bridge_outbound_intents DROP CONSTRAINT %I',identity_name);
END $$;
ALTER TABLE im_bridge_outbound_intents ADD CONSTRAINT im_bridge_outbound_identity
    UNIQUE NULLS NOT DISTINCT (channel,account_id,chat_id,agent_id,source_kind,source_id,block_idx,replay_id);
CREATE TABLE im_bridge_alert_acceptances (
    group_id BIGINT PRIMARY KEY CHECK (group_id > 0),
    request JSONB NOT NULL,
    decisions JSONB NOT NULL,
    intent_ids BIGINT[] NOT NULL CHECK (cardinality(intent_ids)>0),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE im_bridge_alert_acceptances IS
    'Frozen available recipient subset and retained unavailable channel decisions, accepted atomically with shared intents. No FK to mutable/retained source or queue, no expiry, no retrospective fanout.';
