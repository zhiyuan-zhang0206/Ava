-- Ava schema — the squashed **baseline**: the full current schema a fresh DB
-- bootstraps from. Since the 2026-07-19 re-baseline this file IS the source of
-- truth for the current schema (history through sequential 0001..0081 was folded
-- in). A schema change ships as a `migrations/YYYYMMDDTHHMMSS_*.sql` delta AND is
-- reflected here, so this file always describes the latest schema.
--
-- Who uses this file (fresh-DB bootstrap — applies the whole baseline):
--   - `base.cluster.provision_database` — a new cluster's DB is created and this
--     file applied as the owning role (the real production fresh-bootstrap path)
--   - `tests/conftest.py` / `tests/e2e/conftest.py` execute the whole file each session
--     when creating an independent test DB
--   - `tests/test_agent_status_schema_sync.py` directly parses this file to verify CHECK
--     constraint sync
--
-- Who does not use this file:
--   - `base.deploy.schema.migrations.apply_pending_migrations` — it applies the post-baseline
--     `migrations/*.sql` deltas one by one, doesn't read this file. A fresh DB is
--     already at the baseline (via one of the paths above), so apply then only
--     runs deltas not already folded into this file's applied-set seed;
--     production self-update goes through it.
--
-- Postgres trust boundary:
--   - The `ava_gateway` service dials the cluster-main role, which owns this
--     schema and is the only role allowed DDL or unbounded application writes.
--   - `ava_runner` is a separate LOGIN NOSUPERUSER role. It has SELECT over the
--     public schema but may write only runner-local surfaces: inbound_messages
--     (claiming and self-lifecycle); agents_meta and agents (process state and
--     its own label); machine_units, machines, and host_deploy_state (unit
--     registration and deploy posture); api_idempotency (runner /ops dedupe);
--     agent_tasks, agent_pages, and agent_shell_ttls (SDK
--     lifecycle); heartbeat_pause_log (pause history); and the LangGraph
--     checkpoints, checkpoint_blobs, and checkpoint_writes (agent state).
--   - `base.cluster.authority.groups.ensure_groups` is the sole grant list and
--     re-affirms it after migrations. All other writes travel through
--     `ava_gateway`, so runner credentials cannot create agents, run DDL, or
--     mutate gateway-owned tables.
--
-- LangGraph PostgresSaver creates only at fresh install; later versions are
-- mirrored by Ava timestamp migrations:
--   checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations
-- Here we only manage our own tables:
--   agents            — agent identity + label (id also doubles as the LangGraph thread_id,
--                       the value is fed via str(id) into config["configurable"]["thread_id"];
--                       compact modifies messages in place, **does not create a new agent**)
--   agents_meta       — agent process lifecycle (1:1 with agents.id).
--                       spawn_agent INSERT unclaimed 'idling' → process claim
--                       UPDATE to 'running' → claim wait returns to 'idling' →
--                       got batch → 'running' → terminate path UPDATE 'terminated'
--   inbound_messages  — any trigger entering the agent (kinds enumerated in the CHECK constraint below)

-- ─────────────── agents ───────────────
-- The agents table is the source of truth for agent identity. id also doubles as LangGraph's thread_id
-- (PostgresSaver writes str(agents.id) into the checkpoints.thread_id column; "thread_id" is a wire constraint
-- internal to the framework, but externally we standardize on the "agent" naming).
--
-- label NULL = "unset": when spawn carries a prompt, the gateway BackgroundTask runs the LLM
-- to generate a short name, UPDATE WHERE label IS NULL CAS writes it in; on failure / spawn without prompt
-- the label stays NULL, and the frontend shows fallback "#N".
--
-- label_user_set: a sticky bit for whether the user has actively PATCHed. Any direction (set non-empty / reset
-- back to NULL) sets true; the LLM CAS adds `AND NOT label_user_set` — after a user reset, the LLM no
-- longer overwrites (otherwise it would break the "I want default to show #N" intent).
CREATE TABLE agents (
    impersonation_index BIGINT NOT NULL DEFAULT 0,
    id              BIGSERIAL PRIMARY KEY,
    label           TEXT,
    label_user_set  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- ids are 1-based (BIGSERIAL auto-assigns from 1); a non-positive id can
    -- only come from a buggy direct DB write. Reject it at the storage layer so
    -- no such write lands a ghost row, complementing the gateway's gt=0 API
    -- guard. (`ava.AGENT_ID`'s pre-assignment placeholder is None, outside this
    -- id space, so a premature write fails on NOT NULL rather than reaching
    -- here.)
    CONSTRAINT agents_id_positive CHECK (id > 0)
);

-- ─────────────── agents_meta ───────────────
-- Agent process lifecycle table. 1:1 with agents — an agent process is forever bound to
-- a single agents row, and the concepts "agent" and "conversation" are unified.
--
-- State machine:
--   idling     — either unclaimed (pid / started_at / lease are NULL) or a claimed process waiting
--                for inbound work
--   running    — a claimed process, including bootstrap and active execution;
--                running an LLM / exec turn (any node: claim got a batch / before_llm / llm /
--                before_exec / exec / after_exec)
--   terminated — UPDATE before graceful exit, after the process receives a terminate inbound
--
-- After launch, `_launch_agent_process` polls pid to confirm the child claimed the row. No claim within the
-- timeout raises: on the spawn path, the raise is propagated up so the caller sees it (the row remains
-- unclaimed idling for the boot reaper);
-- on the resurrect/respawn path, `_launch_or_force_terminated` catches it and changes to 'terminated'
-- so the caller can retry. This avoids "spawn returncode 0 but child crash" leaving permanent unclaimed rows.
--
-- Wake paths set pid, started_at, and lease to NULL before launch. `claim_agent_row` writes all three
-- atomically, avoiding previous-session ghost data in ops ps/kill views.
--
-- A "terminated" row can be UPDATEd back to 'idling' by resurrect_agent and respawned as a new process
-- (agent + checkpoints + messages all present, the agent picks up from its last state when woken).
--
-- Lineage fields:
--   spawner                   identifier of the entity that triggered spawn, as a string:
--                             - "user"          — UI button / scripts/start_agent
--                             - "agent:<id>"    — peer agent via ava.agents.spawn(...)
--                             - "<other>"       — claude-code / cli / any external trigger
--                             root also uses "user" — no NULL, simplifies frontend tree construction
--                             For a fork this column records the fork SOURCE (the lineage
--                             parent, "agent:<fork_source_agent_id>") — NOT the executor who
--                             triggered the fork; the executor stays traceable via the fork
--                             event's `source` and the fork prompt inbound's source (user
--                             ruling 2026-08-28, task #1879)
--   born_spawner              birth-time original spawner; immutable audit lineage that
--                             folding must never rewrite. Forks record their source as
--                             "agent:<fork_source_agent_id>"; backfilled legacy rows are
--                             the best-known source from fork provenance, a timely agent
--                             chat, or the current spawner.
--   fork_source_agent_id      source agent of a fork; NULL for non-fork spawns
--   fork_source_checkpoint_id exact checkpoint id of the source agent (filled when forking);
--                             LangGraph's checkpoints are append-only, so a fork must
--                             point at a specific snapshot rather than "latest" — latest drifts under
--                             concurrent writes, and a fork should be reproducible
CREATE TABLE agents_meta (
    id                         BIGINT PRIMARY KEY REFERENCES agents(id),
    spawner                    TEXT NOT NULL DEFAULT 'user',
    born_spawner               TEXT,
    fork_source_agent_id       BIGINT REFERENCES agents(id),
    fork_source_checkpoint_id  TEXT,
    status                     TEXT NOT NULL CHECK (status IN ('running', 'idling', 'terminated')),
    pid                        INTEGER,                  -- filled while a process owns the running/idling row, for ops ps lookup / force kill
    spawned_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at                 TIMESTAMPTZ,              -- filled alongside pid and lease by agent._starting.claim_agent_row
    session_index              BIGINT NOT NULL DEFAULT 0,  -- unified shell+watcher session sequence number, auto-incrementing; ava.shell.new()/ava.watcher.launch() atomically take the next via UPDATE ... RETURNING
    machine                    TEXT NOT NULL DEFAULT 'unknown',  -- physical machine identifier, for multi-machine deployment (private network + central Postgres); source = AVA_MACHINE_NAME, written on INSERT in the create path (spawn_agent), claim_agent_row only verifies
    status_changed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),  -- when the row last entered its current status; maintained by the agents_meta_status_changed_at trigger. Lets the restarter reap unclaimed idling rows older than a grace (unlike spawned_at, this resets on resurrect's terminated -> idling)
    last_active_at             TIMESTAMPTZ NOT NULL DEFAULT now(),  -- when the agent last did REAL work: written = now() by the agent process on every completed LLM turn (agent/graph/llm/node.py). Deliberately NOT touched by ops lifecycle churn (rollout quiesce / restarter respawn / self.update / stop-start) — for an idle agent that whole cycle runs without an LLM turn, so this survives it. The heartbeat daemon's idle clock reads THIS (not status_changed_at, which every status flip incl. ops restarts bumps) so an ops event never resets an agent's idle timer. Backfilled from status_changed_at at add time.
    heartbeat_paused_until     TIMESTAMPTZ,              -- pause window for the gateway heartbeat daemon; set to now()+duration by ava.self.pause_heartbeat(). While in the future, the daemon skips this agent's idle-nudge. NULL = never paused.
    last_heartbeat_at          TIMESTAMPTZ,              -- when the heartbeat daemon last inserted a check-in inbound for this agent. A durable cadence floor: once a heartbeat is consumed without a completed LLM turn, the daemon still waits AVA_HEARTBEAT_INTERVAL_SECONDS before inserting another, rather than selecting the same idle row every dispatch step. NULL = never reminded / pre-migration row.
    heartbeat_backoff_level    INTEGER NOT NULL DEFAULT 0 CHECK (heartbeat_backoff_level BETWEEN 0 AND 16),  -- platform-side nudge backoff (B7): consecutive no-op nudges raise the level, stretching the reminder floor to heartbeat_interval * 2^level (cap 24h); reset to 0 on real inbound or an agent pause.
    last_message_text          TEXT,                     -- text of the last AI message produced by this agent; survives compact (which replaces the entire checkpoint). Written by the agent process after each LLM turn; read by get_last_message API. NULL = no AI message yet.
    config_overlay             JSONB,                    -- per-agent config overlay (currently llm_model); authoritative source read at agent boot after spawn/respawn/resurrect. NULL = cluster defaults.
    birth_config               JSONB,                    -- the values the cluster defaults resolved to at THIS agent's birth, for every per-agent field the registry declares lifecycle="frozen" (base/config: the brain + the system-prompt-shaping set). Stamped once at the spawn boundary (base/agents/birth_config.py), replayed on every restart/respawn/resurrect/compact, and inherited verbatim by a fork. Deliberately a SEPARATE column from config_overlay so provenance survives: config_overlay = "someone chose this for this agent", birth_config = "nobody chose; this was merely the cluster default that day". Resolution order everywhere is config_overlay > birth_config > current config. NULL = resolve every frozen field live (pre-column rows the backfill skipped). A migration that rewrites a frozen field's stored VALUE must rewrite this column too — it is a second home for values that used to live only in config_overlay (precedent: 20260725T060802_pin-haiku-dated-model-id.sql rewrites config_overlay->>'llm_model'). The skill-name renames are NOT such a case: they canonicalize agent_presets.config only, since base/packages/skills/names.py folds dash and underscore so an already-stored per-agent value still resolves. See migrations/20260731T071400_agent-birth-config.sql.
    preset_name                TEXT,                     -- spawn-time preset reference (display only): which agent_presets row supplied the base the resolved config_overlay carries. NULL = no preset. Copied verbatim by a fork without its own preset; cleared semantics live in decisions/2026-09-10-preset-in-config-overlay-fork-cache.md
    termination_source         TEXT CHECK (termination_source IN ('user', 'exit', 'reaper', 'launch-confirm', 'integrity')),  -- WHO/WHAT terminated the row; meaningful only while status='terminated'. Value set = base.agents.TerminationSource (locked by tests/test_db_check_enum_sync.py); stamped in the SAME statement as the status flip by every terminated-write site (enforced by scripts/lint_termination_source.py). 'user' = force-kill / terminate-of-already-dead (ops_lifecycle._force_mark_terminated); 'exit' = agent's own graceful process-exit finalize (mark_agent_exited_op); 'reaper' = restarter corpse reaper forced it (dead pid / stale unclaimed idling row); 'launch-confirm' = a launch that never confirmed forced it — the launcher's confirm poll timing out (agent_launch) or the child's own early-boot schema/placement gate rejecting the boot before it claimed the row (agent/_starting.py); 'integrity' = the framework found the row's own state self-inconsistent and killed it (historical rows only; no code writes it now), deliberately NOT resurrectable since the row's history is corrupt and a retry loop would bury a one-time fault. CrashResurrectController resurrects ONLY 'reaper' + 'launch-confirm' (involuntary/system-detected + self-healing); 'user'/'exit'/'integrity'/NULL are never auto-resurrected. NULL = pre-column legacy row → conservatively not eligible. Cleared to NULL on the terminated→idling resurrect transition (per-death). CHECK permits NULL.
    last_force_terminate_inbound_id BIGINT,              -- monotonic explicit-kill fence: every force termination (including an already-terminated row) inserts a kind='terminate' inbound under the agents_meta row lock and stores its id here. Pending-work resurrection (chat/compact_request) requires its exact pending inbound id to be greater than this fence, so older work cannot reverse a later kill. No FK on purpose: inbound retention must not erase lifecycle intent. Never cleared; NULL = no force intent recorded.
    last_resurrect_inbound_id  BIGINT,                  -- incarnation-epoch fence: every resurrection inserts its kind='resurrect' inbound under the agents_meta row lock and stores its id here. A lifecycle command (restart/terminate) whose intent predates the fence is superseded by that resurrection - acceptance settles it as superseded (payload names the resurrect) instead of adopting it - so a delayed terminate created before a resurrect can never kill the incarnation the resurrect just admitted (#2158). No FK on purpose: inbound retention must not erase lifecycle intent. Never cleared; NULL = no resurrection recorded.
    wake_suppressed_until      TIMESTAMPTZ,              -- delivery auto-resurrect and watchdog wake suppression deadline after repeated resurrection failures. New peer/user chats remain pending and become eligible again after expiry. Cleared by a successful resurrection spawn or inbound claim. NULL = not suppressed.
    wake_suppress_reason       TEXT,                     -- operator-readable cause paired with wake_suppressed_until; currently 'resurrect_failed'. Cleared with the deadline on successful recovery.
    lease_expires_at           TIMESTAMPTZ,              -- R1 (Task #1021): the agent-process lease — liveness is a lease-expiry judgment (`lease_expires_at > now()`), status stays lifecycle intent. Written by the agent process at claim/start (now()+lease TTL, agent/db.py / agent/_starting.py), cleared on terminate/resurrect by the ops lifecycle; read by the heartbeat daemon and the reaper (base/db/__init__.py ALIVE_SQL). NULL = row has no lease (pre-R1 legacy, or terminated).
    liveness_state             TEXT NOT NULL DEFAULT 'unknown' CHECK (liveness_state IN ('online', 'offline', 'unknown')),  -- gateway-owned derived liveness projection (Task #1174): 'online' = machine reachable AND (process lease alive where one is held); 'offline' = machine unreachable (2 consecutive failed status_probe) or lease expired; 'unknown' = not yet judged (fresh rows / unregistered machine). Written ONLY by the gateway heartbeat daemon's liveness pass — status stays lifecycle intent (R1 invariant #1); the frontend renders offline distinctly. 'terminated' rows are never judged.
    last_probe_at             TIMESTAMPTZ,              -- when the gateway liveness pass last judged this row (Task #1174).
    last_turn_fatal_at        TIMESTAMPTZ,              -- first fatal turn crash since the last completed LLM turn (the corpse marker: NULL = healthy, set = crash-dead). Stamped with COALESCE (never refreshed while dead) by the hosted runner at crash catch time (agent/hosted_ownership.stamp_turn_fatal); cleared by a completed LLM turn (_persist_last_active) and the resurrect transition. The agent_host beat's reaper terminates marked idling rows past CORPSE_REAP_GRACE_S with termination_source='reaper', and a crash under an already-set mark (a retry dying again) terminates the row at its own settle point via agent/corpse_reap.reap_recrashed_corpse without waiting out the grace; renew_hosted_owner skips marked rows so their lease decays. Each reaper termination also commits that death's near-field recovery wake (one hosted_turn_recovery-marked chat, task #4039).
    permanent_reject_streak    INTEGER NOT NULL DEFAULT 0 CHECK (permanent_reject_streak >= 0),  -- consecutive PERMANENT-class provider rejections since the last completed LLM turn (the recovery circuit breaker, task #3617): +1 at each permanent fatal turn settlement (agent/runloop.py), reset to 0 by the same completed-turn UPDATE that clears last_turn_fatal_at (agent/graph/llm/node.py::_persist_last_active). >= 2 halts every automatic recovery path (event-path resurrect / watchdog re-dispatch+retry / the stalled crash-marked harvest op / the relaxed reaper-marked trigger) until a turn succeeds; a manual resurrect stays exempt. See base/agents/recovery_breaker.py.
    last_permanent_reject_reason TEXT,                   -- the CIRCUIT_REASON_* class (agent/state_channels) of the current consecutive permanent-reject streak: written in the SAME statement as the permanent_reject_streak +1 by base/agents/recovery_breaker.record_permanent_reject_turn, cleared with the streak by the completed-turn UPDATE. 'billing' + streak >= 2 is the billing batch-recovery whitelist (task #3919). NULL = no permanent rejection on the current streak (incl. pre-column rows — never guessed).
    runtime_generation UUID,
    runtime_kind TEXT CHECK (runtime_kind IN ('process', 'hosted')),
    runtime_owner UUID,
    last_admission_outcome TEXT CHECK (last_admission_outcome IN
        ('admitted', 'maintenance_hold', 'resource_fence', 'admission_guard_refused')),
    last_admission_at TIMESTAMPTZ,
    CONSTRAINT agents_meta_admission_observation_pair_check
        CHECK ((last_admission_outcome IS NULL) = (last_admission_at IS NULL)),
    last_launch_attempt_id UUID, -- rotated on explicit retry; fences delayed dispatch results
    last_launch_failure_reason TEXT CHECK (last_launch_failure_reason IN
        ('launch_unreachable', 'launch_rejected', 'launch_unknown')),
    last_launch_failure_at TIMESTAMPTZ,
    CONSTRAINT agents_meta_launch_failure_pair_check
        CHECK ((last_launch_failure_reason IS NULL) = (last_launch_failure_at IS NULL)),
    runtime_protocol_version INTEGER NOT NULL DEFAULT 0 CHECK (runtime_protocol_version >= 0),
    incarnation_resources JSONB, -- server-owned versioned resource evidence; NULL is unknown, never an empty-set proof
    -- fork fields exist in pairs or not at all (constraint explicitly named to align with the ALTER in 0002 migration)
    CONSTRAINT agents_meta_fork_pair_check
        CHECK ((fork_source_agent_id IS NULL) = (fork_source_checkpoint_id IS NULL))
);

-- Live roster scans exclude the retained terminated-agent history.
CREATE INDEX agents_meta_live_roster_idx ON agents_meta (id) WHERE status <> 'terminated';

COMMENT ON COLUMN agents_meta.born_spawner IS
    'Birth-time original spawner. Immutable and never rewritten by folding; '
    'forks use agent:<fork_source>, plain spawns use the birth trigger, and '
    'backfilled rows are best-known.';

COMMENT ON COLUMN agents_meta.last_force_terminate_inbound_id IS
    'Monotonic inbound id fence written by every explicit force termination. '
    'Pending work may auto-resurrect this agent only when its inbound id is greater '
    'than this fence. Deliberately no foreign key: inbound retention must not '
    'erase lifecycle intent.';

COMMENT ON COLUMN agents_meta.last_resurrect_inbound_id IS
    'Monotonic inbound id fence written by every resurrection: the id of the '
    'kind=''resurrect'' inbound it enqueued. A lifecycle command whose intent '
    'predates the fence is superseded by that resurrection; acceptance settles '
    'it instead of dispatching it. Deliberately no foreign key: inbound '
    'retention must not erase lifecycle intent.';

COMMENT ON COLUMN agents_meta.last_permanent_reject_reason IS
    'The reason class of the current consecutive permanent-reject streak '
    '(the _circuit_reason value of the latest permanent provider rejection: '
    '''billing'' for HTTP 402). Written with the streak increment and cleared '
    'with it by the completed-turn UPDATE. The billing batch-recovery entry '
    'reads ''billing'' here (task #3919). NULL = no permanent rejection on '
    'the current streak; never backfilled by guess.';

-- ─────────────── heartbeat_pause_log ───────────────
-- Append-only heartbeat-pause trail: one row per ava.self.pause_heartbeat
-- call. The latest row supplies Inspector pause duration and the table retains
-- agent-side history independently of telemetry.
CREATE TABLE heartbeat_pause_log (
    id          BIGSERIAL PRIMARY KEY,
    agent_id    BIGINT NOT NULL REFERENCES agents(id),
    duration_s  DOUBLE PRECISION NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE heartbeat_pause_log IS
    'Append-only heartbeat-pause trail: one row per ava.self.pause_heartbeat call. The latest row supplies Inspector pause duration without telemetry reads.';

-- Latest-first ordering supports pause-trail inspection.
CREATE INDEX heartbeat_pause_log_agent_created_idx
    ON heartbeat_pause_log (agent_id, created_at DESC, id DESC);

-- ava_runner surface for the pause trail (task #1932): the runner process
-- (ava.self.pause_heartbeat) INSERTs the new
-- row; the BIGSERIAL id draws from the owning sequence. UPDATE/DELETE stay
-- out — the trail is append-only and no runner path rewrites rows.
-- Gated on the role's existence: fresh bootstrap applies this baseline before
-- install birth creates ava_runner, and base/cluster/authority/groups.py's
-- ensure_groups grants the audited surface at birth.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT ON heartbeat_pause_log TO ava_runner;
        GRANT USAGE, SELECT ON SEQUENCE heartbeat_pause_log_id_seq TO ava_runner;
    END IF;
END $$;

-- ─────────────── agent_notices ───────────────
-- The agent->user queue: one primitive at three obligation rungs, discriminated
-- by require_response (the stakes axis priority P0..P3 is orthogonal):
--   require_response=FALSE                 -> FYI, the user may glance or ignore
--   require_response=TRUE,  blocking=FALSE -> needs an answer, agent keeps working
--   require_response=TRUE,  blocking=TRUE  -> needs an answer, agent is stalled
-- blocking is meaningful only when require_response is TRUE (you can only be
-- stalled waiting on a reply you need) -- enforced by a CHECK, and rejected at
-- the SDK (ava.ui.notify raises). ava.ui.notify() INSERTs one row per call.
--
-- title = one-line headline (queue list); content = optional detail body shown
-- on click. resolved_at + resolution + reply is the single close triple:
--   answered  -- user answered a require_response notice (reply = the answer)
--   dismissed -- user waved away a require_response notice without answering
--   read      -- user reviewed an FYI notice (reply optional)
--   withdrawn  -- the agent dismissed its own notice (ava.ui.dismiss_notice)
--   superseded -- replaced by a newer notice from the same agent (ava.ui.notify auto-resolve)
-- reply caches the user's free-text reply for history/display; the live delivery
-- to the agent rides the chat-inbound path, not this column. updated_at tracks
-- the agent editing its own still-open notice (ava.ui.edit_notice).
--
-- The snapshot inlines the still-open require_response notices
-- (notices_awaiting_response, bounded worklist) and counts the still-open FYI
-- notices (unread_notice_count); the FYI feed content is served off-snapshot.
CREATE TABLE agent_notices (
    id              BIGSERIAL PRIMARY KEY,
    local_id        INTEGER NOT NULL,
    agent_id        BIGINT NOT NULL REFERENCES agents(id),
    title           TEXT NOT NULL,
    content         TEXT,
    priority        TEXT NOT NULL CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
    require_response BOOLEAN NOT NULL,
    blocking        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,
    resolution      TEXT CHECK (resolution IN ('answered', 'dismissed', 'read', 'withdrawn', 'superseded', 'expired')),
    reply           TEXT,
    -- Optional task this notice belongs to: the agent reports the task it was
    -- working when it posted, so the human queue groups notices by task rather
    -- than only by owner agent. NULL is the norm (a notice need not name one).
    -- The FK to agent_tasks is added by ALTER after that table is defined below
    -- (agent_notices is declared first, so an inline REFERENCES would forward-ref).
    task_id         BIGINT,
    expire_at       TIMESTAMPTZ NOT NULL,
    CONSTRAINT agent_notices_agent_local_id_unique UNIQUE (agent_id, local_id),
    CONSTRAINT agent_notices_blocking_requires_response
        CHECK (NOT blocking OR require_response),
    CONSTRAINT agent_notices_resolution_pair
        CHECK ((resolved_at IS NULL) = (resolution IS NULL)),
    CONSTRAINT agent_notices_resolution_legal
        CHECK (resolution IS NULL
               OR (require_response AND resolution IN ('answered', 'dismissed', 'withdrawn', 'superseded', 'expired'))
               OR (NOT require_response AND resolution IN ('answered', 'read', 'withdrawn', 'superseded', 'expired'))),
    CONSTRAINT agent_notices_answered_has_reply
        CHECK (resolution IS DISTINCT FROM 'answered' OR reply IS NOT NULL)
);

-- Open notices that need a response: serves the snapshot's
-- notices_awaiting_response inline array (the bounded worklist).
CREATE INDEX agent_notices_awaiting_idx
    ON agent_notices (agent_id, created_at)
    WHERE require_response AND resolved_at IS NULL;

-- Open FYI notices: serves the snapshot's unread_notice_count and the
-- off-snapshot cross-fleet feed.
CREATE INDEX agent_notices_unread_idx
    ON agent_notices (agent_id, created_at)
    WHERE NOT require_response AND resolved_at IS NULL;

-- Resolved-history page (Inbox's greyed history): ORDER BY resolved_at DESC,
-- id DESC on the resolved half — the table accumulates forever, so the
-- history query needs an index, not a seq scan + sort.
CREATE INDEX agent_notices_resolved_idx
    ON agent_notices (resolved_at DESC, id DESC)
    WHERE resolved_at IS NOT NULL;

-- Active open notices index on expire_at for TTL reaper cleanup sweeps.
CREATE INDEX agent_notices_expire_at_idx
    ON agent_notices (expire_at)
    WHERE resolved_at IS NULL;

-- ─────────────── inbound_messages ───────────────
-- The agent's unified gateway — any "trigger" entering an agent goes through this table.
-- See decisions/2026-05-02-self-cycling-langgraph.md.
--
-- code_output does **not** enter this table — subprocess output is directly appended by the
-- graph's exec node as HumanMessage into state.messages, persisted by the LangGraph checkpointer.
CREATE TABLE inbound_messages (
    id         BIGSERIAL PRIMARY KEY,
    agent_id   BIGINT NOT NULL REFERENCES agents(id),
    content    TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'chat'
               CONSTRAINT inbound_messages_kind_check CHECK (kind IN (
                   'chat',              -- conversation message from user / peer agent
                   'system_note',       -- framework system notification (task assign/update/reminder); claim renders as a system note
                   'compact_summary',   -- written by agent ava.compact(summary); on claim, directly replaces messages
                   'compact_request',   -- triggered by UI "/compact" / admin; on claim, runs the backend LLM to generate a summary, then replaces
                   'cancel',            -- /api/cancel pause; in-flight llm/exec interrupts on it, claim halts to idle (agent stays alive, resumable)
                   'terminate',         -- ava.terminate() / admin terminate; claim appends lifecycle marker + goto END
                   'restart',           -- ava.restart() / admin restart; applied as a hosted lifecycle command (no message appended)
                   'restart_completed', -- historical rows only (written by the retired respawn path); claim still renders the lifecycle marker
                   'resurrect',         -- INSERTed by resurrect_agent; after the new process is up, claim appends lifecycle marker
                   'fork',              -- INSERTed by spawn_agent on a fork; the new process's first claim appends an identity marker (you are now agent N, forked from agent:M)
                   'heartbeat',         -- INSERTed by heartbeat daemon; idle-agent nudge delivered as a system note
                   'reminder'           -- INSERTed by the impersonation-maintenance pass; lease-expiry renewal reminder for the external controller
               )),
    -- 'web' / 'terminal' / 'telegram' / 'wechat' / 'eval' / 'cli' / 'system' / 'kernel'
    -- / 'unknown'. 'system' / 'kernel' means the kernel injected it itself, no envelope wrap.
    source     TEXT NOT NULL DEFAULT 'system',
    -- claim Node: UPDATE pending → claimed when grabbing a batch. After the
    -- agent process commits the corresponding HumanMessage into LangGraph
    -- state.messages, startup reconciliation flips claimed → done; if the
    -- inbound id is missing from state.messages on the next startup (commit
    -- was lost — agent 57 class of incident), reconciliation flips
    -- claimed → pending so the same inbound is re-claimed and re-delivered.
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'claimed', 'done')),
    -- Optional JSONB. restart kind carries {"config_overlay": {...}};
    -- restart_completed carries {"effective_config": {...}}. Other kinds leave NULL.
    -- See future/plugin-system-redesign.md PR-E section.
    payload    JSONB,
    -- Server-owned ingress facts. Gateway writers fill these from the
    -- authenticated request/transport boundary; agent-side and legacy writers
    -- leave them NULL. The assertion comparison is informational and never
    -- rejects delivery.
    source_verified_by VARCHAR(120),
    source_transport VARCHAR(80),
    content_hash VARCHAR(64),
    source_assertion_match BOOLEAN,
    -- Caller-generated identity for one logical chat delivery. NULL keeps
    -- internal / legacy writers unchanged; non-NULL keys are cluster-wide
    -- unique so a timeout retry can reconcile at the same transaction that
    -- owns the inbound INSERT (not in a later response cache).
    client_message_id TEXT
               CONSTRAINT inbound_messages_client_message_id_check CHECK (
                   client_message_id IS NULL
                   OR char_length(client_message_id) BETWEEN 1 AND 128
               ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- When the claim node grabbed this row (pending -> claimed for chat,
    -- pending -> done for lifecycle kinds); NULL = never claimed. Lets the
    -- gateway compute creation -> pickup latency (claimed_at - created_at),
    -- e.g. for the delivery watchdog / degraded-wake alerts.
    claimed_at TIMESTAMPTZ,
    dispatch_count INT NOT NULL DEFAULT 0,
    last_dispatch_at TIMESTAMPTZ,
    poisoned_at TIMESTAMPTZ,
    target_generation UUID,
    target_owner UUID,
    applied_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ,
    CONSTRAINT inbound_agent_command_unique UNIQUE(agent_id,id),
    CONSTRAINT inbound_lifecycle_target_check CHECK (
        (target_generation IS NULL AND target_owner IS NULL AND applied_at IS NULL AND observed_at IS NULL)
        OR (target_generation IS NOT NULL AND target_owner IS NOT NULL
            AND kind IN ('restart','terminate') AND claimed_at IS NOT NULL
            AND status IN ('claimed','done') AND (observed_at IS NULL OR applied_at IS NOT NULL))
    )
);

-- Same-agent reference also prevents retention from deleting unfinished intent.
ALTER TABLE agents_meta ADD COLUMN lifecycle_command_id BIGINT;
ALTER TABLE agents_meta ADD CONSTRAINT agents_meta_lifecycle_command_fk
    FOREIGN KEY(id,lifecycle_command_id) REFERENCES inbound_messages(agent_id,id);

COMMENT ON COLUMN inbound_messages.claimed_at IS
    'When the claim node grabbed this row (pending -> claimed for chat, pending -> done for lifecycle kinds). NULL = never claimed. Pickup latency = claimed_at - created_at.';

COMMENT ON COLUMN inbound_messages.client_message_id IS
    'Caller-generated id for one logical chat delivery. Non-NULL values are cluster-wide unique; same-id retries must match the original agent, content, source, kind, and payload.';

CREATE TABLE delivery_watchdog_alerted (
    inbound_id BIGINT PRIMARY KEY REFERENCES inbound_messages(id) ON DELETE CASCADE,
    alerted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE delivery_watchdog_alerted IS
    'Delivery watchdog stall-alert dedup: inbound ids already WARNINGed while pending. Persists the daemon''s in-memory alerted set across restarts so a restart does not re-report every still-stalled inbound (Task #945).';

CREATE TABLE delivery_watchdog_attempts (
    kind                 TEXT NOT NULL CHECK (kind IN ('resurrect', 'harvest', 'hosted_turn')),
    agent_id             BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    last_attempt_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    consecutive_failures INT NOT NULL DEFAULT 0,
    suppress_count       INT NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, agent_id)
);

COMMENT ON TABLE delivery_watchdog_attempts IS
    'Delivery watchdog recovery-loop state, one row per (loop kind, agent): last_attempt_at is the cooldown clock; consecutive_failures and suppress_count (resurrect only) are the wake-suppression escalation counters. Survives watchdog restarts.';

CREATE TABLE maintenance_state (
    kind        TEXT PRIMARY KEY,
    last_run_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE maintenance_state IS
    'ttl-reaper cadence clocks, one row per slow maintenance phase (kind): last_run_at is when the phase was last claimed. Survives service restarts.';

-- Full (non-partial) (agent_id, created_at DESC): select_all's LATERAL
-- MAX(created_at) per agent is an index-only scan on it (the partial
-- pending index below cannot serve MAX over all kinds; audit P1-1).
CREATE INDEX inbound_messages_agent_id_created_at_idx
    ON inbound_messages (agent_id, created_at DESC);

-- Used by claim_inbound_batch / wait_for_inbound — query pending rows filtered by agent_id.
-- In the new design all kinds are claimed by claim without filtering by kind; one partial
-- index covers all hot path queries.
CREATE INDEX idx_inbound_per_agent_pending ON inbound_messages (agent_id, created_at)
    WHERE status = 'pending';

CREATE UNIQUE INDEX idx_inbound_messages_client_message_id
    ON inbound_messages (client_message_id)
    WHERE client_message_id IS NOT NULL;

-- ─────────────── completion_notice_events ───────────────
-- Restart-safe hourly completion-notice buffer. Its rows are also the
-- authoritative platform-side source for canary count-conservation checks.
CREATE TABLE completion_notice_events (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    digest_inbound_id BIGINT REFERENCES inbound_messages(id)
);

CREATE UNIQUE INDEX completion_notice_events_agent_source_unique
    ON completion_notice_events (agent_id, source);

CREATE INDEX completion_notice_events_pending_idx
    ON completion_notice_events (agent_id, created_at, id)
    WHERE digest_inbound_id IS NULL;

CREATE INDEX completion_notice_events_delivered_idx
    ON completion_notice_events (created_at)
    WHERE digest_inbound_id IS NOT NULL;

COMMENT ON TABLE completion_notice_events IS
    'Restart-safe hourly completion-notice buffer and canary conservation source. One row records each platform completion event admitted under an agent hourly policy.';

COMMENT ON COLUMN inbound_messages.source_verified_by IS
    'Server-owned credential identity that admitted the gateway inbound; NULL is unauthenticated or legacy.';

COMMENT ON COLUMN inbound_messages.source_transport IS
    'Server-owned ingress transport for a gateway inbound; NULL is legacy.';

COMMENT ON COLUMN inbound_messages.content_hash IS
    'Lowercase SHA-256 of inbound content at gateway persistence time; NULL is legacy.';

COMMENT ON COLUMN inbound_messages.source_assertion_match IS
    'Whether an agent:N source assertion matches a verified agent_token:M credential; NULL when either side is unknown. Informational only.';

-- Lifecycle pointer -> done guard (task #3678): a command must not reach `done`
-- while agents_meta.lifecycle_command_id still points at it — that torn shape blinds
-- boot recovery (needs claimed) and live observation (needs a process identity) at
-- once, deferring any resurrect forever. Deferred to COMMIT, so every legitimate
-- writer (supersede / force / observe / hosted settle) passes: each clears the
-- pointer in the same transaction. The TTL reaper's hourly torn-pointer scan
-- (gateway/ttl_reaper.py, telemetry lifecycle_pointer_done_torn) is the detector
-- for a bypass (a pointer later set onto an already-done row). Ships to existing
-- clusters via a migration.
CREATE OR REPLACE FUNCTION reject_inbound_done_with_lifecycle_pointer() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM agents_meta m
        WHERE m.lifecycle_command_id = NEW.id AND m.id = NEW.agent_id
    ) THEN
        RAISE EXCEPTION 'inbound % (agent %) cannot reach done while agents_meta.lifecycle_command_id still points at it — settle the command and clear the pointer in the same transaction', NEW.id, NEW.agent_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER inbound_messages_lifecycle_pointer_done_guard
    AFTER UPDATE ON inbound_messages
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.status = 'done' AND OLD.status IS DISTINCT FROM 'done'
          AND NEW.kind IN ('restart', 'terminate'))
    EXECUTE FUNCTION reject_inbound_done_with_lifecycle_pointer();

-- ─────────────── events Since-Birth rollup ───────────────
-- Day-grain rollups that preserve the `events` "since-birth" aggregates
-- across retention (events is partitioned by month and old partitions are
-- DROPped; these tables survive so the whole-life aggregates the readers need do
-- not vanish with the raw rows). The whole ~59-day history reduces to ~2900 rows,
-- so these are never themselves subject to retention. A gateway maintenance daemon
-- (services.upkeep.events_maintenance) upserts them daily; the upsert is a full-day
-- overwrite recompute keyed on the PK, so it is idempotent — a re-run never
-- double-counts. The read-time split is day-boundary (UTC midnight): the ledger
-- serves rolled days, while the newest retained day (which can be stale) and today
-- come from the retained Loki tail so late closed-day writes count exactly once.
--
-- agent_metrics_daily: per agent x UTC-day turn / exec counters plus the mergeable
-- turn-duration stats. turn_dur_sum feeds both the lifetime mean and lm_stage_tps.
-- turn_dur_hist is a mergeable integer-second histogram for p50 / p90; its bucket
-- precision never supplies the exact lifetime min / max values.
CREATE TABLE agent_metrics_daily (
    agent_id      BIGINT NOT NULL REFERENCES agents(id),
    day           DATE   NOT NULL,
    turn_total    BIGINT NOT NULL DEFAULT 0,
    turn_ok       BIGINT NOT NULL DEFAULT 0,
    turn_dur_sum  DOUBLE PRECISION NOT NULL DEFAULT 0,  -- Σ turn_end.duration_seconds → lm_stage_tps denominator + lifetime mean
    turn_dur_min  DOUBLE PRECISION,
    turn_dur_max  DOUBLE PRECISION,
    turn_dur_hist JSONB NOT NULL DEFAULT '{}'::jsonb,  -- floor(duration_seconds) integer-second bucket → count
    exec_ok       BIGINT NOT NULL DEFAULT 0,            -- event = 'exec'
    exec_failed   BIGINT NOT NULL DEFAULT 0,            -- event LIKE 'exec\_%' OR LIKE 'exec(%'
    PRIMARY KEY (agent_id, day)
);

COMMENT ON COLUMN agent_metrics_daily.turn_dur_hist IS
    'Integer-second floor(duration_seconds) bucket-to-count map; mergeable across days and backfilled for archive-era ledger rows.';

-- Compact observed measurements, never a complete billing ledger. The telemetry
-- queue can shed records before any sink; freshness does not prove completeness.
CREATE TABLE agent_metric_collection (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO agent_metric_collection (singleton) VALUES (TRUE);

CREATE TABLE agent_metric_observations (
    event_id NUMERIC(20, 0) PRIMARY KEY CHECK (event_id >= 0),
    agent_id BIGINT NOT NULL REFERENCES agents(id),
    occurred_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    kind TEXT NOT NULL CHECK (kind IN ('usage', 'turn', 'exec', 'activity')),
    model TEXT,
    usage_calls BIGINT NOT NULL DEFAULT 0 CHECK (usage_calls >= 0),
    unpriced_calls BIGINT NOT NULL DEFAULT 0 CHECK (unpriced_calls >= 0),
    tokens_in BIGINT NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out BIGINT NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    tokens_cached BIGINT NOT NULL DEFAULT 0 CHECK (tokens_cached >= 0),
    tokens_reasoning BIGINT NOT NULL DEFAULT 0 CHECK (tokens_reasoning >= 0),
    turn_total BIGINT NOT NULL DEFAULT 0 CHECK (turn_total >= 0),
    turn_ok BIGINT NOT NULL DEFAULT 0 CHECK (turn_ok >= 0),
    exec_ok BIGINT NOT NULL DEFAULT 0 CHECK (exec_ok >= 0),
    exec_failed BIGINT NOT NULL DEFAULT 0 CHECK (exec_failed >= 0),
    cost_usd NUMERIC NOT NULL DEFAULT 0 CHECK (cost_usd >= 0 AND cost_usd <> 'NaN'::numeric),
    turn_duration_seconds DOUBLE PRECISION CHECK (turn_duration_seconds >= 0 AND turn_duration_seconds < 'Infinity'::float8),
    active_seconds DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (active_seconds >= 0 AND active_seconds < 'Infinity'::float8),
    exec_seconds DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (exec_seconds >= 0 AND exec_seconds < 'Infinity'::float8)
);
CREATE INDEX agent_metric_observations_window_idx
    ON agent_metric_observations (agent_id, occurred_at);

CREATE TABLE agent_metric_days (
    agent_id BIGINT NOT NULL REFERENCES agents(id),
    day DATE NOT NULL,
    usage_calls BIGINT NOT NULL DEFAULT 0 CHECK (usage_calls >= 0),
    unpriced_calls BIGINT NOT NULL DEFAULT 0 CHECK (unpriced_calls >= 0),
    tokens_in BIGINT NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out BIGINT NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    tokens_cached BIGINT NOT NULL DEFAULT 0 CHECK (tokens_cached >= 0),
    tokens_reasoning BIGINT NOT NULL DEFAULT 0 CHECK (tokens_reasoning >= 0),
    turn_total BIGINT NOT NULL DEFAULT 0 CHECK (turn_total >= 0),
    turn_ok BIGINT NOT NULL DEFAULT 0 CHECK (turn_ok >= 0),
    exec_ok BIGINT NOT NULL DEFAULT 0 CHECK (exec_ok >= 0),
    exec_failed BIGINT NOT NULL DEFAULT 0 CHECK (exec_failed >= 0),
    cost_usd NUMERIC NOT NULL DEFAULT 0 CHECK (cost_usd >= 0 AND cost_usd <> 'NaN'::numeric),
    turn_duration_sum DOUBLE PRECISION NOT NULL DEFAULT 0,
    turn_duration_min DOUBLE PRECISION,
    turn_duration_max DOUBLE PRECISION,
    active_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    exec_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (agent_id, day)
);

-- Runner-local source cursors advance in the same transaction as repaired facts.
CREATE TABLE agent_metric_file_cursors (
    source_key TEXT PRIMARY KEY,
    identity TEXT NOT NULL,
    position BIGINT NOT NULL CHECK (position >= 0)
);

-- Actual metadata transitions define nonterminated time from this epoch onward.
-- No reconstruction from lossy lifecycle telemetry, nor invented pre-cutover time.
CREATE TABLE agent_lifecycle_intervals (
    agent_id BIGINT NOT NULL REFERENCES agents(id),
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    PRIMARY KEY (agent_id, started_at),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX agent_lifecycle_intervals_open_idx
    ON agent_lifecycle_intervals (agent_id) WHERE ended_at IS NULL;
INSERT INTO agent_lifecycle_intervals (agent_id, started_at)
SELECT id, agent_metric_collection.started_at
FROM agents_meta CROSS JOIN agent_metric_collection
WHERE status <> 'terminated';

CREATE FUNCTION record_agent_lifecycle_interval() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE transitioned_at TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'terminated' THEN
            INSERT INTO agent_lifecycle_intervals (agent_id, started_at)
            VALUES (NEW.id, transitioned_at);
        END IF;
    ELSIF OLD.status = 'terminated' AND NEW.status <> 'terminated' THEN
        INSERT INTO agent_lifecycle_intervals (agent_id, started_at)
        VALUES (NEW.id, transitioned_at);
    ELSIF OLD.status <> 'terminated' AND NEW.status = 'terminated' THEN
        UPDATE agent_lifecycle_intervals SET ended_at = transitioned_at
        WHERE agent_id = NEW.id AND ended_at IS NULL;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER agents_meta_lifecycle_interval
    AFTER INSERT OR UPDATE OF status ON agents_meta
    FOR EACH ROW EXECUTE FUNCTION record_agent_lifecycle_interval();

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT ON agent_metric_observations TO ava_runner;
        GRANT SELECT, INSERT, UPDATE ON agent_metric_days, agent_lifecycle_intervals TO ava_runner;
        GRANT SELECT ON agent_metric_collection TO ava_runner;
        GRANT SELECT, INSERT, UPDATE ON agent_metric_file_cursors TO ava_runner;
    END IF;
END $$;

ALTER TABLE agents_meta ADD COLUMN creation_key TEXT;
ALTER TABLE agents_meta ADD COLUMN creation_request_hash TEXT;
ALTER TABLE agents_meta ADD CONSTRAINT agents_meta_creation_identity_check CHECK (
    (creation_key IS NULL AND creation_request_hash IS NULL) OR
    (creation_key IS NOT NULL AND char_length(creation_key) BETWEEN 1 AND 128
     AND creation_request_hash IS NOT NULL AND creation_request_hash ~ '^[0-9a-f]{64}$')
);
CREATE UNIQUE INDEX agents_meta_creation_key ON agents_meta (creation_key)
    WHERE creation_key IS NOT NULL;
COMMENT ON COLUMN agents_meta.creation_key IS
    'Immutable creation intent; retained with agent identity so lost-response retries cannot create another agent.';

-- ─────────────── api_idempotency ───────────────
-- Generic AtLeastOnceWithKey dedup (R3 doorplate ①): routes whose contract
-- declares Idempotency.AT_LEAST_ONCE_WITH_KEY (POST /api/agents/{id}/messages)
-- dedup by the Idempotency-Key header — the first request with a key
-- executes and its response is stored here; same-key retries (SDK / IM
-- bridge retrying one logical request) replay it instead of re-executing.
-- The cluster_rpc mechanism generalized into one shared table (migration
-- 20260808T200000_unify-ops-idempotency merged the former
-- cluster_ops_idempotency in): the /ops dispatch channel (agent_ops daemon)
-- stores method='ops' rows with their outcome in `op_status`, the HTTP
-- middleware stores HTTP-code rows in `status`.
-- key = caller-generated per logical request; status/completed_at are NULL
-- while the owning request executes. HTTP cache rows live 7 days. Ops rows
-- retain immutable request hashes and are not expired into fresh executions;
-- domain recovery must establish safe retirement before pruning them.
CREATE TABLE api_idempotency (
    key TEXT PRIMARY KEY,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status INTEGER,
    op_status TEXT,
    request_hash TEXT,
    response_body JSONB,
    response_headers JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

COMMENT ON TABLE api_idempotency IS
    'AtLeastOnceWithKey dedup for routes declaring Idempotency.AT_LEAST_ONCE_WITH_KEY (R3 doorplate 1): the first request with an Idempotency-Key header stores its response; same-key retries replay it instead of re-executing.';
COMMENT ON COLUMN api_idempotency.status IS
    'HTTP status of the stored response; NULL while the owning request is still executing.';
COMMENT ON COLUMN api_idempotency.request_hash IS
    'SHA-256 of the immutable ops kind/payload; NULL on legacy/HTTP records. Unknown identity fails closed on ops replay.';
COMMENT ON COLUMN api_idempotency.op_status IS
    'Ops-channel outcome status (''completed''/''failed''); NULL for HTTP middleware rows. The HTTP channel stores its HTTP code in `status` instead.';

-- agent_model_tokens_daily: per agent x UTC-day x model token + cost ledger.
-- cost_usd is the SUM of the day's stored usage-time price snapshots (user
-- principle: cost is billed at the price in force at the call, never
-- re-priced against the current registry) — costed_calls counts rows that
-- carried a snapshot, unpriced_calls the rows without one (they contribute
-- 0 cost), and estimated_calls marks calls costed from an inferred model
-- instead of an event snapshot. model '' = an llm_usage row that carried no
-- model field. The per-agent daily token total = SUM over that day's model
-- rows (not re-stored in agent_metrics_daily). Whole days land here from the
-- events-maintenance rollup pass over telemetry_events; the cost read path is these rows + the
-- newest raw rows.
CREATE TABLE agent_model_tokens_daily (
    agent_id         BIGINT NOT NULL REFERENCES agents(id),
    day              DATE   NOT NULL,
    model            TEXT   NOT NULL,
    llm_calls        BIGINT NOT NULL DEFAULT 0,
    tokens_in        BIGINT NOT NULL DEFAULT 0,
    tokens_out       BIGINT NOT NULL DEFAULT 0,
    tokens_cached    BIGINT NOT NULL DEFAULT 0,
    tokens_reasoning BIGINT NOT NULL DEFAULT 0,
    cost_usd         DOUBLE PRECISION NOT NULL DEFAULT 0,
    costed_calls     BIGINT NOT NULL DEFAULT 0,
    unpriced_calls   BIGINT NOT NULL DEFAULT 0,
    estimated_calls  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_id, day, model)
);

-- The whole-life sums per agent and model, folded from agent_model_tokens_daily once its days can
-- no longer change (migration 20261003T020000_agent-model-tokens-total). A reader of "all time"
-- adds this to the days after the fold and to the newest raw rows.
-- `agent_model_tokens_total_through` holds the single watermark: the last UTC day folded in.
CREATE TABLE IF NOT EXISTS agent_model_tokens_total (
    agent_id         BIGINT NOT NULL REFERENCES agents(id),
    model            TEXT   NOT NULL,
    llm_calls        BIGINT NOT NULL DEFAULT 0,
    tokens_in        BIGINT NOT NULL DEFAULT 0,
    tokens_out       BIGINT NOT NULL DEFAULT 0,
    tokens_cached    BIGINT NOT NULL DEFAULT 0,
    tokens_reasoning BIGINT NOT NULL DEFAULT 0,
    cost_usd         DOUBLE PRECISION NOT NULL DEFAULT 0,
    costed_calls     BIGINT NOT NULL DEFAULT 0,
    unpriced_calls   BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_id, model)
);

CREATE TABLE IF NOT EXISTS agent_model_tokens_total_through (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    day       DATE NOT NULL
);

-- ─────────────── alerts (system→human alert store, Task #1224) ───────────────
-- One row = one alert instance, in the Alertmanager standard webhook payload
-- shape (status + labels + annotations + startsAt/endsAt + fingerprint +
-- generatorURL). One writer: the Grafana embedded-Alertmanager webhook contact
-- point (POST /api/alerts, source='grafana'); rows from before 2026-10-04 also
-- carry the retired in-process writers' tags. Alert is fully separate
-- from Notice: own table, own UI section, own IM channel — nothing here
-- enters the notices queue.
-- severity: critical / warning / error (all three push to IM).
-- status: unresolved / resolved only — no ack, no escalation.
-- Dedup key (fingerprint, starts_at): Alertmanager re-sends the same instance
-- while firing according to notification policy, and once more on resolution. fingerprint
-- is the Alertmanager-standard fnv-1a hash over sorted labels; the ingest
-- computes it when a direct writer omits it. notified_at stamps a landed IM send.
CREATE TABLE alerts (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'unresolved',
    severity     TEXT NOT NULL DEFAULT 'warning',
    alertname    TEXT NOT NULL,
    labels       JSONB NOT NULL DEFAULT '{}'::jsonb,
    annotations  JSONB NOT NULL DEFAULT '{}'::jsonb,
    starts_at    TIMESTAMPTZ NOT NULL,
    ends_at      TIMESTAMPTZ,
    fingerprint  TEXT NOT NULL,
    generator_url TEXT NOT NULL DEFAULT '',
    -- Provenance: 'grafana' (webhook default), 'health-probe', 'machine-probe'.
    source       TEXT NOT NULL DEFAULT 'grafana',
    notified_at  TIMESTAMPTZ,
    notified_revision BIGINT NOT NULL DEFAULT 0 CHECK (notified_revision >= 0),
    notification_revision BIGINT NOT NULL DEFAULT 0 CHECK (notification_revision >= 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT alerts_fingerprint_starts UNIQUE (fingerprint, starts_at),
    CONSTRAINT alerts_status_check CHECK (status IN ('unresolved', 'resolved')),
    CONSTRAINT alerts_severity_check CHECK (severity IN ('critical', 'warning', 'error'))
);

COMMENT ON COLUMN alerts.source IS
    'Provenance: ''grafana'' (webhook default), ''health-probe'', ''machine-probe''.';

CREATE INDEX alerts_status_starts_idx ON alerts (status, starts_at DESC);

COMMENT ON COLUMN alerts.notification_revision IS
    'Current shadow notification transition revision, not provider acceptance or completion.';

CREATE TABLE alert_notification_groups (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('unresolved','resolved')),
    alertname TEXT NOT NULL,
    language TEXT NOT NULL CHECK (language IN ('zh','en')),
    render_version TEXT NOT NULL CHECK (render_version = 'alert-group-v1'),
    text TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'shadow' CHECK (origin IN ('shadow','native-v1')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE alert_notification_groups IS
    'Immutable ingest groups. Only native-v1 creation origin qualifies for new acceptance; shadow history is never promoted or automatically dispatched.';

CREATE TABLE alert_notification_members (
    alert_id BIGINT NOT NULL CHECK (alert_id > 0),
    notification_revision BIGINT NOT NULL CHECK (notification_revision > 0),
    group_id BIGINT NOT NULL REFERENCES alert_notification_groups(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    reason TEXT NOT NULL CHECK (reason IN ('fresh_firing','refire','escalation','resolution','legacy_unconfirmed')),
    fingerprint TEXT NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    source JSONB NOT NULL,
    PRIMARY KEY (alert_id, notification_revision),
    UNIQUE (group_id, ordinal)
);
COMMENT ON TABLE alert_notification_members IS
    'One immutable group membership per shadow instance transition; snapshots survive alert retention and are not delivery receipts.';

-- ─────────────── event_dismissals (Loki event-class resolution, task #1468) ───────────────
-- Loki log lines are immutable, so a resolution is state about an event class,
-- never a write-back onto an historical event. NULL agent_id means every agent;
-- the first API version rejects per-agent dismissals while retaining the field
-- for the class identity's future extension. An empty `process` means every
-- process — the scope every pre-dimension dismissal keeps (task #4329 B5).
CREATE TABLE event_dismissals (
    id           BIGSERIAL PRIMARY KEY,
    category     TEXT NOT NULL,
    level        TEXT NOT NULL,
    event_name   TEXT NOT NULL,
    source       TEXT NOT NULL,
    process      TEXT NOT NULL DEFAULT '',
    agent_id     INTEGER,
    dismissed_by INTEGER NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'dismissed'
                 CHECK (status IN ('dismissed', 'reopened')),
    dismissed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reopened_at  TIMESTAMPTZ,
    burst_count  INTEGER,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN event_dismissals.dismissed_by IS
    'Acting agent id; 0 means a user or operator through the gateway UI/API, -1 is the auto-dismiss system.';

CREATE UNIQUE INDEX event_dismissals_one_active_class_idx
    ON event_dismissals (category, level, event_name, source, process, agent_id) NULLS NOT DISTINCT
    WHERE status = 'dismissed';

-- ─────────────── agent_pages ───────────────
-- HTML UI server registry. ava.ui.show(name, port) registers an agent-owned
-- server, while ava.ui.serve() rows are supervised by the
-- page-server daemon in persistent sessions that outlive the agent process.
CREATE TABLE agent_pages (
    id         BIGSERIAL PRIMARY KEY,
    agent_id   BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    port       INTEGER NOT NULL CHECK (port > 0 AND port < 65536),
    host       TEXT,
    title      TEXT,
    serve_dir  TEXT,  -- set by ava.ui.serve(); NULL for ava.ui.show().
    server_token TEXT, -- durable per-page /health identity, minted by the page-server daemon.
    session_name TEXT, -- daemon-owned persistent shell; NULL for show() and pre-session rows.
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at  TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    expired_at TIMESTAMPTZ
);

COMMENT ON COLUMN agent_pages.serve_dir IS
    'Directory the page server serves, set by ava.ui.serve(); NULL for ava.ui.show().';

CREATE UNIQUE INDEX agent_pages_unique_open
    ON agent_pages (agent_id, name)
    WHERE closed_at IS NULL;

-- One live page per (host, port): a page server binds its row's socket
-- exclusively. Registration refuses a port another live page holds
-- (ops.pages.assert_port_free) and this index enforces the same rule against
-- concurrent registrations. Expired rows are excluded — they no longer serve
-- and must not block a fresh registration.
CREATE UNIQUE INDEX agent_pages_unique_live_port
    ON agent_pages (host, port)
    WHERE closed_at IS NULL AND expired_at IS NULL;

CREATE INDEX agent_pages_per_agent_open
    ON agent_pages (agent_id, created_at)
    WHERE closed_at IS NULL;

CREATE INDEX agent_pages_expiry_idx
    ON agent_pages (expires_at)
    WHERE closed_at IS NULL AND expired_at IS NULL;

CREATE TABLE agent_shell_ttls (
    agent_id        BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    session_id      BIGINT NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    renewals        INTEGER NOT NULL DEFAULT 0,
    last_renewed_at TIMESTAMPTZ,
    PRIMARY KEY (agent_id, session_id)
);

CREATE INDEX agent_shell_ttls_expiry_idx ON agent_shell_ttls (expires_at);

COMMENT ON TABLE agent_shell_ttls IS 'Persistent shell sessions whose agent declared a TTL at creation (ava.shell.sessions.new/run_background ttl=). Reaped by the gateway TTL reaper; rows are removed when reaped or when the session dies (the reaper self-cleans).';

CREATE TABLE agent_shell_ttl_renewals (
    id                    BIGSERIAL PRIMARY KEY,
    agent_id              BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    session_id            BIGINT NOT NULL,
    requested_ttl_seconds DOUBLE PRECISION NOT NULL,
    prev_expires_at       TIMESTAMPTZ NOT NULL,
    new_expires_at        TIMESTAMPTZ NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX agent_shell_ttl_renewals_agent_session_idx
    ON agent_shell_ttl_renewals (agent_id, session_id, created_at, id);

COMMENT ON TABLE agent_shell_ttl_renewals IS
    'Append-only shell-TTL renewal trail: one row per ava.shell.sessions.renew call, in the same transaction as the deadline UPDATE. prev/new_expires_at carry the before/after deadlines. No FK to agent_shell_ttls — the reaper deletes that row on reclamation while the audit history must survive.';

CREATE OR REPLACE FUNCTION cascade_close_agent_pages() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'terminated' AND OLD.status IS DISTINCT FROM 'terminated' THEN
        UPDATE agent_pages SET closed_at = now()
        WHERE agent_id = NEW.id AND closed_at IS NULL AND serve_dir IS NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agents_meta_terminate_cascade_pages
    AFTER UPDATE OF status ON agents_meta
    FOR EACH ROW EXECUTE FUNCTION cascade_close_agent_pages();

CREATE OR REPLACE FUNCTION cascade_open_agent_pages() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM 'terminated' AND OLD.status = 'terminated' THEN
        UPDATE agent_pages SET closed_at = NULL
        WHERE agent_id = NEW.id
          AND closed_at = OLD.status_changed_at;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agents_meta_resurrect_cascade_open_pages
    AFTER UPDATE OF status ON agents_meta
    FOR EACH ROW EXECUTE FUNCTION cascade_open_agent_pages();

-- Stamp status_changed_at on every real status transition. BEFORE UPDATE so the
-- value lands in the same row write; the WHEN guard keeps pid/index-only updates
-- and no-op status rewrites from bumping the clock.
CREATE OR REPLACE FUNCTION set_agents_meta_status_changed_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.status_changed_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agents_meta_status_changed_at
    BEFORE UPDATE OF status ON agents_meta
    FOR EACH ROW
    WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION set_agents_meta_status_changed_at();

CREATE OR REPLACE FUNCTION reject_agents_meta_born_spawner_update() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'agents_meta.born_spawner is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agents_meta_born_spawner_append_only
    BEFORE UPDATE OF born_spawner ON agents_meta
    FOR EACH ROW
    EXECUTE FUNCTION reject_agents_meta_born_spawner_update();

-- ─────────────── events archive (DROPPED — Loki archive stream) ───────────────
-- The frozen PG `events` archive was dropped with the task #1281/#1823 cleanup
-- Every pre-cutover event row
-- lives in the Loki archive stream (parity-verified import, 365d retention),
-- and the cold pg_dump archive is the long-term copy. The baseline omits the
-- table so fresh databases contain only the current read models.

-- ─────────────── agent_tasks (the task registry) ───────────────
-- Persistent, process-decoupled work items agents hand off to each other.
-- owner / created_by name an agent by agents.id; created_by is TEXT because the
-- seeded root carries 'system' (and historical rows may carry 'user').
-- Ownership moves freely (a column, not derived from the spawn graph); parent_id
-- forms an arbitrary-depth subtask tree. Owner liveness is read from
-- agents_meta.status at query time, not tracked here.
CREATE TABLE agent_tasks (
    id          BIGSERIAL PRIMARY KEY,
    parent_id   BIGINT REFERENCES agent_tasks(id),      -- parent task; every task descends from the root, so NULL marks only the root itself (task_registry.create() requires an explicit parent)
    title       TEXT NOT NULL,                          -- short one-line name, shown in listings; renameable via update()/PATCH, unique among in_progress tasks (app-level check)
    description TEXT NOT NULL,                          -- full detail of what to do; read before working
    results     TEXT,                                   -- result log (what was done, output paths); replaced by update, appended by log
    status      TEXT NOT NULL DEFAULT 'in_progress'
                CHECK (status IN ('in_progress', 'done', 'cancelled')),
    priority    TEXT NOT NULL DEFAULT 'P2'
                CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),  -- stakes axis (P0 highest); orders the board within a status column and seeds a stall-escalation notice's priority
    owner       BIGINT REFERENCES agents(id),           -- current owner agent; NULL only on the system root task (every other task always has an owner)
    created_by  TEXT NOT NULL
                CHECK (created_by ~ '^[0-9]+$' OR created_by IN ('system', 'user')),  -- original opener: an agent id, or 'system'/'user' for non-agent rows ('system' on the seeded root)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_root          BOOLEAN NOT NULL DEFAULT FALSE,  -- the immortal system root task; all tasks descend from it
    remind_interval_seconds  INTEGER DEFAULT 1800,  -- seconds until an idle task reminds its owner; default 30 min, capped at 24h. Reminders cannot be disabled: NULL only on the never-reminded root task.
    last_reminded_at   TIMESTAMPTZ,            -- last time the daemon reminded the owner
    reminder_count      INTEGER NOT NULL DEFAULT 0,  -- reminders sent for the current overdue window
    escalated_at        TIMESTAMPTZ,  -- delegator escalation marker: set by the daemon with the delivered digest; cleared with the counters by any update (the user leg marks itself via its notice)
    -- Retired task-cost columns: inactive; retained for expand-contract upgrades.
    token_budget        BIGINT CHECK (token_budget IS NULL OR token_budget > 0),
    usd_budget          DOUBLE PRECISION CHECK (usd_budget IS NULL OR (usd_budget > 0 AND usd_budget < 'Infinity'::double precision)),
    token_used          BIGINT NOT NULL DEFAULT 0 CHECK (token_used >= 0),
    usd_used            DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (usd_used >= 0 AND usd_used < 'Infinity'::double precision),
    token_budget_notified_at TIMESTAMPTZ,  -- first token-ceiling breach notification
    usd_budget_notified_at   TIMESTAMPTZ   -- first USD-ceiling breach notification
);

-- The system root task is permanently 'in_progress': it is the tree anchor and
-- can never be completed, cancelled, or reopened (update()/PATCH reject it, and
-- this CHECK makes the state itself self-verifying against direct DB writes
-- too). Root-pinning only: regular tasks move freely among the three statuses,
-- but the root must never leave its permanent state.
ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_root_status_in_progress
    CHECK (NOT is_root OR status = 'in_progress');

CREATE INDEX idx_agent_tasks_owner_status   ON agent_tasks (owner, status);
CREATE INDEX idx_agent_tasks_parent         ON agent_tasks (parent_id);
CREATE INDEX idx_agent_tasks_status_created ON agent_tasks (status, created_at);

-- No two in_progress tasks share a title — the app-level guards in
-- task_registry.create()/update() and the gateway PATCH give the friendly
-- error; this partial unique index is the database backstop (a concurrent
-- create/rename can slip past a pre-check). A title may repeat once the
-- earlier task leaves in_progress.
CREATE UNIQUE INDEX agent_tasks_title_unique_in_progress ON agent_tasks (title) WHERE status = 'in_progress';

-- The root task: system-owned, permanently 'in_progress', parent of the cluster's
-- top-level tasks only. task_registry.create() requires an explicit parent,
-- and the root (id 1) is the one id callers pass for a top-level task.
-- Idempotent so re-bootstrapping is a no-op.
INSERT INTO agent_tasks (title, description, results, status, created_by, is_root)
SELECT 'Root', 'System root task -- all tasks descend from here.', 'Root task for the task registry tree.', 'in_progress', 'system', TRUE
WHERE NOT EXISTS (SELECT 1 FROM agent_tasks WHERE is_root = TRUE);

-- Deferred FK: agent_notices.task_id -> agent_tasks(id). Declared here, not
-- inline in the agent_notices CREATE TABLE, because agent_notices is defined
-- above agent_tasks and an inline REFERENCES would forward-reference a table
-- that does not exist yet. Matches migration
-- 20260721T082401_agent-notices-task-id (which ALTERs an existing DB where both
-- tables already exist).
ALTER TABLE agent_notices
    ADD CONSTRAINT agent_notices_task_id_fkey FOREIGN KEY (task_id) REFERENCES agent_tasks(id);

-- ─────────────── user_settings ───────────────
-- Key-value store for frontend preferences (force-graph layout, view state).
-- Each key maps to an opaque JSONB value; the frontend owns its shape.
CREATE TABLE user_settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────── neighbor traversal (derived from the events stream) ───────
-- Recency-weighted "who is this agent connected to" used by ava.agents.get_neighbors.
-- Agent tie weights (spawn/fork/resurrect + send_message) and the neighbor walk moved to
-- the event stream in Python (gateway/neighbors.py) when the unified `events` table froze
-- at the LGTM cutover (task #180).
-- ─────────────── machines ───────────────
-- Machine → inbound base URL registry for multi-machine deployment. At the tail of `ava start` on
-- each machine, UPSERT its own (name, role, url) here. gateway_url is the machine's inbound base URL
-- the rest of the cluster dials: the gateway URL, or an agent-runner's ops server URL
-- `http://<reachable-host>:<ops_port>`. Cross-machine RPC (spawn / lifecycle / status / config /
-- inventory) is a direct POST to that URL's /ops endpoint (gateway/cluster_rpc.py -> services/agent_runner/agent_ops).
-- description is free-text machine metadata surfaced to agents (ava.self.MACHINE / ava.agents.list_machines); the framework does not dispatch on it.
-- stopped_at marks an intentional `ava stop` (best-effort POST /api/cluster/stopping just before local
-- teardown); register_self() clears it back to NULL on `ava start`. The cluster view is a live probe, so a
-- stopped host and a crashed host both read online=False; stopped_at lets the UI tell them apart.
CREATE TABLE machines (
    name           TEXT PRIMARY KEY,
    gateway_url    TEXT,
    -- capability SET: a host carries 'gateway', 'agent-runner', and/or
    -- 'observability-station' (a single box carries gateway+agent-runner)
    role           TEXT[] NOT NULL DEFAULT '{agent-runner}'
                       CHECK (role <@ ARRAY['gateway', 'agent-runner', 'observability-station']::text[]
                              AND cardinality(role) >= 1),
    -- The last time a process owning one of this machine's units announced the
    -- unit was up (max over its live units) — a boot/announce stamp, NOT a
    -- heartbeat: nothing refreshes it while a host merely keeps running.
    up_since_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    description    TEXT,
    stopped_at     TIMESTAMPTZ,
    -- operator-set staging latch (migration 20260812T000000): a staging host
    -- is registered + visible in the roster but excluded from the rollout
    -- target set (`list_agent_runners`). Never written by register_self or
    -- the ops daemon.
    is_staging     BOOLEAN NOT NULL DEFAULT false,
    -- operator-set pause latch (migration 20260814T182039): paused_at NOT NULL
    -- = the machine is temporarily pulled from the cluster (user scenario:
    -- disconnect for a week, then resume). Excluded from the roster/cluster
    -- panel/agents' list_machines, from `list_agent_runners()` (no probe -> no
    -- offline alert; rollout skips it) and from spawn targets; pause_reason is
    -- the operator's recorded why. register_self NEVER clears the latch — only
    -- `ava cluster resume` does — so the pause survives the machine's own
    -- re-registrations while it is away. The row (gateway_url/role) is kept:
    -- resume needs it.
    paused_at      TIMESTAMPTZ,
    pause_reason   TEXT
);

-- machine_probe — per-machine status_probe results, written by the gateway
-- heartbeat daemon's liveness pass (Task #1174). Raw probe outcome plus the
-- consecutive-failure count (the anti-jitter gate: a machine is judged offline
-- only after 2 consecutive failed probes). Deliberately NOT a machines-table
-- column: the machines row is a recomputed composition of machine_units
-- (base/cluster/machines.py _recompute_machine_row) and any column there would be
-- clobbered by register_self.
CREATE TABLE machine_probe (
    machine_name         TEXT PRIMARY KEY,
    online               BOOLEAN NOT NULL,
    agent_host_online    BOOLEAN, -- status_probe's existing host-alive verdict; NULL if the probe failed or lacked it
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_probe_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- machine_status_snapshot — the roster's read model: the last status_probe of every
-- roster-visible agent-runner (rollout targets, staging and intentionally stopped
-- hosts), written by the heartbeat liveness pass. `status` is the last ClusterStatus
-- payload (kept across one failed attempt), `status_at` when it was probed; NULL
-- status with reachable = the host answered with a body that is not a ClusterStatus.
CREATE TABLE machine_status_snapshot (
    machine_name         TEXT PRIMARY KEY,
    observed_at          TIMESTAMPTZ NOT NULL,
    reachable            BOOLEAN NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    status               JSONB,
    status_at            TIMESTAMPTZ
);

COMMENT ON TABLE machine_status_snapshot IS
    'Roster read model: the last status_probe per roster-visible agent-runner, written by the heartbeat liveness pass. status holds the last ClusterStatus payload and status_at when it was probed.';

-- machine_units: per-unit capability contributions that COMPOSE the machines row
-- above. One row per (machine_name, home) — `home` is the unit's $AVA_HOME. Two
-- co-located units (e.g. a gateway-only unit under ~/.ava_gateway + an
-- agent-runner-only unit under ~/.ava) share machine_name but differ by home, so
-- each UPSERTs only its own row instead of clobbering the shared machines row.
-- register_self recomputes the machines row as the union over a machine's
-- non-stopped units; every machines reader is unchanged.
CREATE TABLE machine_units (
    machine_name                 TEXT NOT NULL,
    home                         TEXT NOT NULL,
    serve_gateway                BOOLEAN NOT NULL DEFAULT false,
    serve_agent_runner           BOOLEAN NOT NULL DEFAULT false,
    serve_observability_station  BOOLEAN NOT NULL DEFAULT false,
    url                          TEXT,
    up_since_at        TIMESTAMPTZ,
    stopped_at         TIMESTAMPTZ,
    PRIMARY KEY (machine_name, home)
);

-- (runtime_config_overrides was the API-writable config override layer; config is
-- now single-source in each unit's `.env` and the table was dropped — migration
-- 0047. The other 0010 table, plugins_config_overrides, was dropped in 0035.)

-- ─────────────── deployment_state (R1 — Task #1021) ───────────────
-- Cluster singleton row (id=1 CHECK). It holds the client-side code-version
-- gate's minimum (base/db/code_version_gate.py); the deploy lease, the last
-- update's outcome and the publication evidence it once carried are retired
-- (decisions/2026-10-01-contract-the-retired-deploy-storage.md).
CREATE TABLE deployment_state (
    id           INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    -- The lowest code version allowed to write (client-side gate,
    -- base/db/code_version_gate.py): raised to the gateway's own version at every
    -- gateway start, read by every pooled session. 0 = nothing recorded yet.
    min_code_version BIGINT NOT NULL DEFAULT 0
);

INSERT INTO deployment_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE deployment_state IS
    'Cluster singleton row (id=1) holding the code-version gate''s minimum, min_code_version. The deploy lease, last-update outcome and publication evidence it once carried were retired (decisions/2026-10-01-contract-the-retired-deploy-storage.md).';

COMMENT ON COLUMN deployment_state.min_code_version IS
    'Lowest code version (first-parent commit count of the process''s loaded commit) allowed to write; every gateway start raises it with GREATEST, every pooled session reads it and a lower process exits (decisions/2026-09-30-client-side-code-version-gate.md). Lowered only by hand after a rollback.';

-- ─────────────── host_deploy_state (R1 — Task #1021) ───────────────
-- Host-level deploy posture, one row per machine (replaces the cluster_paused
-- file, updating.flag and session probing; R1 wave, Task #1021). `posture` is
-- idle/paused. Owned by base/deploy/state/host_deploy_state.py.
CREATE TABLE host_deploy_state (
    machine                  TEXT PRIMARY KEY,
    posture                  TEXT NOT NULL DEFAULT 'idle'
                             CHECK (posture IN ('idle', 'paused')),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE host_deploy_state IS
    'Host-level deploy posture (idle/paused), one row per machine: written by ava stop, pause, maintenance and start; read by the gateway 503 middleware, ava status and the deploy window.';

-- ─────────────── cluster_defaults ───────────────
-- Cluster-level defaults a NEW agent's birth stamp reads. Singleton row; today it
-- carries exactly one value, the default model, edited through
-- GET/PUT /api/config/default-model.
--
-- Not a revival of `runtime_config_overrides` (dropped in 0047 — see the note
-- above this section's neighbours: config is single-source in each unit's `.env`).
-- Nothing reads this into `settings` and no process consults it for its own
-- behavior; it is an input to exactly one event — resolving a `lifecycle="frozen"`
-- field at agent birth (base/agents/birth_config.py) — whose output is written onto the
-- agent's own `agents_meta.birth_config`.
--
-- llm_model NULL = no cluster choice; birth resolution falls through to the
-- ordinary config chain (`.env` AVA_MODEL, then the code default). A non-NULL
-- value wins over `.env` at that boundary.
-- See migrations/20260731T071500_cluster-defaults.sql.
CREATE TABLE cluster_defaults (
    id         INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    llm_model  TEXT,
    updated_at TIMESTAMPTZ,
    updated_by TEXT
);
-- A fresh baseline resolves the same default as an upgraded database.
INSERT INTO cluster_defaults (id, llm_model) VALUES (1, 'deepseek-flash');

-- ─────────────── schedules ───────────────
-- Gateway-hosted schedules: persistent supervised sessions (a `script` + a
-- `command` to run it). The ScheduleManager writes the script under
-- $AVA_HOME/schedules/<id>/, runs it in a named session, and restarts it on
-- crash. The script is arbitrary agent-written code and holds no scheduler state
-- — reuse is label-keyed against the agents table. Supersedes cron_jobs.
CREATE TABLE schedules (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    script      TEXT NOT NULL,
    command     TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    status      TEXT NOT NULL DEFAULT 'stopped'
                CHECK (status IN ('running', 'stopped', 'error', 'completed')),  -- completed = clean exit (rc=0), a terminal state
    last_error  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    launch_count     INT NOT NULL DEFAULT 0,  -- crash-backoff launch counter (schedule-manager)
    next_launch_at   TIMESTAMPTZ,             -- no relaunch before this
    not_live_since   TIMESTAMPTZ,             -- first sessionless observation of the current outage
    stall_alerted_at TIMESTAMPTZ,             -- the two-hour no-session alert fired for this outage
    desired_revision BIGINT NOT NULL DEFAULT 0,
    applied_revision BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX ON schedules (enabled);

CREATE TABLE resource_creation_receipts (
    operation_key TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    resource_id BIGINT,
    resource_created_at TIMESTAMPTZ,
    resource_updated_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE resource_creation_receipts IS
    'Preset/schedule creation acceptance identities. Only request fingerprint and original resource identity/timestamps are retained, without raw request/config/secret copies or expiry.';

CREATE TABLE schedule_operation_receipts (
    operation_key TEXT PRIMARY KEY,
    request JSONB NOT NULL,
    response JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE schedule_operation_receipts IS
    'Immutable schedule mutation receipts, committed with desired state and sync work; retained without expiry so old retries cannot become new restart intents.';

CREATE TABLE schedule_sync_requests (
    schedule_id  BIGINT PRIMARY KEY,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE schedule_sync_requests IS
    'Pending desired-state convergence. Matching revision/session provenance is adopted. The consumer deletes a row after convergence, only if requested_at is unchanged.';

CREATE TABLE schedule_versions (
    id          BIGSERIAL PRIMARY KEY,
    schedule_id BIGINT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    script      TEXT NOT NULL,
    command     TEXT NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON schedule_versions (schedule_id, created_at DESC);

-- Run history: append-only log of a schedule's runs, for the UI's "last run" +
-- history drawer. The schedule runner appends one row per process execution —
-- ok = NULL while in-progress, closed with the outcome on exit. Severable
-- observability: a write failure never affects the schedule itself.
CREATE TABLE schedule_runs (
    id          BIGSERIAL PRIMARY KEY,
    schedule_id BIGINT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    ran_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok          BOOLEAN,
    agent_id    BIGINT REFERENCES agents(id),  -- reserved: a future script self-report could set it; the runner never does, so NULL today (FK since 20260810T224356)
    note        TEXT
);
CREATE INDEX ON schedule_runs (schedule_id, ran_at DESC);

-- Per-cron-slot at-most-once claims for resident schedules. Both startup
-- catch-up and normal online fires claim through this table before invoking a
-- schedule's fire function. A claim intentionally survives callback failure:
-- this is at-most-once, not at-least-once delivery.
CREATE TABLE schedule_fire_log (
    id           BIGSERIAL PRIMARY KEY,
    schedule_id  BIGINT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    slot_fire_at TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (schedule_id, slot_fire_at)
);

-- ─────────────── agent_presets ───────────────
-- Named config templates for spawning agents. A preset bundles a flat per-agent
-- config overlay (llm_model, plugin per_agent fields, ...) under a stable `name`;
-- a spawn referencing that name seeds its config from it, with an explicit spawn
-- config winning per-key. `config` is an opaque JSONB template — validated only
-- when a spawned agent applies it at boot, not at write time.
CREATE TABLE agent_presets (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL,
    description TEXT,
    config      JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed: the preset catalog. These five carry no config: the skill index is
-- universal (cluster default `["*"]`), so the skill lists they used to carry
-- would now narrow an agent's index instead of widening it — see
-- migrations/20260731T084500_seed-presets-drop-skill-index-list.sql. They stay
-- seeded as named roles; what differentiates them is the next piece of work.
INSERT INTO agent_presets (name, label, description, config) VALUES
    (
        'coder',
        'Coder',
        'Coding agent — writes and ships code, driving Claude Code / Codex for the long runs.',
        '{}'::jsonb
    ),
    (
        'reviewer',
        'Reviewer',
        'Code review agent — judges other agents'' PRs rather than authoring its own.',
        '{}'::jsonb
    ),
    (
        'researcher',
        'Researcher',
        'Research agent — searches the open web and synthesizes what it finds into an answer.',
        '{}'::jsonb
    ),
    (
        'orchestrator',
        'Orchestrator',
        'Orchestration agent — decomposes a goal, spawns workers for the parts, and supervises to completion.',
        '{}'::jsonb
    ),
    (
        'explorer',
        'Explorer',
        'Exploration agent — autonomous technology scouting: discover → evaluate → recommend.',
        '{}'::jsonb
    )
ON CONFLICT (name) DO NOTHING;

CREATE TABLE mcp_clients (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'read' CHECK (scope IN ('read', 'write')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mcp_clients_token_hash ON mcp_clients (token_hash);

-- ─────────────── extension registry ───────────────
-- The cluster owns which extensions exist and their default enablement; the
-- machine owns only capabilities. Slice S2 of
-- future/infra/extensions/extension-ownership.md (issue #39); the ownership model is
-- decisions/2026-08-21-extension-ownership-three-tiers.md.

CREATE TABLE extension_blobs (
    content_hash TEXT PRIMARY KEY,      -- base.packages.extensions.install_registry.tree_hash of the landed tree
    archive      BYTEA NOT NULL,        -- tar of that tree, IGNORED_NAMES excluded
    size_bytes   INTEGER NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The cap is a CONSTRAINT, not a convention. Extension content is source
    -- trees (markdown, a little Python); large artifacts are host provisioning
    -- and do not belong in the cluster's data plane. 8 MiB is far above any
    -- real package and far below "someone put a model checkpoint in Postgres".
    -- base/packages/extensions/registry.py:MAX_BLOB_BYTES carries the same number and
    -- base/packages/extensions/tests/test_extensions_registry.py pins the two together by writing
    -- exactly the cap and exactly one byte over.
    CONSTRAINT extension_blobs_size_cap CHECK (size_bytes > 0 AND size_bytes <= 8388608),
    -- The declared size must BE the archive's size — otherwise the cap is
    -- checked against a number the writer chose rather than the bytes stored.
    CONSTRAINT extension_blobs_size_is_real CHECK (size_bytes = octet_length(archive))
);

CREATE TABLE extensions (
    name            TEXT PRIMARY KEY,   -- match_key-folded (dash/underscore are one name)
    kind            TEXT NOT NULL CHECK (kind IN ('skill', 'plugin', 'mcp')),
    source          TEXT NOT NULL,      -- 'repo' | git URL | 'local:<machine>'
    source_ref      TEXT,               -- commit/tag as installed, when source is git
    version         TEXT,               -- manifest version, when the package declares one
    content_hash    TEXT REFERENCES extension_blobs(content_hash),
    manifest        JSONB,              -- ava-plugin.json as landed
    trust           TEXT NOT NULL DEFAULT 'unreviewed'
                    CHECK (trust IN ('builtin', 'reviewed', 'unreviewed')),
    default_enabled BOOLEAN NOT NULL DEFAULT true,
    installed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Repo-shipped content does NOT ride the data plane: it is already
    -- cluster-consistent via commit-pinned rollout, and its trust story is the
    -- checkout. A 'repo' row exists only to carry `default_enabled`, so it must
    -- have no blob; everything that arrived by INSTALL must have one. Encoding
    -- it here makes "the registry owns what arrives by install, not by release"
    -- a schema fact rather than a sentence in a design doc.
    CONSTRAINT extensions_blob_iff_installed CHECK (
        (source = 'repo' AND content_hash IS NULL)
        OR (source <> 'repo' AND content_hash IS NOT NULL)
    )
);

-- The materialization query is "what should this machine have", which reads the
-- enabled rows; kind narrows it per slice (S2 materializes only skills).
CREATE INDEX idx_extensions_enabled_kind ON extensions (kind) WHERE default_enabled;

-- ─────────────── web sessions ───────────────
-- Opaque browser credentials with server-side expiry and revocation. The
-- gateway keeps only short positive cache entries; this table is authoritative.
CREATE TABLE IF NOT EXISTS web_sessions (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_agent TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS web_sessions_expires_idx ON web_sessions (expires_at);

-- Cooperative, same-machine external execution. PostgreSQL owns the lease;
-- Redis only announces changes. Existing agents and messages remain untouched.
CREATE TABLE IF NOT EXISTS agent_impersonations (
    id UUID NOT NULL UNIQUE,
    agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    session_id BIGINT NOT NULL CHECK (session_id >= 0),
    name TEXT NOT NULL DEFAULT '',
    executor_name TEXT NOT NULL DEFAULT '',
    process_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    automatic BOOLEAN NOT NULL DEFAULT FALSE,
    summary TEXT,
    handoff_document JSONB,
    handoff_path TEXT,
    handoff_applied_at TIMESTAMPTZ,
    events_completed_at TIMESTAMPTZ,
    event_delivery_protocol_version SMALLINT CHECK (event_delivery_protocol_version = 2),
    event_admission_closed_at TIMESTAMPTZ,
    event_delivery_pending_reason TEXT CHECK (event_delivery_pending_reason IN (
        'awaiting_session_end', 'awaiting_participant_seal', 'capture_failed'
    )),
    next_entry BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_id,session_id),
    source TEXT NOT NULL,
    machine TEXT NOT NULL,
    token_hash TEXT,
    reason TEXT NOT NULL DEFAULT '',
    rejection_reason TEXT,
    status TEXT NOT NULL CHECK (status IN ('requested', 'accepted', 'active', 'released', 'rejected', 'expired')),
    ttl_seconds INTEGER NOT NULL CHECK (ttl_seconds BETWEEN 1 AND 86400),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_generation UUID,
    accepted_owner UUID,
    consent_version INTEGER NOT NULL DEFAULT 1,
    activated_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    summary_inbound_id BIGINT REFERENCES inbound_messages(id) ON DELETE SET NULL,
    plugin_delta JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(plugin_delta) = 'array'),
    delta_version INTEGER NOT NULL DEFAULT 0,
    applied_version INTEGER NOT NULL DEFAULT 0,
    start_message TEXT NOT NULL DEFAULT '',
    relay_provider TEXT,
    relay_thread_id TEXT,
    relay_codex_remote TEXT,
    relay_token_hash TEXT,
    relay_heartbeat_at TIMESTAMPTZ,
    relay_last_failure_at TIMESTAMPTZ,
    relay_minted_at TIMESTAMPTZ,
    relay_minted_generation UUID,
    relay_minted_owner UUID,
    relay_generation BIGINT NOT NULL DEFAULT 0 CHECK (relay_generation >= 0),
    relay_identity JSONB,
    relay_degraded_reason TEXT,
    relay_degraded_at TIMESTAMPTZ,
    terminal_notice_snapshot JSONB,
    terminal_notice_pending_at TIMESTAMPTZ,
    terminal_notice_accepted_at TIMESTAMPTZ,
    terminal_notice_attempt_id UUID,
    terminal_notice_attempt_at TIMESTAMPTZ,
    terminal_notice_attempts INTEGER NOT NULL DEFAULT 0 CHECK (terminal_notice_attempts >= 0),
    terminal_notice_error TEXT,
    terminal_notice_unsupported_at TIMESTAMPTZ,
    relay_batch_window_seconds INTEGER NOT NULL DEFAULT 0
        CHECK (relay_batch_window_seconds BETWEEN 0 AND 300),
    ack_window_seconds INTEGER NOT NULL DEFAULT 180 CHECK (ack_window_seconds > 0),
    max_delivery_attempts INTEGER NOT NULL DEFAULT 2 CHECK (max_delivery_attempts > 0),
    CHECK (applied_version >= 0 AND applied_version <= delta_version),
    CHECK (jsonb_array_length(plugin_delta) = delta_version),
    CHECK ((accepted_generation IS NULL) = (accepted_owner IS NULL)),
    CONSTRAINT agent_impersonations_relay_minted_pair CHECK (
        (relay_minted_generation IS NULL) = (relay_minted_owner IS NULL)),
    CONSTRAINT agent_impersonations_relay_spec CHECK (
        (relay_provider IS NULL
            AND relay_thread_id IS NULL
            AND relay_codex_remote IS NULL)
        OR (relay_provider = 'codex' AND relay_thread_id IS NOT NULL)
        OR (relay_provider IN ('claude', 'dsh')
            AND relay_thread_id IS NULL
            AND relay_codex_remote IS NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_impersonations_one_open
    ON agent_impersonations(agent_id)
    WHERE status IN ('requested', 'accepted', 'active') OR delta_version > applied_version
        OR (automatic AND handoff_applied_at IS NULL);
CREATE INDEX IF NOT EXISTS agent_impersonations_expiry ON agent_impersonations(expires_at)
    WHERE status IN ('requested', 'accepted', 'active');
CREATE INDEX IF NOT EXISTS agent_impersonations_relay_heartbeat
    ON agent_impersonations(agent_id, relay_heartbeat_at)
    WHERE status = 'active';

-- Reading delivers without consuming. Only the explicit processing ACK changes
-- an inbound to done; an expired borrower leaves every unacknowledged row pending.
CREATE TABLE IF NOT EXISTS agent_impersonation_messages (
    lease_id UUID NOT NULL REFERENCES agent_impersonations(id) ON DELETE RESTRICT,
    inbound_id BIGINT NOT NULL REFERENCES inbound_messages(id) ON DELETE CASCADE,
    acknowledged_at TIMESTAMPTZ,
    delivery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
    last_delivery_at TIMESTAMPTZ,
    CONSTRAINT agent_impersonation_messages_delivery_consistent CHECK ((delivery_attempts = 0) = (last_delivery_at IS NULL)),
    PRIMARY KEY (lease_id, inbound_id)
);
CREATE INDEX agent_impersonation_messages_unacknowledged_delivery
    ON agent_impersonation_messages(lease_id, last_delivery_at)
    WHERE acknowledged_at IS NULL;

-- Every termination writer (including force/reaper) revokes in its own atomic
-- status transaction. Restart keeps the status and preserves the active lease.
-- Event admission closes with the lease, like every other end.
CREATE OR REPLACE FUNCTION revoke_terminated_impersonation() RETURNS trigger AS $$
DECLARE
    ended_lease RECORD;
    interruption_id BIGINT;
BEGIN
    FOR ended_lease IN
        UPDATE agent_impersonations SET status='expired', ended_at=clock_timestamp(),
            rejection_reason='terminated: agent was terminated'
        WHERE agent_id=NEW.id AND status IN ('requested','accepted','active')
        RETURNING id, session_id
    LOOP
        PERFORM close_impersonation_event_admission(ended_lease.id);
        -- The native graph may be drained. Persist completed facts for its next
        -- resurrection, distinct from the graph's earlier acceptance marker.
        INSERT INTO inbound_messages(agent_id,content,kind,source,payload,created_at)
        VALUES(NEW.id,format('Impersonation session %s was interrupted because this agent was terminated. Unacknowledged messages remain pending; no external completion summary was supplied.', ended_lease.session_id),
            'system_note','system:impersonation',
            jsonb_build_object('impersonation_id',ended_lease.id,'note_tag','impersonation',
                'impersonation_termination_notice',TRUE),
            clock_timestamp())
        RETURNING id INTO interruption_id;
        UPDATE agent_impersonations SET summary_inbound_id=interruption_id
        WHERE id=ended_lease.id;
        INSERT INTO inbound_messages(agent_id,content,kind,source,payload,created_at)
        VALUES(NEW.id,'You were terminated.','system_note','system:impersonation',
            jsonb_build_object('impersonation_id',ended_lease.id,'note_tag','lifecycle_terminate',
                'impersonation_termination_notice',TRUE),
            clock_timestamp());
        UPDATE inbound_messages SET status='done'
        WHERE agent_id=NEW.id AND kind='reminder' AND status='pending'
            AND payload->>'lease_id'=ended_lease.id::text;
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS agents_meta_revoke_impersonation ON agents_meta;
CREATE TRIGGER agents_meta_revoke_impersonation
    AFTER UPDATE OF status ON agents_meta FOR EACH ROW
    WHEN (NEW.status = 'terminated')
    EXECUTE FUNCTION revoke_terminated_impersonation();

-- Lease closure restores the recorded native incarnation in the same transaction.
-- Lifecycle-cleared ownership and placement changes must remain authoritative.
CREATE OR REPLACE FUNCTION restore_native_impersonation_owner() RETURNS trigger AS $$
BEGIN
    IF OLD.accepted_generation IS NOT NULL AND OLD.accepted_owner IS NOT NULL THEN
        UPDATE agents_meta
        SET runtime_generation = OLD.accepted_generation,
            runtime_owner = OLD.accepted_owner
        WHERE id = OLD.agent_id AND machine = NEW.machine
          AND status IN ('running', 'idling')
          AND runtime_generation IS NOT NULL AND runtime_owner IS NOT NULL
          AND (runtime_generation, runtime_owner)
              IS DISTINCT FROM (OLD.accepted_generation, OLD.accepted_owner);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agent_impersonations_restore_native_owner
    AFTER UPDATE OF status ON agent_impersonations FOR EACH ROW
    WHEN (OLD.status IN ('requested', 'accepted', 'active')
          AND NEW.status IN ('released', 'expired'))
    EXECUTE FUNCTION restore_native_impersonation_owner();

CREATE TABLE agent_impersonation_entries (
    lease_id UUID NOT NULL REFERENCES agent_impersonations(id) ON DELETE RESTRICT,
    seq BIGINT NOT NULL CHECK (seq >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('message','lifecycle','sdk_call','api_event')),
    event_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    payload JSONB NOT NULL,
    source_key TEXT,
    PRIMARY KEY(lease_id,seq),
    UNIQUE(lease_id,event_key)
);
CREATE INDEX agent_impersonation_entries_source
    ON agent_impersonation_entries(lease_id, source_key) WHERE source_key IS NOT NULL;

-- ava_runner surface for the session trail (task #3549): the lifecycle and
-- inbound triggers, plus the handoff writer, INSERT rows; readers SELECT them
-- back. UPDATE/DELETE stay out — the preserve trigger rejects rewrites and no
-- runner path updates rows. Gated on the role's existence: fresh bootstrap
-- applies this baseline before install birth creates ava_runner, and
-- base/cluster/authority/groups.py's ensure_groups grants the surface at birth.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT ON agent_impersonation_entries TO ava_runner;
    END IF;
END $$;


CREATE FUNCTION allocate_impersonation_session() RETURNS trigger AS $$
BEGIN
    PERFORM id FROM agents_meta WHERE id=NEW.agent_id FOR UPDATE;
    UPDATE agents SET impersonation_index=impersonation_index+1 WHERE id=NEW.agent_id
        RETURNING impersonation_index-1 INTO NEW.session_id;
    IF NEW.name='' THEN NEW.name='Session ' || NEW.session_id; END IF;
    IF NEW.executor_name='' THEN NEW.executor_name=NEW.source; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public;
CREATE TRIGGER agent_impersonations_allocate BEFORE INSERT ON agent_impersonations
    FOR EACH ROW EXECUTE FUNCTION allocate_impersonation_session();

CREATE FUNCTION preserve_impersonation_history() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Impersonation history is permanent; updates and deletes are forbidden';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER agent_impersonations_preserve_history BEFORE DELETE ON agent_impersonations
    FOR EACH ROW EXECUTE FUNCTION preserve_impersonation_history();
CREATE TRIGGER agent_impersonation_entries_preserve_history
    BEFORE UPDATE OR DELETE ON agent_impersonation_entries
    FOR EACH ROW EXECUTE FUNCTION preserve_impersonation_history();

-- Event-source receipts: a local source may make exactly one open ->
-- sealed/failed transition; a sealed count is checked against the log's rows.
CREATE TABLE agent_impersonation_event_participants (
    lease_id UUID NOT NULL REFERENCES agent_impersonations(id) ON DELETE RESTRICT,
    source_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'sealed', 'failed')),
    opened_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    sealed_at TIMESTAMPTZ,
    failure_reason TEXT,
    item_count BIGINT,
    PRIMARY KEY (lease_id, source_key),
    CHECK (
        (state = 'open' AND sealed_at IS NULL AND failure_reason IS NULL)
        OR (state = 'sealed' AND sealed_at IS NOT NULL AND failure_reason IS NULL)
        OR (state = 'failed' AND failure_reason IS NOT NULL)
    )
);

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

CREATE FUNCTION public.close_impersonation_event_admission(p_lease_id UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
BEGIN
    UPDATE public.agent_impersonations SET event_admission_closed_at=clock_timestamp()
    WHERE id=p_lease_id AND automatic AND event_delivery_protocol_version = 2
      AND event_admission_closed_at IS NULL;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    -- A lease that already ended (termination closes admission after ending it)
    -- completes here when every source is sealed; otherwise this is a no-op.
    PERFORM public.finalize_impersonation_event_log(p_lease_id);
    RETURN TRUE;
END;
$function$;

CREATE FUNCTION public.seal_impersonation_event_participant(
    p_lease_id UUID,p_source_key TEXT,p_state TEXT,p_failure_reason TEXT,p_item_count BIGINT
) RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE actual_count BIGINT;
BEGIN
    IF p_state NOT IN ('sealed','failed') THEN RAISE EXCEPTION 'Receipt transition must seal or fail'; END IF;
    PERFORM 1 FROM public.agent_impersonation_event_participants
    WHERE lease_id=p_lease_id AND source_key=p_source_key AND state='open' FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Receipt is not open'; END IF;
    IF p_state='failed' THEN
        IF p_failure_reason IS NULL THEN RAISE EXCEPTION 'Failed receipt requires a reason'; END IF;
        UPDATE public.agent_impersonation_event_participants SET state='failed',failure_reason=p_failure_reason
        WHERE lease_id=p_lease_id AND source_key=p_source_key;
        RETURN;
    END IF;
    SELECT count(*) INTO actual_count FROM public.agent_impersonation_entries
    WHERE lease_id=p_lease_id AND source_key=p_source_key;
    IF actual_count<>p_item_count THEN
        RAISE EXCEPTION 'Receipt count does not match its recorded rows';
    END IF;
    UPDATE public.agent_impersonation_event_participants
    SET state='sealed',sealed_at=clock_timestamp(),item_count=p_item_count
    WHERE lease_id=p_lease_id AND source_key=p_source_key;
    PERFORM public.finalize_impersonation_event_log(p_lease_id);
END;
$function$;

CREATE FUNCTION public.lock_impersonation_event_participant(p_lease_id UUID, p_source_key TEXT)
RETURNS TEXT LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
DECLARE receipt_state TEXT;
BEGIN
    SELECT state INTO receipt_state FROM public.agent_impersonation_event_participants
    WHERE lease_id=p_lease_id AND source_key=p_source_key FOR UPDATE;
    RETURN receipt_state;
END;
$function$;

REVOKE ALL ON FUNCTION public.close_impersonation_event_admission(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.seal_impersonation_event_participant(UUID,TEXT,TEXT,TEXT,BIGINT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.lock_impersonation_event_participant(UUID,TEXT) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ava_runner') THEN
        REVOKE INSERT, UPDATE ON agent_impersonations FROM ava_runner;
        REVOKE UPDATE ON agent_impersonation_event_participants FROM ava_runner;
        GRANT INSERT (
            id,agent_id,source,machine,reason,status,ttl_seconds,expires_at,
            relay_provider,relay_thread_id,relay_codex_remote,relay_token_hash,
            relay_batch_window_seconds,name,executor_name,process_metadata,automatic,
            ack_window_seconds,max_delivery_attempts,event_delivery_protocol_version
        ) ON agent_impersonations TO ava_runner;
        GRANT UPDATE (
            status,ttl_seconds,expires_at,rejection_reason,summary_inbound_id,summary,
            accepted_generation,accepted_owner,consent_version,activated_at,ended_at,
            plugin_delta,delta_version,applied_version,relay_token_hash,relay_heartbeat_at,
            relay_last_failure_at,relay_minted_at,relay_minted_generation,relay_minted_owner,
            handoff_document,handoff_path,handoff_applied_at,
            next_entry,event_delivery_pending_reason,start_message
        ) ON agent_impersonations TO ava_runner;
        GRANT SELECT,INSERT ON agent_impersonation_event_participants TO ava_runner;
        GRANT EXECUTE ON FUNCTION public.close_impersonation_event_admission(UUID) TO ava_runner;
        GRANT EXECUTE ON FUNCTION public.seal_impersonation_event_participant(UUID,TEXT,TEXT,TEXT,BIGINT) TO ava_runner;
        GRANT EXECUTE ON FUNCTION public.lock_impersonation_event_participant(UUID,TEXT) TO ava_runner;
    END IF;
END $$;

-- A log-native lease is complete once it has ended, admission is closed and every
-- source has sealed with a count equal to its rows. The predicate reads only this
-- database, so no external certifier is involved. Returns whether the lease is
-- complete after the call; not-yet-ready is not an error.
CREATE FUNCTION public.finalize_impersonation_event_log(p_lease_id UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
DECLARE
    lease public.agent_impersonations%ROWTYPE;
    entry_no BIGINT;
    participants BIGINT;
    sdk_count BIGINT;
    api_count BIGINT;
BEGIN
    SELECT * INTO lease FROM public.agent_impersonations WHERE id=p_lease_id FOR UPDATE;
    IF NOT FOUND OR NOT lease.automatic OR lease.event_delivery_protocol_version IS DISTINCT FROM 2 THEN
        RETURN FALSE;
    END IF;
    IF lease.events_completed_at IS NOT NULL THEN
        RETURN TRUE;
    END IF;
    IF lease.ended_at IS NULL OR lease.event_admission_closed_at IS NULL THEN
        RETURN FALSE;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.agent_impersonation_event_participants
        WHERE lease_id=p_lease_id AND state <> 'sealed'
    ) THEN
        RETURN FALSE;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.agent_impersonation_event_participants p
        WHERE p.lease_id=p_lease_id AND p.item_count IS DISTINCT FROM (
            SELECT count(*) FROM public.agent_impersonation_entries e
            WHERE e.lease_id=p.lease_id AND e.source_key=p.source_key
        )
    ) THEN
        RAISE EXCEPTION 'Sealed source count differs from its recorded rows';
    END IF;
    UPDATE public.agent_impersonations
    SET events_completed_at=clock_timestamp(), handoff_document=NULL,
        event_delivery_pending_reason=NULL
    WHERE id=p_lease_id;
    UPDATE public.agent_impersonations SET next_entry=next_entry+1
    WHERE id=p_lease_id RETURNING next_entry-1 INTO entry_no;
    SELECT count(*) INTO participants FROM public.agent_impersonation_event_participants
    WHERE lease_id=p_lease_id;
    SELECT count(*) FILTER (WHERE kind='sdk_call'), count(*) FILTER (WHERE kind='api_event')
      INTO sdk_count, api_count
    FROM public.agent_impersonation_entries
    WHERE lease_id=p_lease_id AND source_key IS NOT NULL;
    INSERT INTO public.agent_impersonation_entries(lease_id,seq,kind,payload)
    VALUES(p_lease_id,entry_no,'lifecycle',jsonb_build_object(
        'event','event_delivery_complete',
        'event_count',sdk_count+api_count,
        'participant_count',participants,
        'sdk_call_count',sdk_count,
        'api_event_count',api_count
    ));
    RETURN TRUE;
END;
$function$;

-- Every end path (release, expiry, abort, termination) sets ended_at; the lease
-- becomes complete in that same transaction when all sources are already sealed.
CREATE FUNCTION finalize_impersonation_event_log_on_end() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    PERFORM public.finalize_impersonation_event_log(NEW.id);
    RETURN NULL;
END;
$$;
CREATE TRIGGER agent_impersonations_finalize_event_log
    AFTER UPDATE OF ended_at ON agent_impersonations
    FOR EACH ROW
    WHEN (OLD.ended_at IS NULL AND NEW.ended_at IS NOT NULL
          AND NEW.event_delivery_protocol_version = 2)
    EXECUTE FUNCTION finalize_impersonation_event_log_on_end();

-- A source row is appended only while its source can still add events: a local
-- participant while its receipt is open, the central source while admission is open.
CREATE FUNCTION guard_impersonation_event_source() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF NEW.source_key IS NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.kind NOT IN ('sdk_call','api_event') THEN
        RAISE EXCEPTION 'Only SDK and API events carry an event source';
    END IF;
    IF NEW.source_key = 'central' THEN
        IF NOT EXISTS (
            SELECT 1 FROM public.agent_impersonations
            WHERE id=NEW.lease_id AND event_delivery_protocol_version=2
              AND event_admission_closed_at IS NULL
        ) THEN
            RAISE EXCEPTION 'Central event admission is closed';
        END IF;
    ELSIF NOT EXISTS (
        SELECT 1 FROM public.agent_impersonation_event_participants
        WHERE lease_id=NEW.lease_id AND source_key=NEW.source_key AND state='open'
    ) THEN
        RAISE EXCEPTION 'Event source receipt is not open';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER agent_impersonation_entries_guard_source
    BEFORE INSERT ON agent_impersonation_entries
    FOR EACH ROW EXECUTE FUNCTION guard_impersonation_event_source();

CREATE FUNCTION record_impersonation_lifecycle() RETURNS trigger AS $$
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
        'source',COALESCE(NULLIF(current_setting('ava.impersonation_actor',true),''),'system:impersonation'),
        'previous_status',CASE WHEN TG_OP='UPDATE' THEN OLD.status ELSE NULL END,
        'previous_expires_at',CASE WHEN TG_OP='UPDATE' THEN OLD.expires_at ELSE NULL END));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER agent_impersonations_lifecycle AFTER INSERT OR UPDATE OF status,expires_at
    ON agent_impersonations FOR EACH ROW EXECUTE FUNCTION record_impersonation_lifecycle();

-- Preserve every inbound body independently of pending/ACK state and inbox retention.
CREATE FUNCTION record_impersonation_inbound() RETURNS trigger AS $$
DECLARE lease UUID; entry_no BIGINT;
BEGIN
    IF NEW.kind NOT IN ('chat','system_note','cancel','reminder','heartbeat') THEN RETURN NEW; END IF;
    -- Match native admission, activation and inbox claim lock order.
    PERFORM id FROM agents_meta WHERE id=NEW.agent_id FOR UPDATE;
    SELECT id INTO lease FROM agent_impersonations
    WHERE agent_id=NEW.agent_id AND status='active'
        AND expires_at>clock_timestamp() FOR UPDATE;
    IF lease IS NULL THEN RETURN NEW; END IF;
    UPDATE agent_impersonations SET next_entry=next_entry+1 WHERE id=lease RETURNING next_entry-1 INTO entry_no;
    INSERT INTO agent_impersonation_entries(lease_id,seq,kind,event_key,created_at,payload)
    VALUES(lease,entry_no,'message','inbound:' || NEW.id,NEW.created_at,jsonb_build_object(
        'direction','in','inbound_id',NEW.id,'kind',NEW.kind,'source',NEW.source,
        'content',NEW.content,'payload',NEW.payload));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER inbound_messages_impersonation_history AFTER INSERT ON inbound_messages
    FOR EACH ROW EXECUTE FUNCTION record_impersonation_inbound();

-- ─────────────── plugin_stats ───────────────
-- Runtime values behind declared statistics-panel cards
-- (`contributions.ui.stats`): one upsert-only row per (plugin, id), written by
-- the plugin's own refresh code through base/packages/plugins/stats.py and read into
-- GET /api/stats/dashboard. A declared card with no row is the console's
-- empty state; a failed refresh writes status=error with the reason in detail,
-- and a value that stops being refreshed keeps its row so staleness is visible.
CREATE TABLE plugin_stats (
    plugin      TEXT        NOT NULL,
    id          TEXT        NOT NULL,
    value       TEXT        NOT NULL,
    detail      TEXT,
    status      TEXT        NOT NULL DEFAULT 'ok'
                            CHECK (status IN ('ok', 'warn', 'error')),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT,
    PRIMARY KEY (plugin, id)
);

COMMENT ON TABLE plugin_stats IS
    'Runtime values behind declared statistics-panel cards (contributions.ui.stats): one upsert-only row per (plugin, id). A declared card with no row is the console''s empty state; a failed refresh writes status=error with the reason in detail.';

-- ava_runner surface: a plugin's refresh code runs in the runner process
-- (agent-process hook or daemon) and upserts its own cards — INSERT, UPDATE,
-- SELECT; no DELETE (a card that stops being reported keeps its last value and
-- updated_at, which is what makes staleness visible). Gated on the role's
-- existence: fresh bootstrap applies this baseline before install birth
-- creates ava_runner, and base/cluster/authority/groups.py's ensure_groups
-- grants the same surface at birth.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT, UPDATE ON plugin_stats TO ava_runner;
    END IF;
END $$;

-- ─────────────── understanding_nodes ───────────────
-- The understanding tree behind the run-timeline narrative layers. One row per
-- node, keyed by the deterministic message span: depth 1 is written by a chunk
-- call (a group of message units and its summary), each level above by an
-- upper-level grouping check. The node's single text plus the hashes that make
-- writes idempotent (text_hash). Boundaries are never trimmed (#1125), so the
-- span identity is stable across runs.
CREATE TABLE understanding_nodes (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL,
    depth INTEGER NOT NULL CHECK (depth >= 1),
    span_start INTEGER NOT NULL,
    span_end INTEGER NOT NULL,
    start_ts TIMESTAMPTZ,
    end_ts TIMESTAMPTZ,
    segment_key TEXT NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    children_count INTEGER NOT NULL,
    parent_id BIGINT REFERENCES understanding_nodes(id) ON DELETE SET NULL,
    -- What produced the node, so its generation cost joins by id: a level-1 node names its chunk
    -- job (understanding_chunk_jobs.id), a node above names its grouping check
    -- (understanding_group_calls.check_key). NULL on rows written before the link existed.
    job_id BIGINT,
    check_key TEXT,
    model TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    -- Every written node is final (its text is a pure function of its input
    -- hash); a rebuild reconciles superseded provisional cuts away rather
    -- than rewriting them (store.write_tree). Kept explicit for a future
    -- structure-ahead-of-text pass; today every row is TRUE.
    sealed BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX understanding_nodes_identity
    ON understanding_nodes (agent_id, depth, span_start, span_end);
CREATE INDEX understanding_nodes_window
    ON understanding_nodes (agent_id, start_ts, end_ts);
CREATE INDEX understanding_nodes_reuse
    ON understanding_nodes (agent_id, input_hash);

COMMENT ON TABLE understanding_nodes IS
    'The understanding tree: one text per (agent_id, depth, message span) — depth 1 written by a chunk call, each level above by an upper-level grouping check; hashes make rewrites idempotent.';

-- ava_runner surface: the generation pass ships as a gateway-side worker, but
-- the operational first-run / ad-hoc regeneration path executes from the
-- agent/runner side (task #3704) — SELECT, INSERT, UPDATE, and DELETE for the
-- write-side reconciliation that removes rows of a superseded earlier cut
-- when a rebuild re-cuts the same stretch. Gated on the role's existence:
-- fresh bootstrap applies this baseline before install birth creates
-- ava_runner, and
-- base/cluster/authority/groups.py's ensure_groups grants the same surface at
-- birth.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON understanding_nodes TO ava_runner;
        GRANT USAGE, SELECT ON SEQUENCE understanding_nodes_id_seq TO ava_runner;
    END IF;
END $$;

-- ─────────────── understanding_chunk_jobs ───────────────
-- Chunk-triggered understanding queue (see migrations/20261007T045501_understanding-chunk-tree.sql):
-- one row per context stretch the understanding layer must describe; claimed
-- with SKIP LOCKED by the agent-host loop. Gated grant: fresh bootstrap applies
-- this baseline before install birth creates ava_runner.
CREATE TABLE IF NOT EXISTS understanding_chunk_jobs (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL,
    compact_version INTEGER NOT NULL,
    start_index INTEGER NOT NULL,
    end_index INTEGER NOT NULL,
    end_msg_id TEXT NOT NULL,
    boundary_checkpoint_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    -- When the job first waited for something outside it (a checkpoint that has not caught up, a
    -- database blink); the give-up clock. NULL while it has never waited, so a job queued while
    -- the feature is off is not timed.
    waiting_since TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS understanding_chunk_jobs_identity
    ON understanding_chunk_jobs (agent_id, compact_version, start_index, end_index);
CREATE INDEX IF NOT EXISTS understanding_chunk_jobs_live
    ON understanding_chunk_jobs (id) WHERE status IN ('pending', 'running');

COMMENT ON TABLE understanding_chunk_jobs IS
    'Chunk-triggered understanding queue: one row per context stretch to describe; claimed with SKIP LOCKED by the agent-host loop, result lands as a depth-1 understanding_nodes row.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT, UPDATE ON understanding_chunk_jobs TO ava_runner;
        GRANT USAGE, SELECT ON SEQUENCE understanding_chunk_jobs_id_seq TO ava_runner;
    END IF;
END $$;

-- ─────────────── understanding_chunk_calls ───────────────
-- Raw record of each provider call of chunk-triggered understanding (see
-- migrations/20261007T045501_understanding-chunk-tree.sql): the instruction, the reply as
-- returned, usage, timing; failed calls included. Gated grant, like the queue.
CREATE TABLE IF NOT EXISTS understanding_chunk_calls (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL,
    agent_id BIGINT NOT NULL,
    attempt INTEGER NOT NULL,
    round INTEGER NOT NULL,
    model TEXT NOT NULL,
    instruction TEXT NOT NULL,
    prefix_len INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    content JSONB,
    tool_calls JSONB,
    additional_kwargs JSONB,
    usage_metadata JSONB,
    response_metadata JSONB,
    duration_ms DOUBLE PRECISION NOT NULL,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind TEXT NOT NULL DEFAULT 'leaf',
    problem TEXT
);
CREATE INDEX IF NOT EXISTS understanding_chunk_calls_job
    ON understanding_chunk_calls (job_id, attempt, round);
CREATE INDEX IF NOT EXISTS understanding_chunk_calls_agent
    ON understanding_chunk_calls (agent_id, created_at);

COMMENT ON TABLE understanding_chunk_calls IS
    'Raw record of each provider call of chunk-triggered understanding (instruction, reply as returned, usage, timing, error); failed calls included.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT INSERT ON understanding_chunk_calls TO ava_runner;
        GRANT USAGE, SELECT ON SEQUENCE understanding_chunk_calls_id_seq TO ava_runner;
    END IF;
END $$;

-- ─────────────── understanding_group_state / understanding_group_calls ───────────────
-- Upper-level grouping of the understanding tree (see migrations/20261007T045501_understanding-chunk-tree.sql):
-- the per-(agent, level) check cursor with its lease, and the raw record of each grouping provider call.
CREATE TABLE IF NOT EXISTS understanding_group_state (
    agent_id BIGINT NOT NULL,
    level INTEGER NOT NULL,
    last_checked_open INTEGER NOT NULL DEFAULT 0,
    claimed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, level)
);

COMMENT ON TABLE understanding_group_state IS
    'Upper-level grouping cursor per (agent, level): open-node count at the last check, and the lease of the runner checking it.';

CREATE TABLE IF NOT EXISTS understanding_group_calls (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL,
    level INTEGER NOT NULL,
    check_key TEXT NOT NULL,
    round INTEGER NOT NULL,
    model TEXT NOT NULL,
    mode TEXT,
    open_ids BIGINT[] NOT NULL,
    request TEXT NOT NULL,
    content JSONB,
    additional_kwargs JSONB,
    usage_metadata JSONB,
    response_metadata JSONB,
    duration_ms DOUBLE PRECISION NOT NULL,
    problem TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS understanding_group_calls_check
    ON understanding_group_calls (check_key, round);
CREATE INDEX IF NOT EXISTS understanding_group_calls_agent
    ON understanding_group_calls (agent_id, created_at);

COMMENT ON TABLE understanding_group_calls IS
    'Raw record of each provider call of an upper-level grouping check (request, reply as returned, usage, timing, refusal reason, error); failed calls included.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT, UPDATE ON understanding_group_state TO ava_runner;
        GRANT INSERT ON understanding_group_calls TO ava_runner;
        GRANT USAGE, SELECT ON SEQUENCE understanding_group_calls_id_seq TO ava_runner;
    END IF;
END $$;

-- ─────────────── audit_events ───────────────
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

-- ─────────────── telemetry_events ───────────────
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

CREATE INDEX telemetry_events_agent_ts ON telemetry_events (agent_id, ts);
CREATE INDEX telemetry_events_name_ts ON telemetry_events (event_name, ts);
CREATE INDEX telemetry_events_trace ON telemetry_events (trace_id) WHERE trace_id IS NOT NULL;

-- Warning, error and critical rows are a small share of telemetry_events, and the stats
-- dashboard and the event-class resolution pass count exactly those over windows of minutes
-- to a week (migration 20261003T000100_telemetry-events-anomaly-index).
CREATE INDEX IF NOT EXISTS telemetry_events_anomaly_ts
    ON telemetry_events (ts)
    INCLUDE (cluster, category, level, event_name, source, process)
    WHERE level IN ('warning', 'error', 'critical');

-- A window's rows counted per agent and per event name come from the index alone (migration
-- 20261003T010000_telemetry-events-metrics-index); it also serves every plain ts range scan.
CREATE INDEX IF NOT EXISTS telemetry_events_ts_agent_name
    ON telemetry_events (ts)
    INCLUDE (agent_id, event_name);

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

-- Monthly partitions (UTC boundaries) from p_months_back months before the current
-- month through p_months_ahead months ahead, created idempotently. The application logins
-- have no DDL, so the writer calls this SECURITY DEFINER function when the
-- month changes. A DEFAULT partition catches an event outside every month so a
-- write never fails; rows left there block creating the month that covers them,
-- so the function's failure is loud, not silent.
CREATE FUNCTION public.ensure_telemetry_event_partitions(
    p_months_ahead INT DEFAULT 3, p_months_back INT DEFAULT 1)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
DECLARE
    month_start TIMESTAMPTZ;
    offset_months INT;
BEGIN
    EXECUTE 'create table if not exists public.telemetry_events_default '
            'partition of public.telemetry_events default';
    FOR offset_months IN -p_months_back..p_months_ahead LOOP
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

REVOKE ALL ON FUNCTION public.ensure_telemetry_event_partitions(INT, INT) FROM PUBLIC;
SELECT public.ensure_telemetry_event_partitions(3);

-- Application surface: both groups read and append, as for audit_events (see
-- there for why the gateway group's UPDATE and DELETE are revoked and the
-- role-existence gate).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT ON telemetry_events TO ava_runner;
        GRANT EXECUTE ON FUNCTION public.ensure_telemetry_event_partitions(INT, INT) TO ava_runner;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_gateway') THEN
        REVOKE UPDATE, DELETE ON telemetry_events FROM ava_gateway;
    END IF;
END $$;

-- ─────────────── im_bridge_cursors ───────────────
-- im-bridge durable positions (migration 20261002T051710_im-bridge-cursors).
CREATE TABLE im_bridge_cursors (
    channel         TEXT        NOT NULL,
    chat_id         TEXT        NOT NULL,
    push_agent_id   BIGINT,
    push_account_id TEXT,
    push_initialized BOOLEAN NOT NULL DEFAULT FALSE,
    push_item_id    TEXT,
    push_created_at TEXT,
    poll_message_id TEXT,
    poll_create_ms  BIGINT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, chat_id),
    CONSTRAINT im_bridge_cursors_push_pair
        CHECK (push_item_id IS NULL OR push_agent_id IS NOT NULL)
);

COMMENT ON TABLE im_bridge_cursors IS
    'Durable im-bridge selection and positions: push_* = timeline acceptance, poll_* = independently handled platform inbound. Acceptance and provider intents commit atomically; cursor advancement is not proof of delivery.';
COMMENT ON COLUMN im_bridge_cursors.push_agent_id IS
    'Canonical recipient selection after initialization/binding; push_item_id belongs to this agent and may be NULL after explicit empty acceptance or clear.';
COMMENT ON COLUMN im_bridge_cursors.push_initialized IS
    'Explicitly accepted initial history, including empty switch batches. FALSE legacy NULL positions remain held without implicit backfill.';
COMMENT ON COLUMN im_bridge_cursors.push_created_at IS
    'Timestamp of newest durably accepted timeline item, not proof of provider delivery. NULL legacy positions compare item_id alone.';

CREATE TABLE im_bridge_outbound_intents (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    agent_id BIGINT,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('message', 'inbound', 'notice', 'alert_group')),
    source_id TEXT NOT NULL,
    block_idx INTEGER NOT NULL CHECK (block_idx >= 0),
    replay_id TEXT NOT NULL DEFAULT '',
    request JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'sending', 'sent', 'uncertain', 'failed')),
    attempt_id UUID,
    outcome_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT im_bridge_outbound_identity UNIQUE NULLS NOT DISTINCT (channel, account_id, chat_id, agent_id, source_kind, source_id, block_idx, replay_id),
    CONSTRAINT im_bridge_outbound_context CHECK ((source_kind='alert_group' AND agent_id IS NULL AND block_idx=0 AND replay_id='')
           OR (source_kind<>'alert_group' AND agent_id IS NOT NULL AND agent_id>0)),
    CHECK ((status = 'queued') = (attempt_id IS NULL))
);
CREATE INDEX im_bridge_outbound_pending ON im_bridge_outbound_intents (id)
    WHERE status IN ('queued', 'sending');
COMMENT ON TABLE im_bridge_outbound_intents IS
    'Immutable IM timeline and normal notice intents. Producer acceptance commits with its receipt/cursor; sending is persisted before provider calls. Unresolved attempts are uncertain and never automatically replayed. No expiry.';
COMMENT ON COLUMN im_bridge_cursors.push_account_id IS
    'Nonsecret adapter account owning the accepted push cursor; legacy NULL binds once without backfill. Explicit switch replay alone may rebind another account.';


CREATE TABLE im_bridge_outbound_replays (
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    replay_id TEXT NOT NULL,
    switch_arg TEXT NOT NULL,
    agent_id BIGINT NOT NULL,
    intent_ids BIGINT[] NOT NULL,
    push_item_id TEXT,
    push_created_at TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, account_id, chat_id, replay_id)
);
COMMENT ON TABLE im_bridge_outbound_replays IS
    'Immutable switch-replay batch acceptance receipts; repeated platform invocations recover the original selected intent IDs and cursor. No expiry or foreign-key pin to delivery rows.';

-- ─────────────── schema_migrations ───────────────
-- Applied-migration registry — maintained by `base.deploy.schema.migrations`. Keyed by
-- migration NAME (an applied SET, not a high-water integer). This whole file is
-- the squashed baseline, so a fresh DB stamps the baseline sentinel and any
-- non-idempotent deltas already folded into this schema instead of replaying
-- them. Post-baseline deltas live in
-- `migrations/YYYYMMDDTHHMMSS_*.sql`; after a successful apply the runner INSERTs
-- the new name. Keep `_BASELINE_NAME` in `base/deploy/schema/migrations.py` in sync with the
-- sentinel below (CI lint checks it).
CREATE TABLE schema_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed: stamp the squashed baseline (this file IS the baseline). A fresh DB is
-- "already at the baseline"; `apply_pending_migrations` then applies only the
-- post-baseline files in migrations/ that are not folded below.
INSERT INTO schema_migrations (name) VALUES ('00000000T000000_baseline');

-- Strict post-baseline deltas retained from concurrent upstream work.
INSERT INTO schema_migrations (name) VALUES ('20260922T053200_impersonation-delivery-budget');
INSERT INTO schema_migrations (name) VALUES ('20260922T075826_impersonation-delivery-config');

-- Current reset anchor: the previous 101 migration names are folded above.
INSERT INTO schema_migrations (name) VALUES ('20260923T031516_schema-baseline');
INSERT INTO schema_migrations (name) VALUES ('20260923T175411_agent-creation-availability');
INSERT INTO schema_migrations (name) VALUES ('20260923T195300_agent-launch-failure');
INSERT INTO schema_migrations (name) VALUES ('20260923T205208_impersonation-event-manifest');
INSERT INTO schema_migrations (name) VALUES ('20260924T070003_hierarchy-worker-breaker');
INSERT INTO schema_migrations (name) VALUES ('20260924T071500_hierarchy-jobs-runner-grant');
INSERT INTO schema_migrations (name) VALUES ('20260924T150840_impersonation-receipt-lock-door');
INSERT INTO schema_migrations (name) VALUES ('20260924T193804_task-escalation-marker');
INSERT INTO schema_migrations (name) VALUES ('20260926T135638_impersonation-dsh-relay');
INSERT INTO schema_migrations (name) VALUES ('20261002T044224_audit-events');
INSERT INTO schema_migrations (name) VALUES ('20261002T153217_telemetry-events');

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

INSERT INTO schema_migrations (name) VALUES ('20261005T095252_separate-impersonation-transport-lifecycle');
INSERT INTO schema_migrations (name) VALUES ('20261006T181011_completion-notice-events-stop-outcome');
INSERT INTO schema_migrations (name) VALUES ('20261007T113552_completion-notice-events-drop-outcome');

INSERT INTO schema_migrations (name) VALUES ('20261007T103034_agent-creation-identity');
-- Domain receipts never expire into fresh notice effects.
CREATE TABLE notice_operation_receipts (
    path TEXT NOT NULL,
    operation_key TEXT NOT NULL CHECK (char_length(operation_key) BETWEEN 1 AND 128),
    request JSONB NOT NULL,
    receipt JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (path, operation_key)
);

INSERT INTO schema_migrations (name) VALUES ('20261007T103457_notice-operation-receipts');

-- Acceptance snapshots survive queue retention without pinning its rows.
CREATE TABLE agent_control_receipts (
    path TEXT NOT NULL,
    operation_key TEXT NOT NULL CHECK (char_length(operation_key) BETWEEN 1 AND 128),
    agent_id BIGINT NOT NULL CHECK (agent_id > 0),
    kind TEXT NOT NULL CHECK (kind IN ('cancel','compact_request')),
    result TEXT NOT NULL CHECK (result IN ('enqueued','already_terminated')),
    inbound_id BIGINT CHECK (inbound_id > 0),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (path, operation_key),
    CONSTRAINT agent_control_receipt_shape_check CHECK (
        (result = 'enqueued' AND inbound_id IS NOT NULL)
        OR (result = 'already_terminated' AND kind = 'cancel' AND inbound_id IS NULL)
    )
);
COMMENT ON TABLE agent_control_receipts IS
    'Immutable cancel/compact acceptance snapshots, not execution receipts or a queue; no expiry or foreign key may turn a retained retry into fresh work or pin queue retention.';

INSERT INTO schema_migrations (name) VALUES ('20261007T172200_agent-control-acceptance');

CREATE TABLE agent_upload_batches (
    operation_key text PRIMARY KEY,
    agent_id bigint NOT NULL,
    request_fingerprint text NOT NULL,
    manifest jsonb NOT NULL,
    receipt jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    ready_at timestamptz,
    CHECK ((receipt IS NULL) = (ready_at IS NULL))
);
CREATE INDEX agent_upload_batches_receiving_agent_idx
    ON agent_upload_batches (agent_id) WHERE receipt IS NULL;
COMMENT ON TABLE agent_upload_batches IS
    'Silent upload identities; receiving manifests reserve quota without expiry, ready receipts replay acceptance.';

INSERT INTO schema_migrations (name) VALUES ('20261007T181501_silent-upload-batches');

CREATE TABLE task_patch_receipts (
    path TEXT NOT NULL,
    operation_key TEXT NOT NULL CHECK (length(operation_key) BETWEEN 1 AND 128),
    request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (path, operation_key)
);

COMMENT ON TABLE task_patch_receipts IS
    'Immutable task PATCH acceptance snapshots; retained independently of tasks and notifications.';

INSERT INTO schema_migrations (name) VALUES ('20261007T181608_task-patch-receipts');
CREATE TABLE task_update_receipts (
    actor_agent_id BIGINT NOT NULL CHECK (actor_agent_id > 0),
    task_id BIGINT NOT NULL CHECK (task_id > 0),
    operation_key TEXT NOT NULL CHECK (length(operation_key) BETWEEN 1 AND 128),
    request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (actor_agent_id, task_id, operation_key)
);

COMMENT ON TABLE task_update_receipts IS
    'Immutable SDK task update/log commit tombstones; agent provenance scope, not authentication or notification execution receipts.';

INSERT INTO schema_migrations (name) VALUES ('20261007T183849_task-update-receipts');

CREATE TABLE task_creation_receipts (
    actor_agent_id BIGINT NOT NULL CHECK (actor_agent_id > 0),
    operation_key TEXT NOT NULL CHECK (length(operation_key) BETWEEN 1 AND 128),
    request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (actor_agent_id, operation_key)
);

INSERT INTO schema_migrations (name) VALUES ('20261007T190652_task-creation-receipts');
INSERT INTO schema_migrations (name) VALUES ('20261007T181301_im-timeline-outbox');
INSERT INTO schema_migrations (name) VALUES ('20261007T202446_alert-notification-shadow-facts');

CREATE TABLE task_assignment_receipts (
    operation_key TEXT PRIMARY KEY CHECK (char_length(operation_key) BETWEEN 1 AND 128),
    request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    result JSONB NOT NULL CHECK (
        jsonb_typeof(result) = 'object'
        AND result ?& ARRAY['task', 'agent_id', 'launch_attempt_id']
        AND jsonb_typeof(result->'task') = 'object'
        AND result->>'agent_id' ~ '^[1-9][0-9]*$'
        AND result->>'launch_attempt_id' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
    ),
    birth_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(birth_snapshot) = 'object'
        AND birth_snapshot ?& ARRAY['machine', 'config_overlay', 'birth_config', 'preset_name', 'launch_attempt_id']
        AND jsonb_typeof(birth_snapshot->'machine') = 'string'
        AND jsonb_typeof(birth_snapshot->'birth_config') = 'object'
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE task_assignment_receipts IS
    'Immutable principal-scoped compound acceptance tombstones; no FK or automatic expiry.';

INSERT INTO schema_migrations (name) VALUES ('20261007T201556_task-assignment-receipts');

CREATE TABLE im_bridge_notice_poll_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    legacy_floor BIGINT NOT NULL CHECK (legacy_floor >= 0),
    import_reason TEXT NOT NULL CHECK (import_reason IN ('legacy_cursor', 'no_history', 'legacy_history_unknown')),
    accepted_notice_id BIGINT NOT NULL DEFAULT 0 CHECK (accepted_notice_id >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE im_bridge_notice_poll_state IS
    'One-time normal Telegram notice cutover. The immutable legacy floor preserves imported skips or unknown old history; accepted_notice_id is diagnostic and never eligibility.';

CREATE TABLE im_bridge_notice_acceptances (
    notice_id BIGINT PRIMARY KEY CHECK (notice_id > 0),
    decision TEXT NOT NULL CHECK (decision IN ('queued', 'filtered')),
    request JSONB NOT NULL,
    intent_ids BIGINT[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((decision = 'filtered') = (cardinality(intent_ids) = 0))
);
COMMENT ON TABLE im_bridge_notice_acceptances IS
    'Normal-poll notice source receipts with immutable destination/rendering or deliberate filter decision. No foreign-key pin or expiry; explicit listing is a separate producer.';

INSERT INTO schema_migrations (name) VALUES ('20261007T194939_im-notice-poll-acceptance');
CREATE TABLE agent_launch_retry_receipts (
    operation_key text PRIMARY KEY,
    agent_id bigint NOT NULL,
    prior_attempt_id uuid NOT NULL,
    launch_attempt_id uuid NOT NULL UNIQUE,
    machine text NOT NULL,
    config_overlay jsonb,
    birth_config jsonb,
    acceptance jsonb NOT NULL
);
COMMENT ON TABLE agent_launch_retry_receipts IS
'Immutable guarded retry-launch intents; retain after target deletion, no TTL.';

INSERT INTO schema_migrations (name) VALUES ('20261008T021500_agent-launch-retry-receipts');

-- Historical page acceptance survives registry/agent cleanup.
CREATE TABLE page_operation_receipts (
    operation_key text PRIMARY KEY,
    request_hash text NOT NULL CHECK (length(request_hash) = 64),
    acceptance jsonb NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations (name) VALUES ('20261007T205013_page-operation-receipts');

CREATE TABLE im_bridge_alert_acceptances (
    group_id BIGINT PRIMARY KEY CHECK (group_id > 0),
    request JSONB NOT NULL,
    decisions JSONB NOT NULL,
    intent_ids BIGINT[] NOT NULL CHECK (cardinality(intent_ids)>0),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE im_bridge_alert_acceptances IS
    'Frozen available recipient subset and retained unavailable channel decisions, accepted atomically with shared intents. No FK to mutable/retained source or queue, no expiry, no retrospective fanout.';

COMMENT ON COLUMN alerts.notified_revision IS
    'Native revision completed by at least one real SENT channel; legacy notified_at never populates this fact.';
INSERT INTO schema_migrations (name) VALUES ('20261007T211550_native-alert-outbound');
