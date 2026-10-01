-- Restore the SHAPE of the retired deploy and watcher storage. Values are not
-- restored: every dropped column returns at its default (NULL, 'stable', 0,
-- FALSE), the singleton tables return with their seed row, and agent_watchers
-- returns empty. The operator keeps the pre-rollout copy of the frozen rows.
-- Restored columns append after the surviving ones, so column order differs
-- from the pre-migration table; every reader names its columns.
--
-- Roll the code back with (or after) this file: code before the drop reads and
-- writes these objects and fails on a schema without them.

ALTER TABLE agents_meta ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;

COMMENT ON COLUMN agents_meta.closed_at IS
    'Retired by the 2026-09-27 "terminate has no closed state" ruling '
    '(decisions/2026-09-27-terminate-has-no-closed-state.md): no code reads or '
    'writes this column any more. A pre-ruling non-NULL value is inert — a '
    'terminated agent resurrects on any new message like any other. Kept under '
    'expand-contract; DROP is a later migration.';

CREATE TABLE IF NOT EXISTS agent_watchers (
    session_id     INTEGER NOT NULL,
    agent_id       BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    PRIMARY KEY (agent_id, session_id),
    kind           TEXT NOT NULL CHECK (kind IN ('at', 'cron', 'launch')),
    name           TEXT NOT NULL,
    message        TEXT,
    fires_at       TIMESTAMPTZ,
    cron_expr      TEXT,
    cron_timezone  TEXT,
    cron_end_at    TIMESTAMPTZ,
    timeout_secs   REAL,
    notify         TEXT NOT NULL DEFAULT 'always'
                   CHECK (notify IN ('always', 'failure', 'agent')),
    template_version INTEGER,
    generation     TEXT,
    status         TEXT NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'rebuilt', 'missed', 'reaped')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_watchers_agent_idx ON agent_watchers (agent_id);

COMMENT ON TABLE agent_watchers IS
    'Watcher registry: every ava.watcher.at/cron/launch session, keyed by its shell-session id. Written at spawn, deleted on clean exit; a killed watcher leaves its row and the agent boot reconcile rebuilds current-generation cron / marks missed one-shots. Superseded generation rows are retained as reaped history (R1 wave, Task #1021).';

CREATE TABLE IF NOT EXISTS cluster_pin (
    id         INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    target_sha TEXT,
    updated_at TIMESTAMPTZ,
    updated_by TEXT,
    last_known_good_sha TEXT,
    last_known_good_at TIMESTAMPTZ,
    pending_known_good_sha TEXT,
    pending_known_good_at TIMESTAMPTZ
);
INSERT INTO cluster_pin (id, target_sha) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS cluster_last_update (
    id           INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    target_sha   TEXT,
    origin       TEXT,
    holder       TEXT,
    started_at   TIMESTAMPTZ,
    ended_at     TIMESTAMPTZ,
    outcome      TEXT,
    failing_step TEXT,
    observed_by  TEXT,
    log_path     TEXT,
    pin_advanced BOOLEAN NOT NULL DEFAULT FALSE
);
INSERT INTO cluster_last_update (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

ALTER TABLE host_deploy_state
    ADD COLUMN IF NOT EXISTS updater_lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS paused_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS stranded_hold_since TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS stranded_hold_reason TEXT,
    ADD COLUMN IF NOT EXISTS stranded_hold_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS stranded_hold_attempted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS stranded_hold_recovery_note TEXT;

COMMENT ON COLUMN host_deploy_state.stranded_hold_since IS
    'When this host''s pause became a STRANDED maintenance hold — an ownerless '
    'hold left by a failed updater leg; NULL when no such record. Stamped once '
    'by the pause controller and preserved until the verdict clears (task #3132).';

COMMENT ON COLUMN host_deploy_state.stranded_hold_reason IS
    'The updater verdict that left the stranded hold (e.g. "updater exited '
    'rc=1"); display/alert context, never a judgment input (task #3132).';

COMMENT ON COLUMN host_deploy_state.stranded_hold_attempts IS
    'Automatic recovery attempts this stranded-hold episode has consumed (task '
    '#3142); reset when the record clears.';

COMMENT ON COLUMN host_deploy_state.stranded_hold_attempted_at IS
    'Postgres timestamp of the last reserved automatic recovery attempt (task '
    '#3142).';

COMMENT ON COLUMN host_deploy_state.stranded_hold_recovery_note IS
    'The latest recovery attempt''s outcome or error summary, for the '
    'operator; display context, never a judgment input (task #3142).';

ALTER TABLE host_deploy_state DROP CONSTRAINT IF EXISTS host_deploy_state_posture_check;
ALTER TABLE host_deploy_state
    ADD CONSTRAINT host_deploy_state_posture_check
    CHECK (posture IN ('idle', 'paused', 'converging'));

COMMENT ON TABLE host_deploy_state IS
    'Host-level deploy posture + updater lease, one row per machine (replaces the cluster_paused file, updating.flag, session probing and updater-log-mtime liveness; R1 wave, Task #1021).';

ALTER TABLE deployment_state
    ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT 'stable'
        CHECK (phase IN ('stable', 'updating', 'settling')),
    ADD COLUMN IF NOT EXISTS kind TEXT CHECK (kind IN ('rollout', 'restart', 'update')),
    ADD COLUMN IF NOT EXISTS holder TEXT,
    ADD COLUMN IF NOT EXISTS acquired_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS settle_hosts TEXT[],
    ADD COLUMN IF NOT EXISTS settle_note TEXT,
    ADD COLUMN IF NOT EXISTS settle_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS outcome TEXT CHECK (outcome IN
        ('clean', 'recovered', 'incomplete', 'aborted', 'running', 'orphaned')),
    ADD COLUMN IF NOT EXISTS failing_step TEXT,
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS origin TEXT,
    ADD COLUMN IF NOT EXISTS target_sha TEXT,
    ADD COLUMN IF NOT EXISTS observed_by TEXT,
    ADD COLUMN IF NOT EXISTS log_path TEXT,
    ADD COLUMN IF NOT EXISTS pin_advanced BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS managed_writer_evidence JSONB;

COMMENT ON TABLE deployment_state IS
    'Cluster-level deployment state: phase/kind + the deploy lease + last outcome — the single authority for "is a deploy running, of what kind" (replaces cluster_update_lock + session probing; R1 wave, Task #1021).';

COMMENT ON COLUMN deployment_state.managed_writer_evidence IS
    'Versioned operation-bound managed-writer closure evidence; NULL is unknown, never permission.';

-- Runtime admission must serialize with rollout writers, but agent processes
-- dial as ava_runner and must not receive UPDATE on deployment_state. This
-- fixed security-definer operation grants only the row lock; callers still
-- read the publication columns through their ordinary SELECT privilege.
CREATE OR REPLACE FUNCTION public.lock_runtime_publication_admission()
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    SELECT NULL::void
    FROM public.deployment_state
    WHERE id = 1
    FOR UPDATE
$function$;

REVOKE ALL ON FUNCTION public.lock_runtime_publication_admission() FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT EXECUTE ON FUNCTION public.lock_runtime_publication_admission() TO ava_runner;
    END IF;
END
$$;

COMMENT ON FUNCTION public.lock_runtime_publication_admission() IS
    'Take the deployment publication row lock for least-privilege runtime admission without granting rollout writes.';
