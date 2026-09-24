"""Daemon config — DaemonSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

import json

from pydantic import Field, field_validator, model_validator

from shared.config._base import EnvSettings
from shared.config.billing_recovery_fields import BillingRecoveryFields
from shared.config.delivery_outbox_fields import DeliveryOutboxFields
from shared.config.delivery_watchdog_fields import DeliveryWatchdogFields
from shared.config.hierarchy_worker_fields import HierarchyWorkerFields


class DaemonSettings(
    BillingRecoveryFields,
    DeliveryOutboxFields,
    DeliveryWatchdogFields,
    HierarchyWorkerFields,
    EnvSettings,
):
    host_max_concurrent_turns: int = Field(
        default=0,
        ge=0,
        alias="AVA_HOST_MAX_CONCURRENT_TURNS",
        description="Hosted agent-runner: optional limit on active agent continuations. Zero (default) disables this admission limit; positive values queue excess agents until a running agent idles or exits. A continuation includes model/tool waits and may span many steps. Database client pools and provider concurrency have separate budgets; this setting does not resize them. Set a positive limit when host memory or execution capacity requires one.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    host_recovery_wake_batch: int = Field(
        default=4,
        ge=1,
        alias="AVA_HOST_RECOVERY_WAKE_BATCH",
        description="Hosted agent-runner: maximum new recovery-class turn starts per host per pending-scan cycle. This paces the post-wave boot recovery herd (task #4652), which can flip the fleet to running simultaneously and contend on database and runtime builds. Lower values give a gentler but longer drain; higher values approach the old herd. Ordinary message delivery is never delayed by this cap.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    host_db_recovery_prolonged_attempts: int = Field(
        default=6,
        ge=1,
        alias="AVA_HOST_DB_RECOVERY_PROLONGED_ATTEMPTS",
        description="Hosted agent-runner: warn once per database-recovery ladder when this attempt count or AVA_HOST_DB_RECOVERY_PROLONGED_SECONDS is reached on a retry. The observed normal band is at most 5 attempts over a couple of minutes; 6 attempts is clearly beyond a transient flap while far below the recovery budget. This warning does not interrupt recovery.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    host_db_recovery_prolonged_seconds: float = Field(
        default=300.0,
        gt=0,
        alias="AVA_HOST_DB_RECOVERY_PROLONGED_SECONDS",
        description="Hosted agent-runner: warn once per database-recovery ladder when this total elapsed time or AVA_HOST_DB_RECOVERY_PROLONGED_ATTEMPTS is reached on a retry. The observed normal band is at most 5 attempts over a couple of minutes; 300 seconds (5 minutes) is clearly beyond a transient flap while far below the recovery budget. This warning does not interrupt recovery.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    host_db_recovery_budget_seconds: float = Field(
        default=3600.0,
        ge=600,
        alias="AVA_HOST_DB_RECOVERY_BUDGET_SECONDS",
        description="Hosted agent-runner: total database-recovery ladder budget, checked before each attempt. Exhaustion emits an error and exits through the existing turn crash path so the next wake can retry. This safety fuse provides a final, alertable convergence channel for a live host with a half-dead database for hours. The default 3600 seconds is approximately 4 times the observed maximum of 853 seconds, deliberately far above any normal flap so it never acts as a throttle. There is no middle tier.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    host_admission_wait_alert_seconds: float = Field(
        default=2400.0,
        gt=0,
        alias="AVA_HOST_ADMISSION_WAIT_ALERT_SECONDS",
        description="Hosted agent-runner: a turn queued on the admission gate (AVA_HOST_MAX_CONCURRENT_TURNS) for at least this many seconds emits one host_admission_wait_exceeded anomaly event per wait episode. Default 2400s mirrors the wedged-turn budget (AVA_WEDGED_AGENT_INBOUND_AGE_SECONDS) so 'too long' has one dialect, but the two knobs are independent: queue wait is capacity pressure, not a stalled turn, and the wait is exempt from stall cancellation. The event is a signal, never a cancellation; raise the limit or inspect the turns holding slots.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    host_db_pool_max_size: int = Field(
        default=64,
        gt=0,
        alias="AVA_HOST_DB_POOL_MAX_SIZE",
        description="Hosted agent-runner: maximum workload/checkpoint client connections per host, independent of active agent count. Connections open on demand. Budget the sum of all hosts' workload and control pools plus other clients below PgBouncer max_client_conn (or PostgreSQL max_connections when connecting directly). This is a client connection budget, not a measured database throughput limit.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    host_control_pool_max_size: int = Field(
        default=8,
        gt=0,
        alias="AVA_HOST_CONTROL_POOL_MAX_SIZE",
        description="Hosted agent-runner: maximum client connections reserved for ownership, lifecycle, recovery, and durable scans. Workload borrowers cannot consume this pool. Both pools still share the same PgBouncer backend pool; this reserves client capacity only. Include it in the cluster-wide client connection budget.",
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

    host_abort_reconcile_enabled: bool = Field(
        default=True,
        alias="AVA_HOST_ABORT_RECONCILE_ENABLED",
        description="Hosted agent-runner: at a settled turn abort, dispose the turn's claimed 'chat' inbounds immediately (committed to the flushed checkpoint -> done, uncommitted -> pending for the next claim, past the stale threshold -> dead-lettered) instead of waiting for a cold admission or boot that may never come. Off restores the deferred behavior; every non-abort crash keeps deferring to the next cold admission's reconcile.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    host_turn_reconcile_enabled: bool = Field(
        default=True,
        alias="AVA_HOST_TURN_RECONCILE_ENABLED",
        description="Hosted agent-runner: at every finished non-crashed turn, dispose the turn's claimed 'chat' inbounds immediately (committed to the flushed checkpoint -> done, uncommitted -> pending for the next claim, past the stale threshold -> dead-lettered) instead of leaving them claimed until the next cold admission or abort. The throttled checkpoint tail is flushed first; off restores the deferred behavior.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    hosted_recrash_prompt_reap_enabled: bool = Field(
        default=False,
        alias="AVA_HOSTED_RECRASH_PROMPT_REAP_ENABLED",
        description="Hosted agent-runner: terminate a crash-marked corpse immediately when a second turn crash settles under the same mark (task #3616) — the first grace window is kept whole; a retry that died again has spent its chance, and only a zombie spending the rest claiming and re-dying is left otherwise. Off keeps today's grace-only semantics; gray release off->on, flipped per host after observing the first enable.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    hosted_crash_recovery_wake_enabled: bool = Field(
        default=True,
        alias="AVA_HOSTED_CRASH_RECOVERY_WAKE_ENABLED",
        description="Hosted agent-runner: when the corpse reaper terminates a crash-marked agent (grace-window or recrash reap), commit one durable system-source recovery chat in the same transaction and let the service layer attempt the guarded auto-resurrect right after — a crash death with no arriving work otherwise has no wake left (reminder-class wakes are ignored for terminated owners), and the 2026-09-15 stall wave left one crashed agent silent for ~11 hours (task #4039). The resurrection gates (closed / wake suppression / recovery breaker) are unchanged and consulted at attempt time; the delivery watchdog's terminated-owner retry owns any deferred attempt until the chat's 24h age gate. Off restores the bare reap: only arriving work resumes the owner.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
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

    chrome_page_default_ttl_seconds: float = Field(
        default=86400.0,
        gt=0,
        alias="AVA_CHROME_PAGE_DEFAULT_TTL_SECONDS",
        description="Default lifetime in seconds for a Chrome page created through the shared browser (an explicit new_page call, or the auto-created page on a page-less first navigation). The browser-mcp daemon closes the page when the deadline passes; an agent extends it with the renew_page tool, at most 24h per renewal. Mirrors the persistent-shell TTL ruling: a page is a bounded resource, never extended by activity alone.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    notice_ttl_limit_seconds: float = Field(
        default=86400.0,
        alias="AVA_NOTICE_TTL_LIMIT_SECONDS",
        description="Maximum lifetime in seconds for agent notices. Notices without an explicit expire_at use now + limit; notices requesting a longer lifetime are clamped to this limit.",
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

    schedule_fire_log_retention_days: int = Field(
        default=30,
        alias="AVA_SCHEDULE_FIRE_LOG_RETENTION_DAYS",
        description="Days of schedule_fire_log claims kept by the gateway reaper's retention prune. schedule_fire_log is the at-most-once claim ledger for schedule catch-up; the prune deletes rows whose slot is older than this window but always keeps the newest claim per schedule so the catch-up baseline never regresses (a regressed baseline would refire a sparse-cron schedule's already-claimed slot).",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    schedule_fire_log_cleanup_interval_seconds: float = Field(
        default=86400.0,
        alias="AVA_SCHEDULE_FIRE_LOG_CLEANUP_INTERVAL_SECONDS",
        description="How often the gateway reaper runs the schedule_fire_log retention prune. One bounded DELETE per pass; the default keeps a single daily pass.",
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
        description="Minimum running/idling agents the health-probe requires for a healthy verdict. A dev/QA cluster (any non-default home) has this seeded to 0 in its `.env` at birth: with no resident agents by design, the check would otherwise fail forever and --auto-rollback would cycle the checkout (2026-08-10 preview and 2026-09-12 dev-worktree incidents). An explicit value — including the prod default 1 — stands as written.",
        json_schema_extra={
            "restart_required": "",
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

    exec_request_bounded_quarantine_enabled: bool = Field(
        default=True,
        alias="AVA_EXEC_REQUEST_BOUNDED_QUARANTINE_ENABLED",
        description="Hosted boot recovery and cold prepare: quarantine an unreadable exec request envelope without human review once it is older than twice the exec node timeout with no live process reference and no live host process (task #3619 D-2). The bytes are preserved with a receipt and the recovery path no longer defers on them; off restores unbounded retention, which needs the manual --force quarantine.",
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

    events_maintenance_checkpoint_trim_enabled: bool = Field(
        default=False,
        alias="AVA_EVENTS_MAINTENANCE_CHECKPOINT_TRIM_ENABLED",
        description="Enforce the per-thread keep-three checkpoint budget on the maintenance fast loop. Default false (never-delete ruling, 2026-09-12): nothing is deleted while the checkpoint storage model is retention-first. Setting true re-enables history deletion — an explicit authorization, and unsafe for delta-written threads.",
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

    @model_validator(mode="after")
    def _validate_hierarchy_budget_below_deadline(self) -> DaemonSettings:
        """The job budget must leave room under the hard deadline.

        A budget >= deadline would let the worker's own stop point overshoot
        the kill ceiling: the child gets SIGKILLed mid-write with no graceful
        partial result, and the retry loop pays full cost each time — exactly
        what the budget exists to prevent.
        """
        if self.hierarchy_job_budget_seconds >= self.hierarchy_job_deadline_seconds:
            raise ValueError(
                "hierarchy_job_budget_seconds must be below "
                "hierarchy_job_deadline_seconds (the graceful stop point must "
                "leave kill margin)"
            )
        return self

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

    @field_validator("delivery_outbox_retry_backoff_steps_s", mode="before")
    @classmethod
    def _parse_delivery_outbox_retry_backoff_steps(cls, value: object) -> object:
        """Accept either a JSON array or a comma-separated environment value."""
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return [float(step.strip()) for step in value.split(",") if step.strip()]
        return value

    @field_validator("delivery_outbox_retry_backoff_steps_s")
    @classmethod
    def _validate_delivery_outbox_retry_backoff_steps(cls, value: list[float]) -> list[float]:
        if not value:
            raise ValueError("delivery outbox retry backoff steps must not be empty")
        if any(step <= 0 for step in value):
            raise ValueError("delivery outbox retry backoff steps must all be positive")
        return value
