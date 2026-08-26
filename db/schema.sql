-- Ava schema — the squashed **baseline**: the full current schema a fresh DB
-- bootstraps from. Since the 2026-07-19 re-baseline this file IS the source of
-- truth for the current schema (history through sequential 0001..0081 was folded
-- in). A schema change ships as a `migrations/YYYYMMDDTHHMMSS_*.sql` delta AND is
-- reflected here, so this file always describes the latest schema.
--
-- Who uses this file (fresh-DB bootstrap — applies the whole baseline):
--   - `shared.cluster.provision_database` — a new cluster's DB is created and this
--     file applied as the owning role (the real production fresh-bootstrap path)
--   - `docker-compose.yml` mounts it to /docker-entrypoint-initdb.d/01-schema.sql,
--     applied automatically by the image on the first Postgres container startup
--   - `tests/conftest.py` / `tests/e2e/conftest.py` execute the whole file each session
--     when creating an independent test DB
--   - `evals/driver.py` applies it when creating an eval DB
--   - `tests/test_agent_status_schema_sync.py` directly parses this file to verify CHECK
--     constraint sync
--
-- Who does not use this file:
--   - `shared.migrations.apply_pending_migrations` — it applies the post-baseline
--     `migrations/*.sql` deltas one by one, doesn't read this file. A fresh DB is
--     already at the baseline (via one of the paths above), so apply then only
--     runs whatever deltas accrued since; production self-update goes through it.
--
-- LangGraph PostgresSaver creates only at fresh install; later versions are
-- mirrored by paired Ava migrations so cluster rollback can reverse them:
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
--   restarting — UPDATE before graceful exit, after the agent receives a restart inbound. The gateway
--                restart watcher sees this state → auto-resurrects to spawn a fresh process
--                attached to the same agent (new PID, LangGraph state preserved)
--   hibernating — ops-only memory swap-out state, projected as idling to SDK and frontend consumers
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
--   fork_source_agent_id      source agent of a fork; NULL for non-fork spawns
--   fork_source_checkpoint_id exact checkpoint id of the source agent (filled when forking);
--                             LangGraph's checkpoints are append-only, so a fork must
--                             point at a specific snapshot rather than "latest" — latest drifts under
--                             concurrent writes, and a fork should be reproducible
CREATE TABLE agents_meta (
    id                         BIGINT PRIMARY KEY REFERENCES agents(id),
    spawner                    TEXT NOT NULL DEFAULT 'user',
    fork_source_agent_id       BIGINT REFERENCES agents(id),
    fork_source_checkpoint_id  TEXT,
    status                     TEXT NOT NULL CHECK (status IN ('running', 'idling', 'restarting', 'terminated', 'hibernating')),
    pid                        INTEGER,                  -- filled while a process owns the running/idling row, for ops ps lookup / force kill
    spawned_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at                 TIMESTAMPTZ,              -- filled alongside pid and lease by agent._starting.claim_agent_row
    session_index              BIGINT NOT NULL DEFAULT 0,  -- unified shell+watcher session sequence number, auto-incrementing; ava.shell.new()/ava.watcher.launch() atomically take the next via UPDATE ... RETURNING
    machine                    TEXT NOT NULL DEFAULT 'unknown',  -- physical machine identifier, for multi-machine deployment (private network + central Postgres); source = $AVA_HOME/machine_name, written on INSERT in the create path (spawn_agent), claim_agent_row only verifies
    status_changed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),  -- when the row last entered its current status; maintained by the agents_meta_status_changed_at trigger. Lets the restarter reap unclaimed idling rows older than a grace (unlike spawned_at, this resets on resurrect's terminated -> idling)
    last_active_at             TIMESTAMPTZ NOT NULL DEFAULT now(),  -- when the agent last did REAL work: written = now() by the agent process on every completed LLM turn (agent/graph/_llm.py). Deliberately NOT touched by ops lifecycle churn (rollout quiesce / restarter respawn / self.update / stop-start) — for an idle agent that whole cycle runs without an LLM turn, so this survives it. The heartbeat daemon's idle clock reads THIS (not status_changed_at, which every status flip incl. ops restarts bumps) so an ops event never resets an agent's idle timer. Backfilled from status_changed_at at add time.
    heartbeat_paused_until     TIMESTAMPTZ,              -- pause window for the gateway heartbeat daemon; set to now()+duration by ava.self.pause_heartbeat(). While in the future, the daemon skips this agent's idle-nudge. NULL = never paused.
    last_message_text          TEXT,                     -- text of the last AI message produced by this agent; survives compact (which replaces the entire checkpoint). Written by the agent process after each LLM turn; read by get_last_message API. NULL = no AI message yet.
    config_overlay             JSONB,                    -- per-agent config overlay (currently llm_model); authoritative source read by spawn/respawn/resurrect, passed to the child as --config-overlay. NULL = cluster defaults.
    birth_config               JSONB,                    -- the values the cluster defaults resolved to at THIS agent's birth, for every per-agent field the registry declares lifecycle="frozen" (shared/config: the brain + the system-prompt-shaping set). Stamped once at the spawn boundary (shared/birth_config.py), replayed on every restart/respawn/resurrect/compact, and inherited verbatim by a fork. Deliberately a SEPARATE column from config_overlay so provenance survives: config_overlay = "someone chose this for this agent", birth_config = "nobody chose; this was merely the cluster default that day". Resolution order everywhere is config_overlay > birth_config > current config. NULL = resolve every frozen field live (pre-column rows the backfill skipped). A migration that rewrites a frozen field's stored VALUE must rewrite this column too — it is a second home for values that used to live only in config_overlay (precedent: 20260725T060802_pin-haiku-dated-model-id.sql rewrites config_overlay->>'llm_model'). The skill-name renames are NOT such a case: they canonicalize agent_presets.config only, since shared/skill_names.py folds dash and underscore so an already-stored per-agent value still resolves. See migrations/20260731T071400_agent-birth-config.sql.
    termination_source         TEXT CHECK (termination_source IN ('user', 'exit', 'reaper', 'launch-confirm', 'integrity')),  -- WHO/WHAT terminated the row; meaningful only while status='terminated'. Value set = shared.agents.TerminationSource (locked by tests/test_db_check_enum_sync.py); stamped in the SAME statement as the status flip by every terminated-write site (enforced by scripts/lint_termination_source.py). 'user' = force-kill / terminate-of-already-dead (ops_lifecycle._force_mark_terminated); 'exit' = agent's own graceful process-exit finalize (mark_agent_exited_op); 'reaper' = restarter corpse reaper forced it (dead pid / stale unclaimed idling row); 'launch-confirm' = a launch that never confirmed forced it — the launcher's confirm poll timing out (agent_launch) or the child's own early-boot schema/placement gate rejecting the boot before it claimed the row (agent/_starting.py); 'integrity' = the framework found the row's own state self-inconsistent and killed it (respawn_agent: status='restarting' with no 'restart' inbound), deliberately NOT resurrectable since the row's history is corrupt and a retry loop would bury a one-time fault. CrashResurrectController resurrects ONLY 'reaper' + 'launch-confirm' (involuntary/system-detected + self-healing); 'user'/'exit'/'integrity'/NULL are never auto-resurrected. NULL = pre-column legacy row → conservatively not eligible. Cleared to NULL on the terminated→idling resurrect transition (per-death). CHECK permits NULL.
    last_force_terminate_inbound_id BIGINT,              -- monotonic explicit-kill fence: every force termination (including an already-terminated row) inserts a kind='terminate' inbound under the agents_meta row lock and stores its id here. Pending-work resurrection (chat/compact_request) requires its exact pending inbound id to be greater than this fence, so older work cannot reverse a later kill. No FK on purpose: inbound retention must not erase lifecycle intent. Never cleared; NULL = no force intent recorded.
    last_resurrect_at          TIMESTAMPTZ,              -- when CrashResurrectController last auto-resurrected this agent; the per-agent backoff clock (pin-heal shape). A crash corpse is skipped until now() - last_resurrect_at exceeds AVA_AUTO_RESURRECT_BACKOFF_SECONDS, so a resurrect that keeps failing (outage / poison message) retries on a fixed cadence instead of a tight loop and self-heals when the cause clears. NULL = never auto-resurrected.
    last_wedged_check_at       TIMESTAMPTZ,              -- when WedgedAgentController last attempted recovery of this agent; the per-agent backoff clock (same shape as last_resurrect_at). Stamped by the claiming UPDATE in ops/controllers/wedged.py; a wedged candidate is skipped until now() - last_wedged_check_at exceeds the backoff, preventing a poison-message loop from becoming a kill-spawn cycle. NULL = never checked. See the add-last-wedged-check-at migration.
    lease_expires_at           TIMESTAMPTZ,              -- R1 (Task #1021): the agent-process lease — liveness is a lease-expiry judgment (`lease_expires_at > now()`), status stays lifecycle intent. Written by the agent process at claim/start (now()+lease TTL, agent/db.py / agent/_starting.py), cleared on terminate/resurrect by the ops lifecycle; read by the heartbeat daemon and the reaper (shared/db.py ALIVE_SQL). NULL = row has no lease (pre-R1 legacy, or terminated).
    liveness_state             TEXT NOT NULL DEFAULT 'unknown' CHECK (liveness_state IN ('online', 'offline', 'unknown')),  -- gateway-owned derived liveness projection (Task #1174): 'online' = machine reachable AND (process lease alive where one is held); 'offline' = machine unreachable (2 consecutive failed status_probe) or lease expired; 'unknown' = not yet judged (fresh rows / unregistered machine). Written ONLY by the gateway heartbeat daemon's liveness pass — status stays lifecycle intent (R1 invariant #1); the frontend renders offline distinctly. 'terminated' rows are never judged.
    last_probe_at             TIMESTAMPTZ,              -- when the gateway liveness pass last judged this row (Task #1174).
    last_compact_at            TIMESTAMPTZ,              -- R1 (Task #1021): synchronous compact stamp — written by agent/hooks/compact.py at each compact, replacing the events-table OFFSET-1 read-your-own-write hack (the anchor for "last compact" without scanning events). NULL = never compacted.
    -- fork fields exist in pairs or not at all (constraint explicitly named to align with the ALTER in 0002 migration)
    CONSTRAINT agents_meta_fork_pair_check
        CHECK ((fork_source_agent_id IS NULL) = (fork_source_checkpoint_id IS NULL))
);

COMMENT ON COLUMN agents_meta.last_force_terminate_inbound_id IS
    'Monotonic inbound id fence written by every explicit force termination. '
    'Pending work may auto-resurrect this agent only when its inbound id is greater '
    'than this fence. Deliberately no foreign key: inbound retention must not '
    'erase lifecycle intent.';

-- ─────────────── agent_activity ───────────────
-- Append-only trail of an agent's self-reported activity. ava.self.log()
-- INSERTs one (agent_id, text, created_at) row per call. The agent snapshot derives
-- the "current" activity line from the latest row per agent; the monitoring / fleet
-- view replays the full ordered trail. Supersedes agents_meta.activity (DEPRECATED, see above).
CREATE TABLE agent_activity (
    id          BIGSERIAL PRIMARY KEY,
    agent_id    BIGINT NOT NULL REFERENCES agents(id),
    text        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Serves the latest-line lateral lookup in the snapshot query: the
-- (agent_id, created_at DESC, id DESC) shape matches the lateral's
-- ORDER BY exactly (select_all; audit P1-1), and the trail replay reads
-- it backwards. The old (agent_id, created_at DESC) prefix index was
-- dropped with the migration that introduced this one.
CREATE INDEX agent_activity_agent_id_created_at_id_idx
    ON agent_activity (agent_id, created_at DESC, id DESC);

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
    resolution      TEXT CHECK (resolution IN ('answered', 'dismissed', 'read', 'withdrawn', 'superseded')),
    reply           TEXT,
    -- Optional task this notice belongs to: the agent reports the task it was
    -- working when it posted, so the human queue groups notices by task rather
    -- than only by owner agent. NULL is the norm (a notice need not name one).
    -- The FK to agent_tasks is added by ALTER after that table is defined below
    -- (agent_notices is declared first, so an inline REFERENCES would forward-ref).
    task_id         BIGINT,
    CONSTRAINT agent_notices_agent_local_id_unique UNIQUE (agent_id, local_id),
    CONSTRAINT agent_notices_blocking_requires_response
        CHECK (NOT blocking OR require_response),
    CONSTRAINT agent_notices_resolution_pair
        CHECK ((resolved_at IS NULL) = (resolution IS NULL)),
    CONSTRAINT agent_notices_resolution_legal
        CHECK (resolution IS NULL
               OR (require_response AND resolution IN ('answered', 'dismissed', 'withdrawn', 'superseded'))
               OR (NOT require_response AND resolution IN ('answered', 'read', 'withdrawn', 'superseded'))),
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
                   'compact_summary',   -- written by agent ava.compact(summary); on claim, directly replaces messages
                   'compact_request',   -- triggered by UI "/compact" / admin; on claim, runs the backend LLM to generate a summary, then replaces
                   'cancel',            -- /api/cancel pause; in-flight llm/exec interrupts on it, claim halts to idle (agent stays alive, resumable)
                   'terminate',         -- ava.terminate() / admin terminate; claim appends lifecycle marker + goto END
                   'restart',           -- ava.restart() / admin restart; claim only marks RESTARTING + goto END (no message appended)
                                        -- the gateway watcher sees restarting and auto-respawns a fresh process + delivers 'restart_completed'
                   'restart_completed', -- INSERTed by respawn_agent; after the new process is up, claim appends lifecycle marker
                   'resurrect',         -- INSERTed by resurrect_agent; after the new process is up, claim appends lifecycle marker
                   'fork',              -- INSERTed by spawn_agent on a fork; the new process's first claim appends an identity marker (you are now agent N, forked from agent:M)
                   'heartbeat'          -- INSERTed by heartbeat daemon; idle-agent nudge delivered as a system note
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
    claimed_at TIMESTAMPTZ
);

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

-- ─────────────── events Since-Birth rollup ───────────────
-- Day-grain rollups that preserve the `events` "since-birth" aggregates
-- across retention (events is partitioned by month and old partitions are
-- DROPped; these tables survive so the whole-life aggregates the readers need do
-- not vanish with the raw rows). The whole ~59-day history reduces to ~2900 rows,
-- so these are never themselves subject to retention. A gateway maintenance daemon
-- (services.events_maintenance) upserts them daily; the upsert is a full-day
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
-- while the owning request executes. Rows live 7 days (pruned
-- opportunistically by the middleware on each claim).
CREATE TABLE api_idempotency (
    key TEXT PRIMARY KEY,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status INTEGER,
    op_status TEXT,
    response_body JSONB,
    response_headers JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

COMMENT ON TABLE api_idempotency IS
    'AtLeastOnceWithKey dedup for routes declaring Idempotency.AT_LEAST_ONCE_WITH_KEY (R3 doorplate 1): the first request with an Idempotency-Key header stores its response; same-key retries replay it instead of re-executing.';
COMMENT ON COLUMN api_idempotency.status IS
    'HTTP status of the stored response; NULL while the owning request is still executing.';
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
-- events-maintenance Loki rollup pass; the cost read path is these rows + a
-- live Loki tail for today.
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

-- Rollback snapshot retained by the 2026-08-24 unpriced-cost backfill. A
-- production upgrade fills it from the ledger's pre-state; a fresh database
-- keeps it empty because there is no historical ledger state to restore.
CREATE TABLE ledger_unpriced_backfill_20260824 (
    agent_id       BIGINT,
    day            DATE,
    model          TEXT,
    llm_calls      BIGINT,
    costed_calls   BIGINT,
    unpriced_calls BIGINT,
    tokens_in      BIGINT,
    tokens_out     BIGINT,
    tokens_cached  BIGINT,
    tokens_reasoning BIGINT,
    cost_usd       DOUBLE PRECISION
);

-- One row per Loki-sourced rollup day. source_count is the event-family count
-- observed at the last successful roll; failed days remain dirty until a later
-- pass can replace the failure marker with a successful watermark.
CREATE TABLE rollup_day_state (
    day          DATE PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'rolled'
                 CHECK (status IN ('rolled', 'failed')),
    source_count BIGINT NOT NULL,
    rolled_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    error        TEXT
);

-- ─────────────── alerts (system→human alert store, Task #1224) ───────────────
-- One row = one alert instance, in the Alertmanager standard webhook payload
-- shape (status + labels + annotations + startsAt/endsAt + fingerprint +
-- generatorURL). Sources: the Grafana embedded-Alertmanager webhook contact
-- point (POST /api/alerts, source='grafana'), the cluster health probe
-- (source='health-probe'), and the heartbeat liveness pass's machine
-- offline/online edges (source='machine-probe'). Alert is fully separate
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
    -- Provenance: 'grafana' (webhook default), 'health-probe', 'machine-probe',
    -- 'permission-watcher'.
    source       TEXT NOT NULL DEFAULT 'grafana',
    notified_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT alerts_fingerprint_starts UNIQUE (fingerprint, starts_at),
    CONSTRAINT alerts_status_check CHECK (status IN ('unresolved', 'resolved')),
    CONSTRAINT alerts_severity_check CHECK (severity IN ('critical', 'warning', 'error'))
);

COMMENT ON COLUMN alerts.source IS
    'Provenance: ''grafana'' (webhook default), ''health-probe'', ''machine-probe'', ''permission-watcher''.';

CREATE INDEX alerts_status_starts_idx ON alerts (status, starts_at DESC);

-- ─────────────── event_dismissals (Loki event-class resolution, task #1468) ───────────────
-- Loki log lines are immutable, so a resolution is state about an event class,
-- never a write-back onto an historical event. NULL agent_id means every agent;
-- the first API version rejects per-agent dismissals while retaining the field
-- for the class identity's future extension.
CREATE TABLE event_dismissals (
    id           BIGSERIAL PRIMARY KEY,
    category     TEXT NOT NULL,
    level        TEXT NOT NULL,
    event_name   TEXT NOT NULL,
    source       TEXT NOT NULL,
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
    ON event_dismissals (category, level, event_name, source, agent_id) NULLS NOT DISTINCT
    WHERE status = 'dismissed';

-- ─────────────── agent_pages ───────────────
-- HTML UI server registry. ava.ui.show(name, port) registers an agent-owned
-- server, while ava.ui.serve()/serve_markdown() rows are supervised by the
-- page-server daemon in persistent sessions that outlive the agent process.
CREATE TABLE agent_pages (
    id         BIGSERIAL PRIMARY KEY,
    agent_id   BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    port       INTEGER NOT NULL CHECK (port > 0 AND port < 65536),
    host       TEXT,
    title      TEXT,
    serve_dir  TEXT,  -- set by ava.ui.serve()/serve_markdown(); NULL for ava.ui.show().
    server_token TEXT, -- durable per-page /health identity, minted by the page-server daemon.
    session_name TEXT, -- daemon-owned persistent shell; NULL for show() and pre-session rows.
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at  TIMESTAMPTZ
);

COMMENT ON COLUMN agent_pages.serve_dir IS
    'Directory the page server serves, set by ava.ui.serve()/serve_markdown(); NULL for ava.ui.show().';

CREATE UNIQUE INDEX agent_pages_unique_open
    ON agent_pages (agent_id, name)
    WHERE closed_at IS NULL;

CREATE INDEX agent_pages_per_agent_open
    ON agent_pages (agent_id, created_at)
    WHERE closed_at IS NULL;

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

-- ─────────────── events (unified event stream — FROZEN ARCHIVE) ───────────────
-- FROZEN ARCHIVE since the LGTM cutover (task #1197, 2026-08-12): the live
-- event stream ships to Loki via the unified OTLP emitter, and reads over
-- recent windows go through LogQL (gateway/loki_events.py) — this table
-- stopped receiving rows at the cutover. It serves retention/rollup and
-- deliberate historical reads only (issue #180).
--
-- The single event stream — the unified model (design §1) that audit, telemetry
-- and log signals share. Write path was the unified emitter
-- (shared/telemetry.py), the ONLY event-write entry every process used; the
-- legacy `event_log` / `agent_events` mirrors were removed with the migration
-- window (2026-08-06), and those tables are frozen (see their sections above).
--
-- `trace_id`/`span_id` are the OTel correlation keys captured from the active
-- span (turn_span) at emit time — one turn = one trace, every event in it
-- carries the same trace_id. `machine` is the required multi-host dimension,
-- bound at process start. `category` drives retention and alerting: the
-- events-maintenance daemon prunes/drops `events` month partitions by
-- per-category expiry (audit 365d / telemetry 90d / log 30d, tunable — see
-- services/events_maintenance/retention.py).
--
-- Level vocabulary is the lowercase debug/info/warning/error/critical — the
-- legacy `agent_events` table kept its UPPERCASE INFO/WARNING/ERROR; the two
-- vocabularies coexist in the baseline (see the agent_events section).
--
-- Partitioned by month exactly like agent_events: PK (id, ts), DEFAULT
-- catch-all here, month partitions (events_YYYY_MM) created at runtime by the
-- events-maintenance daemon. See
-- migrations/20260804T190839_unified-events-table.sql.
CREATE TABLE events (
    id               BIGSERIAL,
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    trace_id         TEXT,            -- OTel trace id (32 hex), from the active span
    span_id          TEXT,            -- OTel span id (16 hex), from the active span
    agent_id         BIGINT REFERENCES agents(id),  -- nullable: service-level events
    machine          TEXT NOT NULL,   -- host dimension — required
    process          TEXT NOT NULL,   -- gateway / watchdog / agent-kernel / ops / ...
    category         TEXT NOT NULL,   -- audit | telemetry | log
    event_name       TEXT NOT NULL,   -- llm_usage / turn_end / spawn / send_message / log ... (OTel event.name)
    level            TEXT NOT NULL,   -- debug | info | warning | error | critical
    source           TEXT NOT NULL,   -- agent:123 / user / system / self
    target_agent_id  BIGINT REFERENCES agents(id),  -- business events' object
    attributes       JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

COMMENT ON TABLE events IS
    'Frozen archive since the LGTM cutover (task #1197): the live event stream ships to Loki via the unified OTLP emitter; reads over recent windows go through LogQL (gateway/loki_events.py). This copy serves retention/rollup and deliberate historical reads only.';

-- Query-pattern indexes (the reads the unified model must serve):
--   (agent_id, ts DESC)   per-agent windows (timeline / stats / inspect)
--   (event_name, ts DESC) event_name aggregation (llm_usage / turn_end / exec_*)
--   (category, ts DESC)   category scans (audit views / retention slicing)
--   (trace_id)            trace correlation: a log line jumps to its call chain
--   (machine, ts DESC)    cross-machine debugging
--   partial (ts DESC)     warning/error windows, kept small
CREATE INDEX idx_events_agent_ts ON events (agent_id, ts DESC);
CREATE INDEX idx_events_event_name_ts ON events (event_name, ts DESC);
CREATE INDEX idx_events_category_ts ON events (category, ts DESC);
CREATE INDEX idx_events_trace_id ON events (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX idx_events_machine_ts ON events (machine, ts DESC);
CREATE INDEX idx_events_level_ts ON events (ts DESC) WHERE level IN ('warning', 'error', 'critical');
-- target_agent_id: FK-check support for DELETE FROM agents (RI trigger scans
-- per partition); agent_id is already covered by idx_events_agent_ts.
CREATE INDEX idx_events_target_agent_id ON events (target_agent_id);

-- DEFAULT catch-all; month partitions created at runtime by the
-- events-maintenance daemon, same contract as agent_events.
CREATE TABLE events_default PARTITION OF events DEFAULT;

-- Materialized inspector reads from the frozen events archive. This table is
-- valid only while `events` remains frozen; live history belongs in Loki.
CREATE TABLE agent_archive_stats (
    agent_id          BIGINT PRIMARY KEY REFERENCES agents(id),
    turn_distribution JSONB NOT NULL DEFAULT '[]'::jsonb,
    active_seconds    DOUBLE PRECISION NOT NULL DEFAULT 0,
    exec_seconds      DOUBLE PRECISION NOT NULL DEFAULT 0,
    lifecycle         JSONB NOT NULL DEFAULT '[]'::jsonb,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE agent_archive_stats IS
    'Materialized inspector reads from the frozen events archive. Valid only while events remains frozen; live history belongs in Loki.';
COMMENT ON COLUMN agent_archive_stats.turn_distribution IS
    'Ascending JSON pairs [duration_seconds, count] for archived turn_end events.';
COMMENT ON COLUMN agent_archive_stats.active_seconds IS
    'Archived node_exit duration sum excluding claim nodes.';
COMMENT ON COLUMN agent_archive_stats.exec_seconds IS
    'Archived node_exit duration sum for exec nodes.';
COMMENT ON COLUMN agent_archive_stats.lifecycle IS
    'Ascending JSON pairs [UTC timestamp, event name] for archived lifecycle replay.';
COMMENT ON COLUMN agent_archive_stats.computed_at IS
    'Backfill time; this materialization is valid only while the events archive is frozen.';

-- ─────────────── agent_tasks (the task registry) ───────────────
-- Persistent, process-decoupled work items agents hand off to each other.
-- owner / created_by name an agent by agents.id; created_by is TEXT because the
-- seeded root carries 'system' (and historical rows may carry 'user').
-- Ownership moves freely (a column, not derived from the spawn graph); parent_id
-- forms an arbitrary-depth subtask tree. Owner liveness is read from
-- agents_meta.status at query time, not tracked here.
CREATE TABLE agent_tasks (
    id          BIGSERIAL PRIMARY KEY,
    parent_id   BIGINT REFERENCES agent_tasks(id),      -- parent task; every task descends from the root, so NULL marks only the root itself (task_registry.create() defaults a parentless task to the root)
    title       TEXT NOT NULL,                          -- short one-line name, shown in listings; renameable via update()/PATCH, unique among open/in_progress tasks (app-level check)
    description TEXT NOT NULL,                          -- full detail of what to do; read before working
    results     TEXT,                                   -- result log (what was done, output paths); replaced by update, appended by log
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'in_progress', 'done', 'cancelled')),
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
    reminder_count      INTEGER NOT NULL DEFAULT 0  -- reminders sent for the current overdue window
);

CREATE INDEX idx_agent_tasks_owner_status   ON agent_tasks (owner, status);
CREATE INDEX idx_agent_tasks_parent         ON agent_tasks (parent_id);
CREATE INDEX idx_agent_tasks_status_created ON agent_tasks (status, created_at);

-- No two open/in_progress tasks share a title — the app-level guards in
-- task_registry.create()/update() and the gateway PATCH give the friendly
-- error; this partial unique index is the database backstop (a concurrent
-- create/rename can slip past a pre-check). A title may repeat once the
-- earlier task leaves open/in_progress.
CREATE UNIQUE INDEX agent_tasks_title_unique_open ON agent_tasks (title) WHERE status IN ('open', 'in_progress');

-- The root task: system-owned, always in_progress, default parent for tasks
-- created without an explicit parent (enforced by task_registry.create(), which
-- resolves this row as the parent when none is passed). Idempotent so
-- re-bootstrapping is a no-op.
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
-- at the LGTM cutover — task #180, migrations/20260821T023527_drop-agent-neighbors.sql.
-- ─────────────── machines ───────────────
-- Machine → inbound base URL registry for multi-machine deployment. At the tail of `ava start` on
-- each machine, UPSERT its own (name, role, url) here. gateway_url is the machine's inbound base URL
-- the rest of the cluster dials: the gateway URL, or an agent-runner's ops server URL
-- `http://<reachable-host>:<ops_port>`. Cross-machine RPC (spawn / lifecycle / status / config /
-- inventory) is a direct POST to that URL's /ops endpoint (gateway/cluster_rpc.py -> services/agent_ops).
-- description is free-text machine metadata surfaced to agents (ava.self.MACHINE / ava.agents.list_machines); the framework does not dispatch on it.
-- stopped_at marks an intentional `ava stop` (best-effort POST /api/cluster/stopping just before local
-- teardown); register_self() clears it back to NULL on `ava start`. The cluster view is a live probe, so a
-- stopped host and a crashed host both read online=False; stopped_at lets the UI tell them apart.
CREATE TABLE machines (
    name           TEXT PRIMARY KEY,
    gateway_url    TEXT,
    -- capability SET: a host carries 'gateway', 'agent-runner', or both (single box)
    role           TEXT[] NOT NULL DEFAULT '{agent-runner}'
                       CHECK (role <@ ARRAY['gateway', 'agent-runner']::text[]
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
-- only after 2 consecutive failed probes) and the true start of the current
-- failed-probe transition (NULL while reachable). Deliberately NOT a machines-table
-- column: the machines row is a recomputed composition of machine_units
-- (shared/machines.py _recompute_machine_row) and any column there would be
-- clobbered by register_self.
CREATE TABLE machine_probe (
    machine_name         TEXT PRIMARY KEY,
    online               BOOLEAN NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_probe_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    transition_since     TIMESTAMPTZ
);

-- machine_units: per-unit capability contributions that COMPOSE the machines row
-- above. One row per (machine_name, home) — `home` is the unit's $AVA_HOME. Two
-- co-located units (e.g. a gateway-only unit under ~/.ava_gateway + an
-- agent-runner-only unit under ~/.ava) share machine_name but differ by home, so
-- each UPSERTs only its own row instead of clobbering the shared machines row.
-- register_self recomputes the machines row as the union over a machine's
-- non-stopped units; every machines reader is unchanged.
CREATE TABLE machine_units (
    machine_name       TEXT NOT NULL,
    home               TEXT NOT NULL,
    serve_gateway      BOOLEAN NOT NULL DEFAULT false,
    serve_agent_runner BOOLEAN NOT NULL DEFAULT false,
    url                TEXT,
    up_since_at        TIMESTAMPTZ,
    stopped_at         TIMESTAMPTZ,
    PRIMARY KEY (machine_name, home)
);

-- (runtime_config_overrides was the API-writable config override layer; config is
-- now single-source in each unit's `.env` and the table was dropped — migration
-- 0047. The other 0010 table, plugins_config_overrides, was dropped in 0035.)

-- ─────────────── deployment_state (R1 — Task #1021) ───────────────
-- Cluster-level deployment state: phase/kind + the deploy lease + the last
-- outcome — the single authority for "is a deploy running, of what kind"
-- (replaces cluster_update_lock + session-name probing; R1 wave, Task
-- #1021). Singleton row (id=1 CHECK); `phase` is stable/updating/settling,
-- `kind` names the orchestration (rollout/restart/update), `holder`+`expires_at`
-- are the deploy lease, and `outcome` is the most recent orchestration result —
-- a RECORD, never a phase (a failure is a fact in here). shared/cluster_lock.py
-- acquires/renews/releases the lease; shared/last_update.py mirrors the last
-- outcome from cluster_last_update. Created by
-- migrations/20260808T043000_r1-deploy-state-tables.sql.
CREATE TABLE deployment_state (
    id           INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    phase        TEXT NOT NULL DEFAULT 'stable'
                 CHECK (phase IN ('stable', 'updating', 'settling')),
    kind         TEXT CHECK (kind IN ('rollout', 'restart', 'update')),
    holder       TEXT,
    acquired_at  TIMESTAMPTZ,
    expires_at   TIMESTAMPTZ,
    settle_hosts TEXT[],
    settle_note  TEXT,
    -- The settle note in its legacy single-column format ("settling, waiting
    -- for: h1, h2" — shared.cluster_lock.settle_note / settle_hosts). Written
    -- alongside settle_hosts/settle_note by the same transition and retired
    -- with the old-signal sweep; until then `note IS NOT NULL` is what keeps
    -- renew/release_settle_hold scoped to settle holds (shared/cluster_lock.py).
    note         TEXT,
    -- last_outcome: the most recent orchestration result — a RECORD, never a
    -- phase (a failure is a fact in here; the enumeration is the same six
    -- values shared/last_update.py already serves). outcome stays NULL while a
    -- rollout executes, and stays NULL if the orchestration dies: the reader
    -- derives RUNNING/ORPHANED from the deploy lease, exactly as
    -- shared/last_update.py does for cluster_last_update today.
    outcome      TEXT CHECK (outcome IN
                    ('clean', 'recovered', 'incomplete', 'aborted', 'running', 'orphaned')),
    failing_step TEXT,
    started_at   TIMESTAMPTZ,
    ended_at     TIMESTAMPTZ,
    origin       TEXT,
    target_sha   TEXT,
    observed_by  TEXT,
    log_path     TEXT,
    pin_advanced BOOLEAN NOT NULL DEFAULT FALSE
);

INSERT INTO deployment_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE deployment_state IS
    'Cluster-level deployment state: phase/kind + the deploy lease + last outcome — the single authority for "is a deploy running, of what kind" (replaces cluster_update_lock + session probing; R1 wave, Task #1021).';

-- ─────────────── host_deploy_state (R1 — Task #1021) ───────────────
-- Host-level deploy posture + updater lease, one row per machine (replaces the
-- cluster_paused file, updating.flag, session probing and updater-log-mtime
-- liveness; R1 wave, Task #1021). `posture` is idle/paused/converging;
-- `updater_lease_expires_at` is the updater process's lease. Owned by
-- shared/host_deploy_state.py. Created by
-- migrations/20260808T043000_r1-deploy-state-tables.sql.
CREATE TABLE host_deploy_state (
    machine                  TEXT PRIMARY KEY,
    posture                  TEXT NOT NULL DEFAULT 'idle'
                             CHECK (posture IN ('idle', 'paused', 'converging')),
    updater_lease_expires_at TIMESTAMPTZ,
    -- the pause-window anchor: written at the pause transition (posture ->
    -- 'paused'), preserved through 'converging', cleared at idle/unpause. The
    -- updater-outcome reader uses it to scope "which log runs belong to this
    -- pause window" (replaces the cluster_paused file mtime; Task #1021).
    paused_at                 TIMESTAMPTZ,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE host_deploy_state IS
    'Host-level deploy posture + updater lease, one row per machine (replaces the cluster_paused file, updating.flag, session probing and updater-log-mtime liveness; R1 wave, Task #1021).';

-- ─────────────── cluster_pin ───────────────
-- The cluster's pinned commit (cluster_target_sha) — the standing record of which
-- git commit the whole cluster should be on. The gateway writes it after a
-- rollout reaches its target; `ava status` compares each node's HEAD against it to
-- surface drift. The persisted form of the per-rollout target_sha (the SHA-pinned
-- rollout); first step of commit-level pinning (persist + visualize now, fail-fast
-- later). Singleton row; target_sha NULL = no rollout has pinned yet.
-- See future/infra/commit-pinned-cluster.md.
CREATE TABLE cluster_pin (
    id         INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    target_sha TEXT,
    updated_at TIMESTAMPTZ,
    updated_by TEXT,
    last_known_good_sha TEXT,
    last_known_good_at TIMESTAMPTZ,
    pending_known_good_sha TEXT,
    pending_known_good_at TIMESTAMPTZ
);
INSERT INTO cluster_pin (id, target_sha) VALUES (1, NULL);

-- ─────────────── cluster_last_update ───────────────
-- The last cluster update's outcome, as a first-class fact rather than an inference
-- from a pin/head mismatch. A failed rollout used to surface only as a yellow
-- warning on a sha mismatch, with no statement that an update had failed, when,
-- toward what, or why (the 2026-07-30 incident). Singleton, like cluster_pin: one
-- standing record, overwritten by each rollout — so a successful one CLEARS a
-- previous failure by replacing it.
--
-- outcome is NULL while a rollout executes, and STAYS NULL if its orchestration
-- dies. That is the design: the row is written ahead of the work, and a NULL
-- outcome whose holder no longer holds the deploy lease is how a reader observes
-- a death the dying process could not report. See shared/last_update.py.
CREATE TABLE cluster_last_update (
    id           INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    target_sha   TEXT,
    origin       TEXT,
    holder       TEXT,
    started_at   TIMESTAMPTZ,
    ended_at     TIMESTAMPTZ,
    outcome      TEXT,
    failing_step TEXT,
    -- An EXTERNAL observer's sentence about this attempt. The orchestration that
    -- dies files nothing, but the processes that clean up after it — today the
    -- auto-rollback, which the health probe shells into — provably witness the
    -- death. They record what they did ("rolled back to X"), which is the half
    -- that makes the surfaced failure actionable rather than merely visible.
    observed_by  TEXT,
    -- The rollout session's own log file, recorded by the intent write from the
    -- path spawn_rollout created before launching the orchestration. Named here so
    -- a surface can point at THE log instead of the rollout-<epoch>.log glob; NULL
    -- for a foreground `ava update --local`, which has no log of its own.
    log_path     TEXT,
    pin_advanced BOOLEAN NOT NULL DEFAULT FALSE
);
INSERT INTO cluster_last_update (id) VALUES (1);

-- ─────────────── cluster_defaults ───────────────
-- Cluster-level defaults a NEW agent's birth stamp reads. Singleton row; today it
-- carries exactly one value, the default model, edited through
-- GET/PUT /api/config/default-model.
--
-- Not a revival of `runtime_config_overrides` (dropped in 0047 — see the note
-- above this section's neighbours: config is single-source in each unit's `.env`).
-- Nothing reads this into `settings` and no process consults it for its own
-- behavior; it is an input to exactly one event — resolving a `lifecycle="frozen"`
-- field at agent birth (shared/birth_config.py) — whose output is written onto the
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
-- Seed matches migrations/20260731T071600_default-model-deepseek-v4-flash.sql:
-- a fresh baseline must resolve the same default as a migrated prod DB (audit
-- P3: schema-only DBs used to seed NULL here while prod carried deepseek-v4-flash).
INSERT INTO cluster_defaults (id, llm_model) VALUES (1, 'deepseek-v4-flash');

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
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON schedules (enabled);

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
    agent_id    BIGINT REFERENCES agents(id),  -- the agent a run spawned, if any (FK since 20260810T224356; NULL = no agent involved)
    note        TEXT
);
CREATE INDEX ON schedule_runs (schedule_id, ran_at DESC);

-- ─────────────── agent_watchers (R1 — Task #1021) ───────────────
-- The watcher registry — the "should it exist?" half of the design's
-- registry × lease frame for ava.watcher.at/cron/launch sessions. Written at
-- spawn; a watcher that exits CLEANLY deletes its own row; a KILLED watcher
-- (stop / rollout reap / SIGKILL) leaves the row, and the agent's boot
-- reconcile rebuilds cron watchers from the stored expression or marks
-- one-shots 'missed'. Liveness is the session itself, so there is no lease
-- column. Created by
-- migrations/20260808T104500_agent-watchers.sql.
CREATE TABLE agent_watchers (
    session_id     INTEGER NOT NULL,      -- the watcher's shell-session id (per-agent counter)
    agent_id       BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,  -- the spawning agent
    PRIMARY KEY (agent_id, session_id),
    kind           TEXT NOT NULL CHECK (kind IN ('at', 'cron', 'launch')),
    name           TEXT NOT NULL,         -- the lowercase slug
    -- rebuild source of truth — the payload each kind was spawned with:
    message        TEXT,                  -- at/cron wake message
    fires_at       TIMESTAMPTZ,           -- kind='at'
    cron_expr      TEXT,                  -- kind='cron'
    cron_timezone  TEXT,                  -- kind='cron'
    cron_end_at    TIMESTAMPTZ,           -- kind='cron' (NULL = standing)
    timeout_secs   REAL,                  -- kind='launch'
    template_version INTEGER,             -- watcher template generation at spawn (issue #1330)
    status         TEXT NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'rebuilt', 'missed')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX agent_watchers_agent_idx ON agent_watchers (agent_id);

COMMENT ON TABLE agent_watchers IS
    'Watcher registry: every ava.watcher.at/cron/launch session, keyed by its shell-session id. Written at spawn, deleted on clean exit; a killed watcher leaves its row and the agent boot reconcile rebuilds cron / marks missed one-shots (R1 wave, Task #1021).';

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
-- future/infra/extension-ownership.md (issue #39); the ownership model is
-- decisions/2026-08-21-extension-ownership-three-tiers.md.

CREATE TABLE extension_blobs (
    content_hash TEXT PRIMARY KEY,      -- shared.install_registry.tree_hash of the landed tree
    archive      BYTEA NOT NULL,        -- tar of that tree, IGNORED_NAMES excluded
    size_bytes   INTEGER NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The cap is a CONSTRAINT, not a convention. Extension content is source
    -- trees (markdown, a little Python); large artifacts are host provisioning
    -- and do not belong in the cluster's data plane. 8 MiB is far above any
    -- real package and far below "someone put a model checkpoint in Postgres".
    -- shared/extension_registry.py:MAX_BLOB_BYTES carries the same number and
    -- tests/shared/test_extension_registry.py pins the two together by writing
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

-- ─────────────── schema_migrations ───────────────
-- Applied-migration registry — maintained by `shared.migrations`. Keyed by
-- migration NAME (an applied SET, not a high-water integer). This whole file is
-- the squashed baseline, so a fresh DB stamps the single baseline sentinel row
-- below instead of replaying history. Post-baseline deltas live in
-- `migrations/YYYYMMDDTHHMMSS_*.sql`; after a successful apply the runner INSERTs
-- the new name. Keep `_BASELINE_NAME` in `shared/migrations.py` in sync with the
-- sentinel below (CI lint checks it).
CREATE TABLE schema_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed: stamp the squashed baseline (this file IS the baseline). A fresh DB is
-- "already at the baseline"; `apply_pending_migrations` then applies only the
-- post-baseline files in migrations/.
INSERT INTO schema_migrations (name) VALUES ('00000000T000000_baseline');
