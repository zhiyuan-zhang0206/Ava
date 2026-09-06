"""Runtime event declarations and registry builders."""

from typing import Any, Literal

from shared.events.payloads import (
    LLM_ERROR_FAMILY,
    AgentSpawned,
    CompactionCompleted,
    ComputerAction,
    ComputerSessionEnd,
    ComputerSessionStart,
    DeliveryPoisoned,
    DeliveryStalled,
    DeliveryWakeSuppressed,
    EventLogDrop,
    EventTier,
    ExecChildBoot,
    ExecEnvelope,
    ExecFailed,
    ExecPayload,
    ExecSubprocessKilled,
    FrontendInteraction,
    Halt,
    HeartbeatNudged,
    HeartbeatPaused,
    LlmProviderError,
    LlmUsage,
    LokiWritePathProbeFailed,
    NodeExit,
    PluginActivation,
    RetentionClass,
    SdkCall,
    ServiceStarted,
    Spawn,
    SseDrop,
    StatusChange,
    SyntaxFix,
    TaskEscalation,
    TaskReminderDigest,
    TaskUpdate,
    TurnEnd,
)
from shared.events.system import (
    AgentBootFailed,
    EventSpec,
    HostDispatcherScanFailed,
    PluginLoadFailed,
    ProcessExit,
)


def _audit(
    name: str,
    doc: str,
    *,
    payload: Any | None = None,
    tier: EventTier = "business",
    retention_class: RetentionClass | None = None,
) -> EventSpec:
    return EventSpec(
        name=name,
        category="audit",
        tier=tier,
        payload=payload,
        doc=doc,
        retention_class=retention_class,
    )


def _telemetry_audit(name: str, doc: str, *, payload: Any | None = None) -> EventSpec:
    """A name that genuinely carries both categories (status_change: the
    loguru side emits telemetry, audit_events emits audit)."""
    return EventSpec(
        name=name,
        category="telemetry",
        tier="noise",
        extra_categories=frozenset({"audit"}),
        payload=payload,
        doc=doc,
    )


def _telemetry(
    name: str,
    doc: str,
    *,
    payload: Any | None = None,
    family: str | None = None,
    destination: Literal["events", "file"] = "events",
    tier: EventTier = "observation",
    retention_class: RetentionClass | None = None,
) -> EventSpec:
    return EventSpec(
        name=name,
        category="telemetry",
        tier=tier,
        payload=payload,
        family=family,
        destination=destination,
        doc=doc,
        retention_class=retention_class,
    )


_EVENTS_RUNTIME: dict[str, EventSpec] = {
    # ── audit (category=audit, 17) — registry.md §2, append-only operations ──
    # The lineage class (retention_class="lineage"): spawn/fork/resurrect plus
    # the ops-mirror names of the same two facts (agent_spawned /
    # agent_resurrected, emitted telemetry-side). The mirrors are bundled
    # deliberately — a reader that only kept one spelling would lose half the
    # rows for the same event.
    "spawn": _audit("spawn", "new agent born", payload=Spawn, retention_class="lineage"),
    "fork": _audit("fork", "agent forked from another", retention_class="lineage"),
    "send_message": _audit("send_message", "message sent to an agent"),
    "terminate": _audit("terminate", "agent terminated"),
    "restart": _audit("restart", "agent restart initiated"),
    "cancel": _audit("cancel", "in-flight turn cancelled"),
    "resurrect": _audit("resurrect", "terminated agent woken", retention_class="lineage"),
    "restart_completed": _audit("restart_completed", "restart finished"),
    "compact": _audit("compact", "agent context compacted"),
    "circuit_breaker": _audit(
        "circuit_breaker",
        "heartbeat circuit breaker opened — a permanent provider rejection stopped "
        "heartbeat re-fires (context_overflow reason arms the forced-compact self-rescue)",
    ),
    "report_activity": _audit("report_activity", "activity report"),
    "status_change": _telemetry_audit(
        "status_change",
        "agent status transition — both telemetry (loguru) and audit (audit_events) sides emit this name",
        payload=StatusChange,
    ),
    "exit": _audit("exit", "agent process exited"),
    "label_change": _audit("label_change", "agent label changed"),
    "skill_invoked": _audit("skill_invoked", "skill invoked by an agent"),
    "task_create": _audit("task_create", "task created"),
    "task_update": _audit("task_update", "task updated", payload=TaskUpdate),
    "report_breached": _audit("report_breached", "guarantee report breached"),
    "computer_action": _audit(
        "computer_action",
        "computer-use desktop action (executed or refused)",
        payload=ComputerAction,
    ),
    "env_write": _audit(
        "env_write",
        "official .env config write (actor and keys; values never recorded)",
    ),
    "env_unauthorized_write": _audit(
        "env_unauthorized_write",
        "out-of-band .env modification detected (no official write recorded)",
        tier="anomaly",
    ),
    "computer_session_start": _audit(
        "computer_session_start",
        "computer-use task session opened (first action with a task_id)",
        payload=ComputerSessionStart,
    ),
    "computer_session_end": _audit(
        "computer_session_end",
        "computer-use task session closed (idle timeout)",
        payload=ComputerSessionEnd,
    ),
    "mcp_tool_call": _audit(
        "mcp_tool_call",
        "MCP tool invoked through the gateway /mcp endpoint (client-scoped, args redacted)",
    ),
    # ── telemetry (category=telemetry) — registry.md §3 ──
    # frontend user modeling
    "frontend_interaction": _telemetry(
        "frontend_interaction",
        "tracked frontend interaction (click / page view / settings change)",
        payload=FrontendInteraction,
        tier="noise",
    ),
    # turn lifecycle
    "llm_usage": _telemetry("llm_usage", "LLM call metering", payload=LlmUsage),
    "turn_end": _telemetry("turn_end", "one turn finished", payload=TurnEnd),
    "llm_turn_aborted": _telemetry(
        "llm_turn_aborted", "turn aborted after retries", family=LLM_ERROR_FAMILY, tier="anomaly"
    ),
    "compact_turn_aborted": _telemetry(
        "compact_turn_aborted", "turn aborted because compaction failed", tier="anomaly"
    ),
    "llm_provider_error": _telemetry(
        "llm_provider_error",
        "LLM provider failure",
        payload=LlmProviderError,
        family=LLM_ERROR_FAMILY,
        tier="anomaly",
    ),
    "stream_stalled_retry": _telemetry(
        "stream_stalled_retry", "stream stalled, retried", family=LLM_ERROR_FAMILY, tier="anomaly"
    ),
    "stream_overloaded_retry": _telemetry(
        "stream_overloaded_retry",
        "stream overloaded, retried",
        family=LLM_ERROR_FAMILY,
        tier="anomaly",
    ),
    "thinking_block_sanitized": _telemetry(
        "thinking_block_sanitized", "thinking block sanitized", tier="noise"
    ),
    "multiple_tool_calls_merged": _telemetry(
        "multiple_tool_calls_merged", "concurrent tool calls merged"
    ),
    "llm_cancelled": _telemetry("llm_cancelled", "LLM call cancelled", tier="anomaly"),
    # exec lifecycle
    "exec": _telemetry("exec", "execute_code succeeded", payload=ExecPayload),
    "exec_failed": _telemetry(
        "exec_failed", "execute_code failed", payload=ExecFailed, tier="anomaly"
    ),
    "plugin_load_failed": _telemetry(
        "plugin_load_failed",
        "enabled plugin skipped because it failed to load (fail-soft)",
        payload=PluginLoadFailed,
        tier="anomaly",
    ),
    "exec_envelope": _telemetry(
        "exec_envelope",
        "exec envelope transfer cost (size + serialize time) — request snapshot / result delta",
        payload=ExecEnvelope,
    ),
    "exec_child_boot": _telemetry(
        "exec_child_boot",
        "exec child bootstrap duration before agent-authored code",
        payload=ExecChildBoot,
        tier="noise",
    ),
    "compaction_completed": _telemetry(
        "compaction_completed",
        "applied context compaction size reduction and completed count",
        payload=CompactionCompleted,
        tier="noise",
    ),
    "exec_cancelled": _telemetry("exec_cancelled", "execute_code cancelled", tier="anomaly"),
    "exec(timeout)": _telemetry(
        "exec(timeout)", "historical parenthesized name (migration target)", tier="anomaly"
    ),
    "exec(failed)": _telemetry(
        "exec(failed)", "historical parenthesized name (migration target)", tier="anomaly"
    ),
    "exec(cancelled)": _telemetry(
        "exec(cancelled)", "historical parenthesized name (migration target)", tier="anomaly"
    ),
    "exec(thread-stuck)": _telemetry(
        "exec(thread-stuck)", "historical parenthesized name (migration target)", tier="anomaly"
    ),
    "exec_timeout": _telemetry("exec_timeout", "execute_code timed out", tier="anomaly"),
    "exec_node_timeout": _telemetry("exec_node_timeout", "node-level timeout", tier="anomaly"),
    "exec_subprocess_killed": _telemetry(
        "exec_subprocess_killed",
        "exec child survived the signal grace period and was SIGKILLed",
        payload=ExecSubprocessKilled,
        tier="anomaly",
    ),
    # hosted runner (future/infra/agent-runner-as-server.md) — the dispatcher
    # that turns an inbound wake into a turn task, and the turn tasks it runs
    "host_stale_running_settled": _telemetry(
        "host_stale_running_settled",
        "hosted boot settle restored rows a previous host instance left running "
        "without a task (crash / kill -9); carries n = rows settled",
        tier="noise",
    ),
    "host_dispatcher_subscribed": _telemetry(
        "host_dispatcher_subscribed",
        "hosted dispatcher subscribed to the inbound wake pattern",
        tier="noise",
    ),
    "host_dispatcher_reconnect": _telemetry(
        "host_dispatcher_reconnect",
        "hosted dispatcher's wake subscription dropped — reconnecting (wakes published "
        "while down are lost; the delivery watchdog re-publish covers them)",
        tier="noise",
    ),
    "host_dispatcher_scan_failed": _telemetry(
        "host_dispatcher_scan_failed",
        "hosted dispatcher's durable pending scan failed; the wake subscription remains "
        "open and attributes carry the next scan backoff_s",
        payload=HostDispatcherScanFailed,
        tier="anomaly",
    ),
    "host_dispatcher_restart_required": _telemetry(
        "host_dispatcher_restart_required",
        "hosted dispatcher could not unwind a stale turn — exiting for supervisor recovery",
        tier="anomaly",
    ),
    "host_dispatcher_bad_channel": _telemetry(
        "host_dispatcher_bad_channel",
        "hosted dispatcher ignored a wake whose channel name carried no agent id",
        tier="anomaly",
    ),
    "host_config_rejected": _telemetry(
        "host_config_rejected",
        "a hosted wake was consumed without a turn because the agent's stored model "
        "config cannot build (unknown model or missing provider key) — logged once per "
        "stored config state (fingerprint); the pending inbound is kept until the "
        "overlay is fixed",
        tier="anomaly",
    ),
    "host_turn_crashed": _telemetry(
        "host_turn_crashed",
        "a hosted turn task raised — the task is dropped and the next wake retries "
        "from the checkpoint; neighbours are unaffected. Carries exception_type, plus "
        "config_fingerprint when the stored config was read before the failure",
        tier="anomaly",
    ),
    "host_agent_prepared": _telemetry(
        "host_agent_prepared",
        "the host built an agent's per-agent runtime (chat model + startup reconcile) "
        "on a cold path — carries duration_ms and a reason of cold / config_changed / "
        "evicted, so a wake that pays the cold cost is distinguishable from one that "
        "does not, and a cache thrashing on config churn is visible as reason mix",
        tier="noise",
    ),
    "host_started": _telemetry(
        "host_started",
        "the hosted agent-runner finished process-scope boot and its dispatcher is live",
        tier="noise",
    ),
    "host_stdout_log_rotated": _telemetry(
        "host_stdout_log_rotated",
        "the hosted daemon rotated its raw stdout transcript at the size ceiling "
        "(task #2356) — carries size and ceiling; a crash storm shows up as repeated "
        "rotation events instead of an unbounded file",
        tier="noise",
    ),
    "host_turn_uncancellable": _telemetry(
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
    "host_turn_stall_timeout": _telemetry(
        "host_turn_stall_timeout",
        "the hosted stall guard aborted a graph.ainvoke whose turn clock "
        "(agent/_turn_progress.py: node enters + completed LLM steps) was "
        "silent past AVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS (turn activity = "
        "node enter, completed LLM step, streamed chunk) — the turn-level "
        "injection guard of task #2417. The invocation was cancelled and "
        "unwound; the row settles to idling; the next wake resumes from the "
        "checkpoint",
        tier="anomaly",
    ),
    "host_turn_stall_uncancellable": _telemetry(
        "host_turn_stall_uncancellable",
        "a stalled invocation that had been cancelled for the bounded unwind "
        "window REFUSED to unwind (blocked where asyncio cannot interrupt it "
        "— a C call). The host cannot fix this in-process: it signals a "
        "daemon restart so the supervisor recovers the turn from its "
        "checkpoint",
        tier="anomaly",
    ),
    "host_turn_stall_aborted": _telemetry(
        "host_turn_stall_aborted",
        "a hosted turn task ended after its no-progress abort: the invocation "
        "unwound and was dropped; the runtime was discarded by run_turn, so "
        "the next wake re-runs the startup reconcile before resuming from "
        "the checkpoint",
        tier="anomaly",
    ),
    "host_turn_stall_detected": _telemetry(
        "host_turn_stall_detected",
        "the hosted dispatcher's durable scan found an in-flight turn whose "
        "turn-progress clock (agent/_turn_progress.py: node enters, completed "
        "LLM steps, streamed LLM chunks) has been silent past the wedged "
        "budget while NO pending "
        "inbound exists — the turn-level fake-alive shape (process alive, turn "
        "dead) that pending-row and pid-based detectors cannot see. The turn "
        "task is cancelled and the agent rescheduled; a turn that refuses to "
        "unwind instead escalates to a daemon restart",
        tier="anomaly",
    ),
    # node / process lifecycle
    "node_enter": _telemetry(
        "node_enter",
        "LangGraph node entered — sink-filtered out of the events table (PR #1758); log files only",
        destination="file",
        tier="noise",
    ),
    "node_exit": _telemetry("node_exit", "LangGraph node exited", payload=NodeExit, tier="noise"),
    "process_exit": _telemetry(
        "process_exit", "agent process exited", payload=ProcessExit, tier="noise"
    ),
    "service_started": _telemetry(
        "service_started", "gateway/daemon started", payload=ServiceStarted, tier="noise"
    ),
    "halt": _telemetry("halt", "turn stopped (idle/compact/system)", payload=Halt, tier="noise"),
    "agent_restarted": _telemetry("agent_restarted", "agent restarted (phase2 done)"),
    "restart_handoff_host_unhealthy": _telemetry(
        "restart_handoff_host_unhealthy",
        "hosted restart ownership could not transfer: agent-host is unhealthy; row left restarting "
        "for retry",
        tier="anomaly",
    ),
    "heartbeat_nudged": _telemetry(
        "heartbeat_nudged", "heartbeat reminder", payload=HeartbeatNudged, tier="noise"
    ),
    "task_reminder_digest": _telemetry(
        "task_reminder_digest",
        "overdue-task owner digest",
        payload=TaskReminderDigest,
        tier="noise",
    ),
    "task_escalation": _telemetry(
        "task_escalation", "stalled-task escalation", payload=TaskEscalation
    ),
    "delivery_stalled": _telemetry(
        "delivery_stalled", "delivery backlog", payload=DeliveryStalled, tier="anomaly"
    ),
    "loki_write_path_probe_failed": _telemetry(
        "loki_write_path_probe_failed",
        "Loki write-path probe failed",
        payload=LokiWritePathProbeFailed,
        tier="anomaly",
    ),
    "delivery_poisoned": _telemetry(
        "delivery_poisoned",
        "delivery backlog — permanently-failing inbound poisoned (dispatch cap reached)",
        payload=DeliveryPoisoned,
        tier="anomaly",
    ),
    "delivery_wake_suppressed": _telemetry(
        "delivery_wake_suppressed",
        "automatic delivery wakes suppressed after repeated resurrection failures",
        payload=DeliveryWakeSuppressed,
        tier="anomaly",
    ),
    "claim_cas_lost": _telemetry(
        "claim_cas_lost", "claim CAS race lost — another lifecycle op owns the row", tier="anomaly"
    ),
    "claim_cas_lost_exit": _telemetry(
        "claim_cas_lost_exit",
        "claim wait aborted by a lost CAS — process exiting cleanly",
        tier="anomaly",
    ),
    "idle_cas_lost": _telemetry(
        "idle_cas_lost", "idle-flip CAS race lost — degraded, not fatal", tier="anomaly"
    ),
    "boot_timing": _telemetry("boot_timing", "boot duration", tier="noise"),
    "dangling_tool_pairing_repaired": _telemetry(
        "dangling_tool_pairing_repaired", "dangling tool pairing repaired", tier="anomaly"
    ),
    "agent_spawned": _telemetry(
        "agent_spawned",
        "agent process started",
        payload=AgentSpawned,
        retention_class="lineage",
    ),
    "agent_resurrected": _telemetry(
        "agent_resurrected", "agent resurrected", retention_class="lineage"
    ),
    "agent_terminated": _telemetry("agent_terminated", "agent terminated"),
    "agent_revived": _telemetry("agent_revived", "agent revived", tier="noise"),
    "respawn_phase1": _telemetry("respawn_phase1", "restart phase 1", tier="noise"),
    "respawn_phase2_launch": _telemetry(
        "respawn_phase2_launch", "restart phase 2 launch", tier="noise"
    ),
    "launch_confirm_extended": _telemetry(
        "launch_confirm_extended", "launch confirm extended", tier="noise"
    ),
    "launch_confirm_failed": _telemetry(
        "launch_confirm_failed", "launch confirm failed", tier="anomaly"
    ),
    "agent_boot_failed": _telemetry(
        "agent_boot_failed",
        "agent boot failed (process exits; crash-loop budget applies)",
        payload=AgentBootFailed,
        tier="anomaly",
    ),
    "launch_confirm_task_crashed": _telemetry(
        "launch_confirm_task_crashed", "launch confirm task crashed", tier="anomaly"
    ),
    "launch_force_terminated": _telemetry(
        "launch_force_terminated", "launch force-terminated", tier="anomaly"
    ),
    "launch_force_terminated_skipped": _telemetry(
        "launch_force_terminated_skipped", "launch force-terminate skipped", tier="noise"
    ),
    "launch_retry": _telemetry("launch_retry", "launch retried"),
    # sdk / channel health
    "sdk_call": _telemetry("sdk_call", "SDK call metering", payload=SdkCall, tier="noise"),
    "plugin_activation": _telemetry(
        "plugin_activation",
        "a plugin injection surface fired (hook / wrap / prompt section)",
        payload=PluginActivation,
        tier="noise",
    ),
    "sse_drop": _telemetry("sse_drop", "SSE event dropped", payload=SseDrop, tier="anomaly"),
    "event_log_drop": _telemetry(
        "event_log_drop", "event-pipeline row shed", payload=EventLogDrop, tier="anomaly"
    ),
    "heartbeat_paused": _telemetry("heartbeat_paused", "heartbeat paused", payload=HeartbeatPaused),
    "code": _telemetry("code", "LLM generated code block", payload=ExecPayload, tier="noise"),
    # label-fallback events kept in the registry
    "text": _telemetry("text", "LLM text output", tier="noise"),
    "syntax_fix": _telemetry(
        "syntax_fix", "syntax repair executed", payload=SyntaxFix, tier="noise"
    ),
    "inbound_reconcile": _telemetry("inbound_reconcile", "inbound reconciliation", tier="noise"),
    "screen_capture_notify_failed": _telemetry(
        "screen_capture_notify_failed", "screenshot notify failed", tier="anomaly"
    ),
    # ava.ui.serve page-restore
    "page_restore_alive": _telemetry("page_restore_alive", "page restore alive", tier="noise"),
    "page_restore_reserved": _telemetry(
        "page_restore_reserved", "page restore reserved", tier="noise"
    ),
    "page_restore_query_failed": _telemetry(
        "page_restore_query_failed", "page restore query failed", tier="anomaly"
    ),
    "page_restore_failed": _telemetry("page_restore_failed", "page restore failed", tier="anomaly"),
    "page_restore_closed": _telemetry("page_restore_closed", "page restore closed", tier="noise"),
    "page_restore_notified": _telemetry(
        "page_restore_notified", "page restore notified", tier="noise"
    ),
}
