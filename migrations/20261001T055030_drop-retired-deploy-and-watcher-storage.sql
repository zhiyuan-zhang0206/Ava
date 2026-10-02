-- Contract the storage whose readers and writers are gone
-- (decisions/2026-10-01-contract-the-retired-deploy-storage.md). Every object
-- here was left unread and unwritten by an earlier release that shipped the
-- code change first, so this migration removes no behavior; it removes
-- inert schema.
--
--   * the publication row-lock function, which no admission calls;
--   * deployment_state's deploy lease, last-outcome and publication-evidence
--     columns (the row and `min_code_version` stay: the code-version gate);
--   * host_deploy_state's updater lease, pause-window anchor and stranded-hold
--     record, and the 'converging' posture (the table and `posture` stay);
--   * the cluster_pin and cluster_last_update singletons;
--   * agents_meta.closed_at (terminate has no closed state) and the
--     agent_watchers registry (watchers are never restarted).
--
-- Dropped values are not carried anywhere: the operator saves the frozen rows
-- before the rollout (see the PR's rollout notes).

DROP FUNCTION IF EXISTS public.lock_runtime_publication_admission();

ALTER TABLE deployment_state
    DROP COLUMN IF EXISTS phase,
    DROP COLUMN IF EXISTS kind,
    DROP COLUMN IF EXISTS holder,
    DROP COLUMN IF EXISTS acquired_at,
    DROP COLUMN IF EXISTS expires_at,
    DROP COLUMN IF EXISTS settle_hosts,
    DROP COLUMN IF EXISTS settle_note,
    DROP COLUMN IF EXISTS settle_started_at,
    DROP COLUMN IF EXISTS outcome,
    DROP COLUMN IF EXISTS failing_step,
    DROP COLUMN IF EXISTS started_at,
    DROP COLUMN IF EXISTS ended_at,
    DROP COLUMN IF EXISTS origin,
    DROP COLUMN IF EXISTS target_sha,
    DROP COLUMN IF EXISTS observed_by,
    DROP COLUMN IF EXISTS log_path,
    DROP COLUMN IF EXISTS pin_advanced,
    DROP COLUMN IF EXISTS managed_writer_evidence;

COMMENT ON TABLE deployment_state IS
    'Cluster singleton row (id=1) holding the code-version gate''s minimum, min_code_version. The deploy lease, last-update outcome and publication evidence it once carried were retired (decisions/2026-10-01-contract-the-retired-deploy-storage.md).';

ALTER TABLE host_deploy_state
    DROP COLUMN IF EXISTS updater_lease_expires_at,
    DROP COLUMN IF EXISTS paused_at,
    DROP COLUMN IF EXISTS stranded_hold_since,
    DROP COLUMN IF EXISTS stranded_hold_reason,
    DROP COLUMN IF EXISTS stranded_hold_attempts,
    DROP COLUMN IF EXISTS stranded_hold_attempted_at,
    DROP COLUMN IF EXISTS stranded_hold_recovery_note;

UPDATE host_deploy_state SET posture = 'idle' WHERE posture = 'converging';
ALTER TABLE host_deploy_state DROP CONSTRAINT IF EXISTS host_deploy_state_posture_check;
ALTER TABLE host_deploy_state
    ADD CONSTRAINT host_deploy_state_posture_check CHECK (posture IN ('idle', 'paused'));

COMMENT ON TABLE host_deploy_state IS
    'Host-level deploy posture (idle/paused), one row per machine: written by ava stop, pause, maintenance and start; read by the gateway 503 middleware, ava status and the deploy window.';

DROP TABLE IF EXISTS cluster_pin;
DROP TABLE IF EXISTS cluster_last_update;

ALTER TABLE agents_meta DROP COLUMN IF EXISTS closed_at;
DROP TABLE IF EXISTS agent_watchers;
