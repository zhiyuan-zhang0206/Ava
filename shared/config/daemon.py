"""Daemon config — DaemonSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import NoDecode

from shared.config._base import EnvSettings


class DaemonSettings(EnvSettings):
    runner_mode: Literal["process", "hosted"] = Field(
        default="hosted",
        alias="AVA_RUNNER_MODE",
        description="How the agent-runner hosts agents. `hosted` (the default since 2026-09, user ruling) = the runner daemon hosts every local agent's turns as asyncio tasks in its own process, and an idle agent is no task at all; it is what puts the `agent-host` service on this host's start roster. `process` = one OS process per agent, alive from spawn to terminate — the legacy model, kept as an explicit opt-out for rollback. Cluster-pinned because the model must be uniform: a cluster running both would need double bookkeeping for agent leases, since a hosted agent's liveness is an in-process fact while a process agent's is a lease row. Rollback is a restart with this flipped back — no schema shape changes with it.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    host_max_concurrent_turns: int = Field(
        default=16,
        alias="AVA_HOST_MAX_CONCURRENT_TURNS",
        description="Hosted agent-runner: how many agents may have a turn RUNNING at once. Wakes beyond this queue rather than run, so the host's shared Postgres pool can be sized as a statement about this bound (bound + headroom) instead of a hope. An agent waiting on the bound still holds no task-level cost beyond the coroutine itself; the whole point of the hosted model is that an idle agent is no task at all.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    host_agent_cache_size: int = Field(
        default=32,
        alias="AVA_HOST_AGENT_CACHE_SIZE",
        description="Hosted agent-runner: how many agents' prepared runtimes (chat model + the boot reconcile already done for them) the host keeps warm, evicted least-recently-used. A cold entry costs one model build plus this agent's startup reconcile on its next wake; an unbounded cache would let a fleet-wide wake burst hold one per local agent forever.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    host_agent_idle_ttl_seconds: float = Field(
        default=900.0,
        alias="AVA_HOST_AGENT_IDLE_TTL_SECONDS",
        description="Hosted agent-runner: how long a prepared agent runtime survives with no turn before the host drops it. The size cap alone would keep a long-silent agent warm forever on a lightly loaded runner; this is the other half, so a runner that goes quiet returns to holding nothing.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    host_turn_no_progress_timeout_seconds: float = Field(
        default=2400.0,
        alias="AVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS",
        description="Hosted agent-runner: a graph invocation whose per-agent turn clock has shown no activity (a LangGraph node enter, a completed LLM step) for this long is treated as turn-level fake-alive and aborted: the invocation is cancelled with the bounded unwind, the row settles to idling, one Error event is emitted, and the next wake resumes from the checkpoint. The default covers exec_node_timeout (1200s) plus the LLM retry budget plus margin, matching AVA_WEDGED_AGENT_INBOUND_AGE_SECONDS. A days-long turn that keeps stepping is never aborted — only no-progress counts.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    host_turn_progress_scan_interval_seconds: float = Field(
        default=30.0,
        alias="AVA_HOST_TURN_PROGRESS_SCAN_INTERVAL_SECONDS",
        description="Hosted agent-runner: how often the no-progress stall guard polls the per-agent turn clock while a graph.ainvoke is running. Poll cadence only — it changes detection latency, never the abort threshold (AVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS).",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    restarter_poll_interval_seconds: float = Field(
        default=1.0,
        alias="AVA_RESTARTER_POLL_INTERVAL_SECONDS",
        description="Restarter daemon main-loop poll interval (seconds) for agents.status='restarting' rows. Shorter is lower latency but slightly higher DB load.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    page_server_poll_interval_seconds: float = Field(
        default=2.0,
        alias="AVA_PAGE_SERVER_POLL_INTERVAL_SECONDS",
        description="Page server supervisor daemon poll interval (seconds): how often it reconciles open agent_pages rows against live page sessions.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    page_default_ttl_seconds: float = Field(
        default=86400.0,
        alias="AVA_PAGE_DEFAULT_TTL_SECONDS",
        description="Default lifetime in seconds for agent-published pages when the SDK does not specify ttl. The gateway applies this policy when it registers the page.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    ttl_reaper_poll_interval_seconds: float = Field(
        default=60.0,
        alias="AVA_TTL_REAPER_POLL_INTERVAL_SECONDS",
        description="Gateway TTL reaper poll interval in seconds for expired pages and explicitly time-limited persistent shell sessions.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    health_probe_agent_min: int = Field(
        default=1,
        alias="AVA_HEALTH_PROBE_AGENT_MIN",
        description="Minimum running/idling agents the health-probe requires for a healthy verdict. A test/QA cluster with no resident agents must set this to 0, or the probe's agent-population check fails forever and --auto-rollback rolls the cluster back to the last-known-good commit on a cycle (2026-08-10 preview incident).",
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_max_resurrect_per_tick: int = Field(
        default=3,
        alias="AVA_DELIVERY_WATCHDOG_MAX_RESURRECT_PER_TICK",
        description="Ceiling on how many terminated-owner resurrect retries the delivery watchdog spawns per tick (Task #689 G4). A pile of dead letters drains over ticks; the cap plus the 60s per-agent cooldown and 2-way concurrency semaphore prevent an LLM wake storm when many terminated agents hold pending chats.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_resurrect_fail_before_suppress: int = Field(
        default=5,
        ge=1,
        alias="AVA_DELIVERY_WATCHDOG_RESURRECT_FAIL_BEFORE_SUPPRESS",
        description="Consecutive failed terminated-owner auto-resurrect attempts before the delivery watchdog suppresses that agent's automatic wakes.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_suppress_base_seconds: float = Field(
        default=1800.0,
        gt=0,
        alias="AVA_DELIVERY_WATCHDOG_SUPPRESS_BASE_SECONDS",
        description="Initial automatic-wake suppression window after repeated terminated-owner resurrection failures; each later suppression for that agent doubles this duration.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_suppress_max_seconds: float = Field(
        default=86400.0,
        gt=0,
        alias="AVA_DELIVERY_WATCHDOG_SUPPRESS_MAX_SECONDS",
        description="Maximum automatic-wake suppression window after repeated terminated-owner resurrection failures.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    revive_max_per_pass: int = Field(
        default=50,
        alias="AVA_REVIVE_MAX_PER_PASS",
        description="Ceiling on how many dead-pid 'running'/'idling' rows the restarter revives per reap pass (Task #689 G5: a rebooted machine's agents are relaunched automatically instead of being reaped to 'terminated'). A backlog beyond the cap drains over successive passes (30s cadence); the cap is the anti-storm guard so a mass-death event (e.g. one host's whole fleet) cannot launch 300 processes in one tick.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    boot_reap_grace_seconds: float = Field(
        default=120.0,
        alias="AVA_ALLOCATED_REAP_GRACE_SECONDS",
        description="Grace (seconds) before the restarter reaps an unclaimed idling row to 'terminated' — a process that died before claiming or was never launched. The `AVA_ALLOCATED_REAP_GRACE_SECONDS` alias is retained for existing deployment configuration. Must exceed boot plus the launch-confirm window (launch_confirm_timeout_seconds, 45s), pinned by tests/shared/test_timing_topology.py. It is also the ceiling on the launch-confirm's one extension for a still-live child, so that wait never outlives the point where this reaper takes the row.",
        json_schema_extra={
            # The gateway's durable wake selector and runner admission must
            # share this deadline; gateway profile construction must retain it.
            "capability": "common",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    heartbeat_enabled: bool = Field(
        default=True,
        alias="AVA_HEARTBEAT_ENABLED",
        description="Run the heartbeat daemon on the gateway. On by default; set false to disable idle-agent check-ins cluster-wide.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    heartbeat_interval_seconds: float = Field(
        default=300.0,
        alias="AVA_HEARTBEAT_INTERVAL_SECONDS",
        description="Heartbeat daemon poll interval (seconds): how often it scans idle agents and sends a check-in to those that haven't paused. Longer = less disturbance, slower stall detection.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    heartbeat_idle_threshold_seconds: float = Field(
        default=300.0,
        alias="AVA_HEARTBEAT_IDLE_THRESHOLD_SECONDS",
        description="Minimum idle time (seconds) since an agent's last completed turn before the heartbeat checks in on it. Measured from last activity, not status change, so an ops restart never resets the timer.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    heartbeat_backoff_consecutive_noop_nudges: int = Field(
        default=3,
        alias="AVA_HEARTBEAT_BACKOFF_CONSECUTIVE_NOOP_NUDGES",
        description="Platform-side nudge backoff (B7): consecutive heartbeat nudges that produce no real inbound and no agent pause raise the agent's backoff level, stretching the reminder interval by 2^level (cap 24h). Real inbound or a pause resets it.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    delivery_watchdog_enabled: bool = Field(
        default=True,
        alias="AVA_DELIVERY_WATCHDOG_ENABLED",
        description="Run the delivery watchdog daemon on the gateway. On by default; set false to disable stale-pending-inbound alerting cluster-wide.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    delivery_watchdog_interval_seconds: float = Field(
        default=0.5,
        alias="AVA_DELIVERY_WATCHDOG_INTERVAL_SECONDS",
        description="Delivery watchdog tick (seconds): how often it re-dispatches lost wakes and scans for stalled inbounds. Constant load (~2 qps), independent of fleet size. Lower = faster wake recovery after a lost publish.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_dispatch_threshold_seconds: float = Field(
        default=1.0,
        alias="AVA_DELIVERY_WATCHDOG_DISPATCH_THRESHOLD_SECONDS",
        description="Wake re-dispatch threshold (seconds): a pending inbound of an idling owner older than this gets its Redis wake re-published (plus the wake-key breadcrumb). Must stay below the claim loop's 30s SELECT recheck — it is the fast fallback for lost pub/sub wakes.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_max_dispatch_count: int = Field(
        default=5,
        ge=1,
        alias="AVA_DELIVERY_WATCHDOG_MAX_DISPATCH_COUNT",
        description="Maximum successful delivery-watchdog wake re-dispatches for one pending inbound before the watchdog poisons the row and stops re-publishing. Poison does not prevent the agent from claiming the inbound after recovery.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_dispatch_backoff_steps_s: Annotated[list[float], NoDecode] = Field(
        default=[5.0, 30.0, 120.0, 300.0],
        alias="AVA_DELIVERY_WATCHDOG_DISPATCH_BACKOFF_STEPS_S",
        description="Minimum seconds between successive delivery-watchdog wake re-dispatches, indexed by the row's current dispatch count (the first re-dispatch waits steps[0]). The last step repeats when the dispatch cap is longer than this list.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_threshold_seconds: float = Field(
        default=30.0,
        alias="AVA_DELIVERY_WATCHDOG_THRESHOLD_SECONDS",
        description="Delivery watchdog stall alert threshold (seconds): a chat inbound still pending longer than this whose owner is in a waiting/terminal state (idling/terminated) is reported as a stalled delivery.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_stale_claimed_threshold_seconds: float = Field(
        default=86400.0,
        alias="AVA_DELIVERY_WATCHDOG_STALE_CLAIMED_THRESHOLD_SECONDS",
        description="Stale-claimed dead-letter threshold (seconds): a 'claimed' chat inbound whose owner is terminated and whose claim is older than this (claimed_at, falling back to created_at for pre-column rows) is flipped to 'done' instead of being re-delivered if the agent is ever resurrected. Terminated agents leave claimed rows behind (reconcile runs only at boot); a resurrect would otherwise flip them all to 'pending' and re-deliver ancient messages (Task #654).",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_stale_claimed_idling_threshold_seconds: float = Field(
        default=7200.0,
        alias="AVA_DELIVERY_WATCHDOG_STALE_CLAIMED_IDLING_THRESHOLD_SECONDS",
        description="Stale-claimed dead-letter threshold for IDLING owners (seconds): a 'claimed' chat inbound whose owner is idling and whose claim is older than this is flipped to 'done'. Hosted agents stay idling without booting, so their claims never hit the reconcile path; running owners are never swept.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    auto_resurrect_enabled: bool = Field(
        default=True,
        alias="AVA_AUTO_RESURRECT_ENABLED",
        description="Run the crash auto-resurrect controller (agent-runner), which brings back agents that died involuntarily while a pending inbound waits. Off does not strand anything — a new inbound still resurrects a terminated agent; this controller only closes the gap where no new message arrives after the crash.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    auto_resurrect_backoff_seconds: float = Field(
        default=300.0,
        alias="AVA_AUTO_RESURRECT_BACKOFF_SECONDS",
        description="Per-agent backoff (seconds) for crash auto-resurrect: after resurrecting a crashed agent, it won't resurrect the same agent again until this passes (loud WARN each retry). Caps a reliably-recrashing agent at one attempt per window while a transient outage still self-heals. Default 300s.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    auto_resurrect_max_attempts: int = Field(
        default=3,
        alias="AVA_AUTO_RESURRECT_MAX_ATTEMPTS",
        description="Maximum unconsumed kind='resurrect' lifecycle inbounds before system-initiated recovery from the crash/wedged controllers, delivery path, or delivery watchdog stops auto-resurrecting an agent; a successful boot consumes them, so this bounds consecutive failed recovery attempts while manual resurrect remains exempt.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    wedged_agent_enabled: bool = Field(
        default=True,
        alias="AVA_WEDGED_AGENT_ENABLED",
        description="Run the wedged-agent controller (agent-runner), which detects a running/idling agent with a live pid but a stale unconsumed pending inbound, then force-kills and resurrects it so it processes its backlog. It also identity-reaps a user-terminated row whose old process retains a live lease and pending terminate inbound, without resurrection. Off does not strand anything — a new inbound still wakes the agent; this controller only closes the gap where the existing pending inbound is ignored.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    wedged_agent_inbound_age_seconds: float = Field(
        default=2400.0,
        alias="AVA_WEDGED_AGENT_INBOUND_AGE_SECONDS",
        description="Minimum age (seconds) of an unconsumed pending inbound or no-progress running turn before a running agent is considered wedged; the turn check uses the status_changed_at and last_active_at window. Default 2400s (40 min) — exec_node_timeout_seconds (1200s) + LLM retry budget plus margin. Raise for agents doing long-running work; lower for tighter detection.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    wedged_idling_agent_inbound_age_seconds: float = Field(
        default=180.0,
        alias="AVA_WEDGED_IDLING_AGENT_INBOUND_AGE_SECONDS",
        description="Minimum age (seconds) of an unconsumed pending inbound before an idling agent is considered wedged. Default 180s allows several 30-second claim-loop fallback checks plus recovery margin; it deliberately does not include running-turn exec or LLM budgets.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    task_maintenance_enabled: bool = Field(
        default=True,
        alias="AVA_TASK_MAINTENANCE_ENABLED",
        description="Run the task-maintenance daemon on the gateway. On by default; set false to disable task reminders and the escalation pass cluster-wide.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    task_maintenance_interval_seconds: float = Field(
        default=300.0,
        alias="AVA_TASK_MAINTENANCE_INTERVAL_SECONDS",
        description="Task-maintenance daemon poll interval (seconds): how often it checks for overdue tasks and reminds owners. A precision lower bound, not the cadence — each task controls its own remind_interval_seconds.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    task_reminder_backoff_seconds: float = Field(
        default=3600.0,
        alias="AVA_TASK_REMINDER_BACKOFF_SECONDS",
        description="Floor for the interval (seconds) between repeated reminders for the same overdue window: a task whose remind_interval_seconds exceeds this repeats at its own interval instead.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    task_escalate_n: int = Field(
        default=3,
        alias="AVA_TASK_ESCALATE_N",
        description="Number of unanswered reminders before the daemon escalates to the parent task's owner.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_maintenance_interval_seconds: float = Field(
        default=3600.0,
        alias="AVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS",
        description="Events-maintenance daemon poll interval (seconds): how often it probes the retained rollup watermark and re-aggregates dirty days. Hourly keeps the durable ledger fresh and recovers a downtime gap within the hour.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_maintenance_pass_deadline_s: float = Field(
        default=1500.0,
        alias="AVA_EVENTS_MAINTENANCE_PASS_DEADLINE_S",
        description="Hard deadline in seconds for one hourly events-maintenance pass. Its longest slice, the Loki rollup, is bounded by its own pass deadline; exceeding this bound wedges the loop for watchdog respawn.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_rollup_pass_deadline_s: float = Field(
        default=1200.0,
        alias="AVA_EVENTS_ROLLUP_PASS_DEADLINE_S",
        description="Wall-clock budget in seconds for one Loki-to-Postgres rollup pass. The daemon stops between day probes or full recomputes when the budget is exhausted, leaving untouched days dirty for the next pass.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_maintenance_trim_deadline_s: float = Field(
        default=300.0,
        alias="AVA_EVENTS_MAINTENANCE_TRIM_DEADLINE_S",
        description="Hard deadline in seconds for one events-maintenance checkpoint-trim pass; exceeding it wedges the loop for watchdog respawn.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_maintenance_resolution_deadline_s: float = Field(
        default=600.0,
        alias="AVA_EVENTS_MAINTENANCE_RESOLUTION_DEADLINE_S",
        description="Hard deadline in seconds for one events-maintenance class-resolution pass; exceeding it wedges the loop for watchdog respawn.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_rollup_late_write_lookback_days: int = Field(
        default=1,
        ge=1,
        alias="AVA_EVENTS_ROLLUP_LATE_WRITE_LOOKBACK_DAYS",
        description="Number of most-recent closed UTC days that the Loki rollup always recomputes, even when their source-count watermark is unchanged. Older candidate days are recomputed only when their count changes or a prior roll failed.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_jsonl_rollup_retention_days: int = Field(
        default=90,
        ge=1,
        alias="AVA_EVENTS_JSONL_ROLLUP_RETENTION_DAYS",
        description="Retention in days for the filtered local JSONL replay source (llm_usage, turn_end, and exec-family events). This must remain longer than Loki retention so the events-maintenance daemon can repair ledger gaps after an extended outage.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_resolution_burst_threshold: int = Field(
        default=5,
        ge=0,
        alias="AVA_EVENTS_RESOLUTION_BURST_THRESHOLD",
        description="A dismissed Loki event class reopens when its trailing ten-minute count is greater than this threshold. 0 reopens on the first matching event; the default 5 leaves normal low-volume recurrence dismissed.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_resolution_interval_seconds: int = Field(
        default=300,
        ge=1,
        alias="AVA_EVENTS_RESOLUTION_INTERVAL_SECONDS",
        description="Cadence in seconds for the immutable-event class-resolution slice: Loki count queries, burst reopen safety valve, and the six-hour unresolved gauges.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_auto_dismiss_enabled: bool = Field(
        default=False,
        alias="AVA_EVENTS_AUTO_DISMISS_ENABLED",
        description="Enable the daily stable-class auto-dismiss scan. Off by default: the normal resolution flow is an explicit authenticated API call by the ops agent or operator.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_auto_dismiss_days: int = Field(
        default=7,
        ge=1,
        alias="AVA_EVENTS_AUTO_DISMISS_DAYS",
        description="Days of consecutive non-empty six-hour Loki slices required before the optional stable-class auto-dismiss creates a dismissal.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    computer_use_lease_s: float = Field(
        default=30.0,
        alias="AVA_COMPUTER_LEASE_S",
        description="Computer-use screen-ownership lease: a holder that sends no action for this long loses the screen (Phase 2, task #1101).",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    computer_use_queue_timeout_s: float = Field(
        default=30.0,
        alias="AVA_COMPUTER_QUEUE_TIMEOUT_S",
        description="How long a computer-use action waits in the FIFO queue before failing with 'screen busy' (Phase 2, task #1101).",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    computer_use_session_idle_s: float = Field(
        default=600.0,
        alias="AVA_COMPUTER_SESSION_IDLE_S",
        description="Computer-use task-session idle threshold: a task_id with no action for this long emits computer_session_end (Phase 2, task #1101).",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    @field_validator("delivery_watchdog_dispatch_backoff_steps_s", mode="before")
    @classmethod
    def _parse_delivery_watchdog_dispatch_backoff_steps(cls, value: object) -> object:
        """Accept either a JSON array or a comma-separated environment value."""
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return [float(step.strip()) for step in value.split(",") if step.strip()]
        return value

    @field_validator("delivery_watchdog_dispatch_backoff_steps_s")
    @classmethod
    def _validate_delivery_watchdog_dispatch_backoff_steps(cls, value: list[float]) -> list[float]:
        if not value:
            raise ValueError("delivery watchdog dispatch backoff steps must not be empty")
        if any(step <= 0 for step in value):
            raise ValueError("delivery watchdog dispatch backoff steps must all be positive")
        return value
