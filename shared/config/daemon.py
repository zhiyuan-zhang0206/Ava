"""Daemon config — DaemonSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from shared.config._base import EnvSettings


class DaemonSettings(EnvSettings):
    runner_mode: Literal["process", "hosted"] = Field(
        default="process",
        alias="AVA_RUNNER_MODE",
        description="How the agent-runner hosts agents. `process` = one OS process per agent, alive from spawn to terminate (today's model). `hosted` = the runner daemon hosts every local agent's turns as asyncio tasks in its own process, and an idle agent is no task at all; it is what puts the `agent-host` service on this host's start roster. Cluster-pinned because the model must be uniform: a cluster running both would need double bookkeeping for agent leases, since a hosted agent's liveness is an in-process fact while a process agent's is a lease row. Rollback is a restart with this flipped back — no schema shape changes with it.",
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

    health_probe_agent_min: int = Field(
        default=1,
        alias="AVA_HEALTH_PROBE_AGENT_MIN",
        description="Minimum running/idling agents the health-probe requires for a healthy verdict. A test/QA cluster with no resident agents must set this to 0, or the probe's agent-population check fails forever and --auto-rollback rolls the cluster back to the last-known-good commit on a cycle (2026-08-10 preview incident).",
        json_schema_extra={
            "restart_required": "none",
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
            "capability": "agent-runner",
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

    idle_shell_reminder_enabled: bool = Field(
        default=True,
        alias="AVA_IDLE_SHELL_REMINDER_ENABLED",
        description="Run the gateway daemon that reminds agents about persistently idle shell sessions. On by default; set false to disable reminders without closing any sessions.",
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

    delivery_watchdog_threshold_seconds: float = Field(
        default=30.0,
        alias="AVA_DELIVERY_WATCHDOG_THRESHOLD_SECONDS",
        description="Delivery watchdog stall alert threshold (seconds): a chat inbound still pending longer than this whose owner is in a waiting/terminal state (idling/hibernating/terminated) is reported as a stalled delivery.",
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

    hibernate_enabled: bool = Field(
        default=True,
        alias="AVA_HIBERNATE_ENABLED",
        description="Run the hibernation controller (agent-runner), which swaps idle agents' processes out to free RAM. Off disables swap-out cluster-wide but does not resurrect already-hibernating agents — a heartbeat/inbound still wakes them.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    hibernate_idle_threshold_seconds: float = Field(
        default=450.0,
        alias="AVA_HIBERNATE_IDLE_THRESHOLD_SECONDS",
        description="Minimum idle time (seconds) since an agent's last completed turn before the hibernation controller swaps its process out to free RAM. Default 450s, deliberately above heartbeat_idle_threshold_seconds, so a normally-idling agent is woken by the heartbeat first and hibernation mainly reclaims heartbeat-paused agents. Lower it to also reclaim non-paused idle agents, at the cost of a cold start on the next wake.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hibernate_min_active: int = Field(
        default=100,
        ge=0,
        alias="AVA_HIBERNATE_MIN_ACTIVE",
        description="Warm-pool floor: this host's N most recently active agents (by last activity, among running/idling) are exempt from swap-out no matter how long they idle. Candidates are drawn only from the tail beyond the floor. 0 disables the floor. Trades a little resident RAM for zero cold-start latency on the agents most likely to be messaged next; size it to the host's RAM.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
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

    wedged_agent_enabled: bool = Field(
        default=True,
        alias="AVA_WEDGED_AGENT_ENABLED",
        description="Run the wedged-agent controller (agent-runner), which detects and recovers live-but-stuck agents — a running/idling agent with a live pid but a stale unconsumed pending inbound. Force-kills and resurrects the agent so it boots fresh and processes its backlog. Off does not strand anything — a new inbound still wakes the agent; this controller only closes the gap where no new message arrives and the existing pending inbound is ignored.",
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
        description="Minimum age (seconds) of an unconsumed pending inbound before the agent is considered wedged. Default 2400s (40 min) — exec_node_timeout_seconds (1200s) + LLM retry budget plus margin. Raise for agents doing long-running work; lower for tighter detection.",
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

    events_maintenance_enabled: bool = Field(
        default=False,
        alias="AVA_EVENTS_MAINTENANCE_ENABLED",
        description="Run the events-archive maintenance slices (partition rolling + retention + index governance) inside the events-maintenance daemon. The daemon itself always runs on the gateway: the cost-ledger rollup (Loki -> agent_model_tokens_daily), the checkpoint reaper (Rule A/B) and the blob vacuum are unconditional — the rollup must outlive Loki's 168h retention, and gating the daemon off stopped the reaper and checkpoint_blobs grew ~150MB/h (2026-08-12 design regression). Off by default since the LGTM cutover (task #1197): the PG events copy is a read-only archive.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
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

    events_retention_audit_days: int = Field(
        default=365,
        ge=0,
        alias="AVA_EVENTS_RETENTION_AUDIT_DAYS",
        description="Retention (days) for category=audit rows in the unified events table: the events-maintenance daemon drops a whole month partition once every category in it has outlived its retention. Audit is the compliance category, so this is the long pole — telemetry/log rows in a mixed partition are pruned early, the partition itself is dropped when audit expires too. 0 = expire immediately (all closed months dropped on the next pass).",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_retention_telemetry_days: int = Field(
        default=90,
        ge=0,
        alias="AVA_EVENTS_RETENTION_TELEMETRY_DAYS",
        description="Retention (days) for category=telemetry rows in the unified events table (llm_usage / turn_end / exec / ...): the performance/cost analysis window. Expired telemetry rows are pruned from still-live month partitions ahead of the audit-driven whole-partition drop. 0 = expire immediately.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_retention_log_days: int | None = Field(
        default=None,
        ge=0,
        alias="AVA_EVENTS_RETENTION_LOG_DAYS",
        description="Optional override (days) for category=log retention in the unified events table: log value decays fastest and the local JSONL mirror keeps the full text, so the registry default (30d) applies unless overridden. None (default) = the registry default from shared/events/contract.py — the single source of truth. Expired log rows are pruned from still-live month partitions ahead of the audit-driven whole-partition drop. 0 = expire immediately.",
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
