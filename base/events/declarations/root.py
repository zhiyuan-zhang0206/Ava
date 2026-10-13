"""Root supervisor, backup, schedule and converge events."""

from __future__ import annotations

from typing import TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class RootHealthTick(TypedDict):
    """Completed service/diagnostic observation round, regardless of its verdicts."""

    home_id: str
    last_tick_timestamp_seconds: float


class RootHealthExpected(TypedDict):
    """Observer start time before its first sample; zero explicitly retires it."""

    home_id: str
    expected_since_timestamp_seconds: float


class RootRestartFailed(TypedDict):
    """One member's failed replacement half, recorded explicitly (task #4872).

    Emitted when a unit enters `restart_failed`: the unit ends an interrupted
    replacement in an explicit failure state instead of a stop residue, its
    intent stays running, and the health monitor has a finite automatic restart budget.
    """

    unit: str
    stage: str
    detail: str


class RootRestartCleared(TypedDict):
    """A recorded replacement failure cleared by a fresh active generation."""

    unit: str
    failed_for_s: float


class RootUnitFailureState(TypedDict):
    """One root unit sits in an explicit failure state (task #4872).

    Emitted on EVERY health round while the condition holds — the unit's intent
    is running and it has a recorded replacement failure or exhausted automatic
    restart attempts. A rule can debounce that stream; silence resolves it. ``kind`` is ``restart_failed``,
    ``restarts_exhausted`` (the first that applies in that order);
    ``detail`` is the bounded evidence line, never a grouping key.
    """

    unit: str
    kind: str
    detail: str


class RootUnitNotRevivable(TypedDict):
    """A unit has no automatic restart authority for its observed failure.

    Emitted on every health round that observes a terminal verdict (unknown
    identity, inspection error, timeout, foreign listener) outside an operator
    stop. DOWN with NEVER policy, no restart verb or a code-gate exit also reports
    here. One round is often an inspection race; a streak is the incident —
    the rule's pending period tells them apart. ``detail`` is the probe's
    evidence, never a grouping key.
    """

    unit: str
    detail: str


class RootDiagnostic(TypedDict):
    """`root_diagnostic` payload — services/supervision/ava_root_glue/diagnostics.py.

    One row per non-alive diagnostic sample (WARNING, every sample while the
    condition persists) and one on recovery (INFO). ``diagnostic`` names the
    check, ``verdict`` is its reading, ``consecutive_failures`` the streak the
    sample extends; ``detail`` is the evidence, never a grouping key.
    """

    diagnostic: str
    verdict: str
    detail: str
    consecutive_failures: int


class ScheduleStalled(TypedDict):
    """`schedule_stalled` payload — services/wake/schedule_manager/manager.py.

    Emitted once after an enabled, non-completed schedule has had no live
    session for more than two hours. A live observation rearms a later outage.
    """

    schedule_id: int
    status: str
    stalled_seconds: float


class ScheduleConvergenceAbandoned(TypedDict):
    """`schedule_convergence_abandoned` payload — services/wake/schedule_manager/manager.py.

    Emitted once when the schedule manager stops retrying a schedule whose
    session survives its reap (or whose live session's launch provenance cannot
    be read) for ``blocked_seconds``. ``reason`` names the blocked step. The
    schedule is recorded as an error; an explicit new revision lifts the stop.
    """

    schedule_id: int
    reason: str
    blocked_seconds: float


class MaintenanceHoldOverdue(TypedDict):
    """`maintenance_hold_overdue` payload — services/supervision/healthchecks/maintenance_hold.py.

    Emitted once per hold (one episode) by the root diagnostic that reads this
    home's pause-owner journal, when the hold has been in place for the
    configured age. ``state`` is ``paused`` for a readable hold, with its
    ``holder``, ``phase`` and ``acquired_at``; ``invalid`` for an unreadable
    journal, whose holder, phase and acquisition time are unknown (None) and
    whose ``held_seconds`` counts from the file's modification time.
    """

    state: str
    holder: str | None
    phase: str | None
    acquired_at: str | None
    held_seconds: float


class RecoveryDrillFailed(TypedDict):
    """`recovery_drill_failed` payload — the scheduled logical restore drill.

    ``drill`` identifies the recovery path that needs intervention. ``detail``
    is the bounded failure diagnostic retained in the event stream; it is not a
    Prometheus measurement or alert grouping key.
    """

    drill: str
    detail: str


class BackupDumpFailed(TypedDict):
    """`backup_dump_failed` payload — the scheduled daily logical dump.

    ``detail`` is the bounded failure diagnostic retained in the event stream; it
    is not a Prometheus measurement or alert grouping key. The scheduler attempts
    a dump once per cluster day, so each event is one day without a new dump.
    """

    detail: str


class HealthProbeFailing(TypedDict):
    """`health_probe_failing` payload — cli/commands/cluster/health.py.

    One row per unhealthy probe run, repeated on every run while the condition
    holds. ``check`` names the failed check (the Grafana grouping key);
    ``failure_class`` is the probe's reading of the cause (``code`` /
    ``environment`` / ``maintenance``); ``message`` is the bounded diagnostic
    and not a grouping key.
    """

    check: str
    failure_class: str
    message: str


class HealthProbeRan(TypedDict):
    """`health_probe_ran` payload — cli/commands/cluster/health.py.

    One row per completed probe run, healthy or not: the probe's own heartbeat,
    so its absence (not any failure) is the signal that the probe stopped
    running. ``unhealthy_checks`` is 0 for a healthy run, 1 when the run
    stopped at its first failing check.
    """

    unhealthy_checks: int


class ServiceStartUnready(TypedDict):
    """`service_start_unready` payload — cli/commands/lifecycle/start.py.

    One row per non-critical service that missed its readiness window at an
    `ava start` (the start does not fail on it).
    """

    service: str


class BootRetriesExhausted(TypedDict):
    """`boot_retries_exhausted` payload — cli/start_intent.py.

    The OS boot job (`ava start --boot`) spent its attempt budget
    (`base.host.system.boot_policy`): ``attempts`` boot starts failed and the last
    one exited ``exit_code``. The scheduler is no longer asked to retry; the cause
    is in the boot log and an operator runs `ava start`.
    """

    attempts: int
    exit_code: int


class LifecyclePointerStuck(TypedDict):
    """`lifecycle_pointer_stuck` payload — services/upkeep/ttl_reaper/lifecycle_fences.py.

    One row per hourly scan that found stuck pointers: ``count`` agents are
    affected, ``samples`` lists up to five of their ids.
    """

    count: int
    samples: str


class ScheduleVerifyFailed(TypedDict):
    """`schedule_verify_failed` payload — cli/commands/management/schedules_verify.py.

    One row per non-clean `ava schedules verify` sweep of the live table:
    ``red`` in-store scripts failed out of ``checked``; ``tool_error`` is set
    when the sweep itself could not run; ``detail`` lists the failures.
    """

    checked: int
    red: int
    tool_error: str | None
    detail: str


class ConvergeFilePreserved(TypedDict):
    """`converge_file_preserved` payload — base/telemetry/converge_report.py.

    One row per converge-managed destination preserved because its current
    content no longer matches the recorded render — someone hand-edited it
    since the last write. The renderer warns and preserves (never overwrites);
    this event makes the interception visible without reading converge output
    (task #3871, after the LGTM dashboard evaded three consecutive rollouts).
    """

    path: str
    key: str
    surface: str


EVENTS: dict[str, EventSpec] = {
    "lgtm_dashboard_render_failed": telemetry_event(
        "lgtm_dashboard_render_failed",
        "ava-ops dashboard render failed during converge; the previous provisioning file was kept",
        tier="anomaly",
        site=(
            "cli/commands/observability/_lgtm_provisioning.py:_render_ava_ops_dashboard "
            'telemetry.emit("telemetry", ...)'
        ),
    ),
    "converge_file_preserved": telemetry_event(
        "converge_file_preserved",
        "converge kept a locally modified destination instead of overwriting — the current "
        "content no longer matches the recorded render; repeats every converge until resolved",
        payload=ConvergeFilePreserved,
        tier="anomaly",
        site=(
            "base/telemetry/converge_report.py:report_converge_preserve "
            'telemetry.emit("telemetry", ...)'
        ),
    ),
    # Root-owned chain episodes and service recovery policy.
    "root_chain_broken": telemetry_event(
        "root_chain_broken",
        "root self-check found a managed unit no longer a live child of the root process — one alert per episode, held until intact",
        tier="anomaly",
    ),
    "root_restart_failed": telemetry_event(
        "root_restart_failed",
        "root unit replacement failed at its down|up half — explicit failure state recorded; "
        "intent stays running and the health monitor has a finite automatic restart budget (task #4872)",
        payload=RootRestartFailed,
        tier="anomaly",
    ),
    "root_restart_cleared": telemetry_event(
        "root_restart_cleared",
        "root unit replacement succeeded — the recorded failure state was cleared (task #4872)",
        payload=RootRestartCleared,
        tier="noise",
    ),
    "root_unit_failure_state": telemetry_event(
        "root_unit_failure_state",
        "root unit sits in an explicit failure state (intent running, and restart_failed, "
        "restart attempts exhausted) — emitted every health round while it holds "
        "(task #4872)",
        payload=RootUnitFailureState,
        tier="anomaly",
    ),
    "root_unit_not_revivable": telemetry_event(
        "root_unit_not_revivable",
        "root health round observed a terminal probe verdict the unit cannot revive "
        "(unknown identity, inspection error, foreign listener) — emitted every round "
        "it is observed, WARNING",
        payload=RootUnitNotRevivable,
        tier="anomaly",
    ),
    # Parent-helper diagnosis never grants root authority to replace its ancestor.
    "permissions_helper_unhealthy": telemetry_event(
        "permissions_helper_unhealthy",
        "permissions helper failed its healthcheck (ping plus launchd job classification) — one alert per episode, held until a ping-alive round",
        tier="anomaly",
    ),
    "schedule_stalled": telemetry_event(
        "schedule_stalled",
        "enabled non-completed schedule has had no live session for more than two hours",
        payload=ScheduleStalled,
        tier="anomaly",
        site=("services/wake/schedule_manager/manager.py:_report_stalled_schedules telemetry.emit"),
    ),
    "schedule_convergence_abandoned": telemetry_event(
        "schedule_convergence_abandoned",
        "the schedule manager stopped retrying a schedule whose session survives its reap or whose live session's launch provenance is unreadable, and recorded the schedule as an error; an explicit new revision lifts the stop",
        payload=ScheduleConvergenceAbandoned,
        tier="anomaly",
        site="services/wake/schedule_manager/manager.py:_give_up telemetry.emit",
    ),
    "maintenance_hold_overdue": telemetry_event(
        "maintenance_hold_overdue",
        "this home's maintenance hold, or an unreadable hold journal, has been in place for more than the configured age (default 30 minutes) and freezes every wake on the machine; one event per hold, reporting only, release it with `ava start`",
        payload=MaintenanceHoldOverdue,
        tier="anomaly",
        site="services/supervision/healthchecks/maintenance_hold.py:_emit_overdue logger.error",
    ),
    "root_health_expected": telemetry_event(
        "root_health_expected",
        "root health observation rounds expected, including before the first sample",
        payload=RootHealthExpected,
        tier="noise",
    ),
    "root_diagnostic": telemetry_event(
        "root_diagnostic",
        "root diagnostic sampled non-alive (every sample while it persists, WARNING) or "
        "recovered (once, INFO); observation only, no recovery authority",
        payload=RootDiagnostic,
        tier="anomaly",
    ),
    "root_health_tick": telemetry_event(
        "root_health_tick",
        "root completed one service health and diagnostic observation round",
        payload=RootHealthTick,
        tier="noise",
    ),
    "postgres_stop_escalated": telemetry_event(
        "postgres_stop_escalated",
        "a Postgres fast shutdown did not finish within its budget and was ended by an "
        "immediate shutdown plus a SIGKILL of the leftover descendants (usually a hung "
        "archive command)",
        tier="anomaly",
        site=(
            "cli/commands/data_plane/stop_waits.py:report_postgres_stop_escalation "
            'telemetry.emit("telemetry", ...)'
        ),
    ),
    "backup_dump_failed": telemetry_event(
        "backup_dump_failed",
        "the scheduled daily logical dump failed; it is not attempted again before the next backup hour",
        payload=BackupDumpFailed,
        tier="anomaly",
        site="services/backup/scheduler/daemon.py:_backup_loop (positional emit)",
    ),
    "recovery_drill_failed": telemetry_event(
        "recovery_drill_failed",
        "scheduled logical restore drill failed",
        payload=RecoveryDrillFailed,
        tier="anomaly",
        site=("services/backup/scheduler/daemon.py:_run_due_local_dump_restore (positional emit)"),
    ),
    "health_probe_failing": telemetry_event(
        "health_probe_failing",
        "the cluster health probe found a failing check; repeated on every unhealthy run",
        payload=HealthProbeFailing,
        tier="anomaly",
        site="cli/commands/cluster/health.py:_report_failing telemetry.emit",
    ),
    "health_probe_ran": telemetry_event(
        "health_probe_ran",
        "the cluster health probe completed a run, healthy or not (its own heartbeat)",
        payload=HealthProbeRan,
        site="cli/commands/cluster/health.py:_report_ran telemetry.emit",
    ),
    "service_start_unready": telemetry_event(
        "service_start_unready",
        "a non-critical service missed its readiness window at ava start",
        payload=ServiceStartUnready,
        tier="anomaly",
        site="cli/commands/probe.py:report_non_critical_unready_services telemetry.emit",
    ),
    "boot_retries_exhausted": telemetry_event(
        "boot_retries_exhausted",
        "the boot-time ava start failed through its whole attempt budget and is no longer retried",
        payload=BootRetriesExhausted,
        tier="anomaly",
        site="cli/start_intent.py:_run_boot_attempt telemetry.emit",
    ),
    "schedule_verify_failed": telemetry_event(
        "schedule_verify_failed",
        "ava schedules verify found in-store schedule scripts that fail dry-import, an "
        "undefined-name read or call-signature binding, or the sweep itself failed",
        payload=ScheduleVerifyFailed,
        tier="anomaly",
        site="cli/commands/management/schedules_verify.py:_report_verify telemetry.emit",
    ),
    "lifecycle_pointer_stuck": telemetry_event(
        "lifecycle_pointer_stuck",
        "the TTL reaper's hourly scan found agents_meta.lifecycle_command_id pointing at a lifecycle "
        "command that nothing will settle on its own: one sitting at done (an out-of-band torn write, "
        "task #3678) or an applied force-terminate still unobserved an hour after it was applied (its "
        "original host crashed or its machine was decommissioned). Every resurrect of the named "
        "agent(s) defers until an operator settles the command",
        payload=LifecyclePointerStuck,
        tier="anomaly",
        site="services/upkeep/ttl_reaper/lifecycle_fences.py:_scan_stuck_lifecycle_pointers_blocking telemetry.emit",
    ),
}
