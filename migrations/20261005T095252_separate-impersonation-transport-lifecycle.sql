ALTER TABLE agent_impersonations
    ADD COLUMN relay_generation BIGINT NOT NULL DEFAULT 0 CHECK (relay_generation >= 0),
    ADD COLUMN relay_identity JSONB,
    ADD COLUMN relay_degraded_reason TEXT,
    ADD COLUMN relay_degraded_at TIMESTAMPTZ,
    ADD COLUMN terminal_notice_snapshot JSONB,
    ADD COLUMN terminal_notice_pending_at TIMESTAMPTZ,
    ADD COLUMN terminal_notice_accepted_at TIMESTAMPTZ,
    ADD COLUMN terminal_notice_attempt_id UUID,
    ADD COLUMN terminal_notice_attempt_at TIMESTAMPTZ,
    ADD COLUMN terminal_notice_attempts INTEGER NOT NULL DEFAULT 0 CHECK (terminal_notice_attempts >= 0),
    ADD COLUMN terminal_notice_error TEXT,
    ADD COLUMN terminal_notice_unsupported_at TIMESTAMPTZ;

CREATE OR REPLACE FUNCTION mark_impersonation_terminal_notice() RETURNS trigger AS $$
BEGIN
    IF OLD.status IN ('requested','accepted','active')
       AND NEW.status NOT IN ('requested','accepted','active') THEN
        NEW.terminal_notice_pending_at := clock_timestamp();
        NEW.terminal_notice_snapshot := jsonb_build_object(
            'lease_id', OLD.id, 'session_id', OLD.session_id, 'agent_id', OLD.agent_id,
            'provider', OLD.relay_provider, 'thread_id', OLD.relay_thread_id,
            'endpoint', OLD.relay_codex_remote, 'status', NEW.status,
            'reason', NEW.rejection_reason, 'ended_at', NEW.ended_at);
    ELSIF NEW.terminal_notice_snapshot IS DISTINCT FROM OLD.terminal_notice_snapshot
       OR NEW.terminal_notice_pending_at IS DISTINCT FROM OLD.terminal_notice_pending_at THEN
        RAISE EXCEPTION 'Terminal notice destination and end snapshot are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agent_impersonations_terminal_notice BEFORE UPDATE ON agent_impersonations
    FOR EACH ROW EXECUTE FUNCTION mark_impersonation_terminal_notice();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ava_runner') THEN
        GRANT UPDATE (relay_generation,relay_identity,relay_degraded_reason,relay_degraded_at,terminal_notice_accepted_at,terminal_notice_attempt_id,terminal_notice_attempt_at,terminal_notice_attempts,terminal_notice_error,terminal_notice_unsupported_at) ON agent_impersonations TO ava_runner;
    END IF;
END $$;
