-- Align database comments, trigger settings, and capability-group grants with the Meiva rename.

CREATE OR REPLACE FUNCTION record_impersonation_lifecycle() RETURNS trigger AS $$
DECLARE entry_no BIGINT;
BEGIN
    IF TG_OP='UPDATE' AND (NEW.status,NEW.expires_at) IS NOT DISTINCT FROM (OLD.status,OLD.expires_at) THEN
        RETURN NEW;
    END IF;
    UPDATE agent_impersonations SET next_entry=next_entry+1 WHERE id=NEW.id RETURNING next_entry-1 INTO entry_no;
    INSERT INTO agent_impersonation_entries(lease_id,seq,kind,payload)
    VALUES(NEW.id,entry_no,'lifecycle',jsonb_build_object(
        'status',NEW.status,'expires_at',NEW.expires_at,'executor_name',NEW.executor_name,
        'session_id',NEW.session_id,'name',NEW.name,'machine',NEW.machine,
        'summary',NEW.summary,'reason',NEW.reason,'rejection_reason',NEW.rejection_reason,
        'source',COALESCE(NULLIF(current_setting('meiva.impersonation_actor',true),''),'system:impersonation'),
        'previous_status',CASE WHEN TG_OP='UPDATE' THEN OLD.status ELSE NULL END,
        'previous_expires_at',CASE WHEN TG_OP='UPDATE' THEN OLD.expires_at ELSE NULL END));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMENT ON TABLE heartbeat_pause_log IS
    'Append-only heartbeat-pause trail: one row per meiva.self.pause_heartbeat call. The latest row supplies Inspector pause duration without telemetry reads.';

COMMENT ON COLUMN agent_pages.serve_dir IS
    'Directory the page server serves, set by meiva.ui.serve(); NULL for meiva.ui.show().';

COMMENT ON TABLE agent_shell_ttls IS
    'Persistent shell sessions whose agent declared a TTL at creation (meiva.shell.sessions.new/run_background ttl=). Reaped by the gateway TTL reaper; rows are removed when reaped or when the session dies (the reaper self-cleans).';

COMMENT ON TABLE agent_shell_ttl_renewals IS
    'Append-only shell-TTL renewal trail: one row per meiva.shell.sessions.renew call, in the same transaction as the deadline UPDATE. prev/new_expires_at carry the before/after deadlines. No FK to agent_shell_ttls — the reaper deletes that row on reclamation while the audit history must survive.';

COMMENT ON TABLE host_deploy_state IS
    'Host-level deploy posture (idle/paused), one row per machine: written by meiva stop, pause, maintenance and start; read by the gateway 503 middleware, meiva status and the deploy window.';

COMMENT ON TABLE upload_delivery_copies IS
    'Retained immutable copy intent and physical-root reservation; MEIVA_HOME is target identity, not quota partition.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_runner') THEN
        GRANT EXECUTE ON FUNCTION public.close_impersonation_event_admission(UUID) TO meiva_runner;
        GRANT EXECUTE ON FUNCTION public.seal_impersonation_event_participant(UUID,TEXT,TEXT,TEXT,BIGINT) TO meiva_runner;
        GRANT SELECT, INSERT, UPDATE ON understanding_chunk_jobs TO meiva_runner;
        GRANT USAGE, SELECT ON SEQUENCE understanding_chunk_jobs_id_seq TO meiva_runner;
        GRANT INSERT ON understanding_chunk_calls TO meiva_runner;
        GRANT USAGE, SELECT ON SEQUENCE understanding_chunk_calls_id_seq TO meiva_runner;
        GRANT SELECT, INSERT, UPDATE ON understanding_group_state TO meiva_runner;
        GRANT INSERT ON understanding_group_calls TO meiva_runner;
        GRANT USAGE, SELECT ON SEQUENCE understanding_group_calls_id_seq TO meiva_runner;
        GRANT SELECT, UPDATE ON understanding_rebuilds TO meiva_runner;
        GRANT DELETE ON understanding_group_state TO meiva_runner;
        GRANT SELECT, INSERT, DELETE ON
            run_timeline_segments, run_timeline_message_facts, run_timeline_blocks TO meiva_runner;
    END IF;
END $$;
