"""Hosted agent runner: dispatcher, turns, corpse reaper, crash recovery and reconcile events."""

from __future__ import annotations

from typing import Literal, TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class PauseLifecycleWait(TypedDict):
    """`pause_lifecycle_wait` payload — ops/agent_pause/__init__.py::prepare.

    One row per preparation episode that met in-flight work it did not author
    (task #3591). ``waited_s`` is the bounded retry time before the outcome:
    ``resolved`` (the work finished and preparation proceeded), ``exceeded``
    (the bound was spent — abort), or ``refused`` (maintenance-authored work —
    no wait by design).
    """

    waited_s: float
    outcome: Literal["resolved", "exceeded", "refused"]
    agents: list[int]


class PauseOrphanClaimSettled(TypedDict):
    """`pause_orphan_claim_settled` payload — base/deploy/maintenance/cohort.py."""

    agent: int
    message_id: int
    age_s: float
    outcome: Literal["pending", "done"]


class HostDispatcherScanFailed(TypedDict):
    """`host_dispatcher_scan_failed` payload — agent-host durable backstop."""

    backoff_s: float


class ImpersonationEventLogIncomplete(TypedDict):
    """`impersonation_event_log_incomplete` payload — base/agents/impersonation/event_signals.py.

    One event per lease and condition on every TTL-reaper pass while the row
    fact holds (state, not edge): `seal_stuck` is an ended lease still waiting
    on an open event source (clears when it seals), `capture_failed` a lease
    with a failed source (permanent — a failed source never completes its
    lease). The agent is the event's `agent_id`."""

    condition: Literal["seal_stuck", "capture_failed"]
    lease_id: str
    lease_machine: str
    pending_reason: str
    session: str


EVENTS: dict[str, EventSpec] = {
    "impersonation_event_log_incomplete": telemetry_event(
        "impersonation_event_log_incomplete",
        "an impersonation event log cannot complete on its own (ended lease with an "
        "open source, or a failed capture source); re-emitted every reaper pass while it holds",
        payload=ImpersonationEventLogIncomplete,
        tier="anomaly",
    ),
    # Hosted runner dispatcher and turns (future/infra/agent-runner-as-server.md).
    "host_stale_running_settled": telemetry_event(
        "host_stale_running_settled",
        "hosted boot settle restored rows a previous host instance left running "
        "without a task (crash / kill -9); carries n = rows settled",
        tier="noise",
    ),
    "host_dispatcher_subscribed": telemetry_event(
        "host_dispatcher_subscribed",
        "hosted dispatcher subscribed to the inbound wake pattern",
        tier="noise",
    ),
    "host_recovery_wake_started": telemetry_event(
        "host_recovery_wake_started",
        "hosted recovery wake started a turn and occupied an in-flight pacing slot",
        tier="noise",
    ),
    "host_recovery_wake_released": telemetry_event(
        "host_recovery_wake_released",
        "hosted recovery turn completed and released its in-flight pacing slot",
        tier="noise",
    ),
    "host_dispatcher_reconnect": telemetry_event(
        "host_dispatcher_reconnect",
        "hosted dispatcher's wake subscription dropped — reconnecting (wakes published "
        "while down are lost; the delivery watchdog re-publish covers them)",
        tier="noise",
    ),
    "host_dispatcher_scan_failed": telemetry_event(
        "host_dispatcher_scan_failed",
        "hosted dispatcher's durable pending scan failed; the wake subscription remains "
        "open and attributes carry the next scan backoff_s",
        payload=HostDispatcherScanFailed,
        tier="anomaly",
    ),
    "host_dispatcher_restart_required": telemetry_event(
        "host_dispatcher_restart_required",
        "hosted dispatcher could not unwind a stale turn — exiting for supervisor recovery",
        tier="anomaly",
    ),
    "host_dispatcher_bad_channel": telemetry_event(
        "host_dispatcher_bad_channel",
        "hosted dispatcher ignored a wake whose channel name carried no agent id",
        tier="anomaly",
    ),
    "host_config_rejected": telemetry_event(
        "host_config_rejected",
        "a hosted wake was consumed without a turn because the agent's stored model "
        "config cannot build (unknown model or missing provider key) — logged once per "
        "stored config state (fingerprint); the pending inbound is kept until the "
        "overlay is fixed",
        tier="anomaly",
    ),
    "host_config_normalized": telemetry_event(
        "host_config_normalized",
        "a hosted wake bound a stored llm_model pin as its registered fallback "
        "because the registry has withdrawn the pinned model — the turn and its "
        "usage attribution run on the fallback; logged once per stored config "
        "state (fingerprint)",
        tier="anomaly",
    ),
    # Write-side counterpart to wake-time host_config_normalized (task #4306).
    "spawn_config_normalized": telemetry_event(
        "spawn_config_normalized",
        "a spawn request's config_overlay carried a withdrawn llm_model — the "
        "gateway settled it to the registered fallback before the row was "
        "created, and the response carries the receipt (requested, resolved)",
        tier="anomaly",
    ),
    "spawn_overlay_model_normalized": telemetry_event(
        "spawn_overlay_model_normalized",
        "the spawn row INSERT settled a withdrawn llm_model in the overlay to "
        "its registered fallback — the last-mile guard for client paths that "
        "compose the map outside the gateway preflight",
        tier="anomaly",
    ),
    "restart_config_normalized": telemetry_event(
        "restart_config_normalized",
        "a restart config_overlay carried a withdrawn llm_model — it was "
        "settled to the registered fallback before the overlay update and the "
        "restart payload",
        tier="anomaly",
    ),
    "host_turn_crashed": telemetry_event(
        "host_turn_crashed",
        "a hosted turn task raised — the task is dropped and the next wake retries "
        "from the checkpoint; neighbours are unaffected. Carries exception_type, plus "
        "config_fingerprint when the stored config was read before the failure",
        tier="anomaly",
    ),
    "host_agent_prepared": telemetry_event(
        "host_agent_prepared",
        "the host built an agent's per-agent runtime (chat model + startup reconcile) "
        "on a cold path — carries duration_ms and a reason of cold / config_changed / "
        "evicted, so a wake that pays the cold cost is distinguishable from one that "
        "does not, and a cache thrashing on config churn is visible as reason mix",
        tier="noise",
    ),
    "host_started": telemetry_event(
        "host_started",
        "the hosted agent-runner finished process-scope boot and its dispatcher is live",
        tier="noise",
    ),
    "host_stdout_log_rotated": telemetry_event(
        "host_stdout_log_rotated",
        "the hosted daemon rotated its raw stdout transcript at the size ceiling "
        "(task #2356) — carries size and ceiling; a crash storm shows up as repeated "
        "rotation events instead of an unbounded file",
        tier="noise",
    ),
    "host_turn_uncancellable": telemetry_event(
        "host_turn_uncancellable",
        "a hosted turn did not unwind after being cancelled — it is blocked where asyncio "
        "cannot interrupt it (a C call), so the host stopped waiting and exited. Carries the "
        "agent, how long the cancel was pending (waited_s), and the agent's real activity "
        "clock (last_active_at / idle_s from agents_meta, NOT the /api/agents field of the "
        "same name, which is MAX(inbound_messages.created_at) and goes stale during long "
        "turns — issue #183) so a slow shutdown is distinguishable from a genuine wedge. The "
        "turn resumes from its checkpoint on restart. Process mode had no equivalent because "
        "SIGKILL always lands",
        tier="anomaly",
    ),
    "host_turn_stall_timeout": telemetry_event(
        "host_turn_stall_timeout",
        "the hosted stall guard aborted a graph.ainvoke whose turn clock "
        "(base/agents/observation/turn_progress.py: node enters + completed LLM steps) was "
        "silent past AVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS (turn activity = "
        "node enter, completed LLM step, streamed chunk) — the turn-level "
        "injection guard of task #2417. The invocation was cancelled and "
        "unwound; the row settles to idling; the next wake resumes from the "
        "checkpoint",
        tier="anomaly",
    ),
    "host_turn_stall_uncancellable": telemetry_event(
        "host_turn_stall_uncancellable",
        "a stalled invocation that had been cancelled for the bounded unwind "
        "window REFUSED to unwind (blocked where asyncio cannot interrupt it "
        "— a C call). The host cannot fix this in-process: it signals a "
        "daemon restart so the supervisor recovers the turn from its "
        "checkpoint",
        tier="anomaly",
    ),
    # Corpse reaper (task #2609): mark crashed hosted rows, then terminate them.
    "host_turn_corpse_marked": telemetry_event(
        "host_turn_corpse_marked",
        "a hosted turn crashed and the row was stamped with the corpse marker "
        "(last_turn_fatal_at) — the reaper terminates it once the grace window "
        "elapses unless a completed turn clears the mark first",
        tier="anomaly",
    ),
    "corpse_stamp_failed": telemetry_event(
        "corpse_stamp_failed",
        "the corpse marker stamp failed after a hosted turn crash — the row "
        "keeps looking alive until a later stamp or a completed turn; the "
        "reaper cannot see this death",
        tier="anomaly",
    ),
    "corpse_reaper_terminated": telemetry_event(
        "corpse_reaper_terminated",
        "the corpse reaper terminated crash-marked idling rows past the grace "
        "window (termination_source='reaper')",
        tier="anomaly",
    ),
    "corpse_reaper_failed": telemetry_event(
        "corpse_reaper_failed",
        "the beat's corpse reap pass failed — retried on the next beat; leases "
        "of healthy rows are unaffected (renewal runs first)",
        tier="anomaly",
    ),
    "corpse_reaper_publish_failed": telemetry_event(
        "corpse_reaper_publish_failed",
        "a reaped corpse's frontend snapshot publish failed — best-effort; the "
        "durable terminated flip already committed",
        tier="noise",
    ),
    # Crash-recovery wake (task #4039): reap commits it; service consumes it.
    "crash_recovery_wake_queued": telemetry_event(
        "crash_recovery_wake_queued",
        "the corpse reaper committed a crash death's recovery wake — one "
        "system-source chat carrying the hosted_turn_recovery marker — inside "
        "the terminating transaction, so a committed reap always has a wake "
        "to resume its owner (task #4039)",
        tier="observation",
    ),
    "crash_recovery_wake_attempted": telemetry_event(
        "crash_recovery_wake_attempted",
        "the service layer attempted the guarded auto-resurrect for a reaped "
        "corpse's committed recovery wake; carries the status the attempt "
        "returned (task #4039)",
        tier="observation",
    ),
    "crash_recovery_wake_deferred": telemetry_event(
        "crash_recovery_wake_deferred",
        "a reaped corpse's guarded resurrect attempt failed — the wake row "
        "stays pending for the delivery watchdog's terminated-owner retry "
        "until the stale age gate (task #4039)",
        tier="observation",
    ),
    "host_recrash_reap_skipped": telemetry_event(
        "host_recrash_reap_skipped",
        "the recrash prompt reap skipped terminating a re-crashed corpse "
        "(fail-closed) — the grace-window reap stays the backstop. Carries the "
        "reason: the gray switch is off (disabled), the turn never settled to "
        "idling (settle_incomplete), or the row moved on since the crash "
        "(row_moved_on)",
        tier="noise",
    ),
    "host_turn_stall_aborted": telemetry_event(
        "host_turn_stall_aborted",
        "a hosted turn task ended after its no-progress abort: the invocation "
        "unwound and was dropped; the runtime was discarded by run_turn, so "
        "the next wake re-runs the startup reconcile before resuming from "
        "the checkpoint",
        tier="anomaly",
    ),
    # Settled-abort reconcile (task #3615): dispose claims at settlement.
    "host_abort_reconcile_skipped": telemetry_event(
        "host_abort_reconcile_skipped",
        "the settled hosted turn abort skipped the immediate inbound reconcile "
        "(fail-closed) — the claimed rows are left to the next cold admission. "
        "Carries the reason: the soft switch is off (disabled), the turn's "
        "resources never fully settled (resources_unsettled), or the runtime "
        "ownership was already replaced (ownership_lost — the replacement "
        "disposes the rows)",
        tier="noise",
    ),
    "host_abort_reconcile_failed": telemetry_event(
        "host_abort_reconcile_failed",
        "the immediate inbound reconcile at a settled hosted turn abort raised "
        "— the host does not treat it as fatal and the next cold admission "
        "retries the disposal of the claimed rows",
        tier="anomaly",
    ),
    # Finished-turn reconcile (task #3999): dispose claims after checkpoint flush.
    "host_turn_reconcile_skipped": telemetry_event(
        "host_turn_reconcile_skipped",
        "the finished hosted turn skipped the immediate inbound reconcile "
        "(fail-closed) — the claimed rows are left to the next cold admission. "
        "Carries the reason: the soft switch is off (disabled), the turn's "
        "resources never fully settled (resources_unsettled), or the runtime "
        "ownership was already replaced (ownership_lost — the replacement "
        "disposes the rows)",
        tier="noise",
    ),
    "host_turn_reconcile_failed": telemetry_event(
        "host_turn_reconcile_failed",
        "the immediate inbound reconcile at a finished hosted turn raised "
        "— the host does not treat it as fatal and the next cold admission "
        "retries the disposal of the claimed rows",
        tier="anomaly",
    ),
    # Impersonation auto-stop (task #3998): close the lease on executor/relay death.
    "impersonation_aborted": telemetry_event(
        "impersonation_aborted",
        "the native impersonation supervisor detected a dead core component "
        "(the executor's recorded process chain all dead/reused, or the bound "
        "relay's heartbeat stale past the exception window) and closed the "
        "lease: carries the agent, lease, session, the dead component "
        "(executor | relay) and its detail; the end note is delivered through "
        "the resume chain",
        tier="anomaly",
    ),
    "host_admission_wait_exceeded": telemetry_event(
        "host_admission_wait_exceeded",
        "a hosted turn has queued at the host admission gate "
        "(AVA_HOST_MAX_CONCURRENT_TURNS) for at least "
        "AVA_HOST_ADMISSION_WAIT_ALERT_SECONDS — carries the agent, its current "
        "wait, the limit and the queue depth; reported once per wait episode. "
        "Queueing is the configured memory/runtime trade-off working, not an "
        "error; a wait this long means the queue is backing up (raise the limit "
        "or inspect the turns holding slots). The wait is exempt from stall "
        "cancellation — cancelling it would only re-queue it at the tail",
        tier="anomaly",
    ),
    "hosted_boot_recovery_deferred": telemetry_event(
        "hosted_boot_recovery_deferred",
        "hosted boot recovery was deferred for an agent: retained exec request "
        "evidence is not disposable yet. Emitted once per deferred agent per "
        "boot; repeats across boots mean the evidence is not clearing on its "
        "own — inspect the named evidence and its disposition commands",
        tier="anomaly",
    ),
    "host_turn_stall_detected": telemetry_event(
        "host_turn_stall_detected",
        "the hosted dispatcher's durable scan found an in-flight turn whose "
        "turn-progress clock (base/agents/observation/turn_progress.py: node enters, completed "
        "LLM steps, streamed LLM chunks) has been silent past the wedged "
        "budget while NO pending "
        "inbound exists — the turn-level fake-alive shape (process alive, turn "
        "dead) that pending-row and pid-based detectors cannot see. The turn "
        "task is cancelled and the agent rescheduled; a turn that refuses to "
        "unwind instead escalates to a daemon restart",
        tier="anomaly",
    ),
    "claim_cas_lost": telemetry_event(
        "claim_cas_lost",
        "claim CAS race lost — another lifecycle op owns the row",
        tier="anomaly",
        retired=True,
    ),
    "claim_cas_lost_exit": telemetry_event(
        "claim_cas_lost_exit",
        "claim wait aborted by a lost CAS — process exiting cleanly",
        tier="anomaly",
        retired=True,
    ),
    "idle_cas_lost": telemetry_event(
        "idle_cas_lost",
        "idle-flip CAS race lost — degraded, not fatal",
        tier="anomaly",
        retired=True,
    ),
    "inbound_reconcile": telemetry_event(
        "inbound_reconcile", "inbound reconciliation", tier="noise"
    ),
    "inbound_reconcile_sideload_fallback": telemetry_event(
        "inbound_reconcile_sideload_fallback",
        "inbound reconcile switched from the claim window to settled history",
    ),
    # pause / rollout lifecycle
    "pause_lifecycle_wait": telemetry_event(
        "pause_lifecycle_wait",
        "preparation bounded-waited in-flight work it did not author",
        payload=PauseLifecycleWait,
        tier="anomaly",
        site="ops/agent_pause/__init__.py:_emit_lifecycle_wait (positional emit)",
    ),
    "pause_orphan_claim_settled": telemetry_event(
        "pause_orphan_claim_settled",
        "preparation settled an ordinary claim without a live runtime",
        payload=PauseOrphanClaimSettled,
        tier="anomaly",
        site="base/deploy/maintenance/cohort.py:_emit_orphan_settlements",
    ),
    # The force-termination quiet close (task #4180): an externally commanded
    # force terminate (delivery-watchdog wedge recovery / CLI force / machine
    # pause) of the turn's own incarnation stops through the classification
    # instead of the crash path — the applied command awaits its observation
    # by the pump's own boundary; no corpse marker, no error event, no
    # failure receipt.
    "host_turn_force_terminated": telemetry_event(
        "host_turn_force_terminated",
        "this hosted turn ended on its own incarnation's applied force terminate "
        "(e.g. the delivery watchdog's hosted-turn wedge recovery): the terminate "
        "command was applied but not yet observed, the turn's fail-closed guard "
        "read refused, and the pump's own boundary observes the command; not a "
        "failure",
        tier="observation",
    ),
    "host_held_wake_force_terminated": telemetry_event(
        "host_held_wake_force_terminated",
        "a held-controls wake stopped quietly because its incarnation's applied "
        "force terminate landed — the pump's boundary owns the command's "
        "observation, so the wake had nothing left to do; not a failure",
        tier="observation",
    ),
}
