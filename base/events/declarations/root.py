"""Root supervisor, custody, backup, schedule and converge events."""

from __future__ import annotations

from typing import Literal, TypedDict

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
    intent stays running, and the health monitor retries under its backoff.
    """

    unit: str
    stage: str
    detail: str


class RootRestartCleared(TypedDict):
    """A recorded replacement failure cleared by a fresh active generation."""

    unit: str
    failed_for_s: float


class CustodyReconcile(TypedDict):
    """One custody record's reconcile outcome (task #4872, route C audit).

    Emitted for every record a reconcile pass examines — ``released`` when the
    record was cleared (every recorded birth gone and the unit's process group
    empty) or ``retained`` when one unproven fact kept it — so the decision and
    its evidence replay from the stream. A repeatable pass reports a release
    always and a retained outcome on first sight and on evidence change only,
    so the stream carries the decision rather than a per-round heartbeat. A
    record an active generation owns is its own bookkeeping: not examined, not
    emitted.
    """

    unit: str
    checked: int
    found: int
    decision: Literal["released", "retained"]
    evidence: str


class RootUnitFailureState(TypedDict):
    """One root unit sits in an explicit failure state (task #4872).

    Emitted on EVERY health round while the condition holds — the unit's intent
    is running and it has a recorded replacement failure, an open restart
    breaker, or retained native custody — so a rule's pending period can debounce
    it and the stream going quiet resolves it. ``kind`` is ``restart_failed``,
    ``breaker_open`` or ``custody_held`` (the first that applies in that order);
    ``detail`` is the bounded evidence line, never a grouping key.
    """

    unit: str
    kind: str
    detail: str


class RootUnitNotRevivable(TypedDict):
    """A unit's probe verdict is terminal: this unit cannot revive it.

    Emitted on every health round that observes a terminal verdict (unknown
    identity, inspection error, timeout, foreign listener) outside an operator
    stop. One round is often an inspection race; a streak is the incident —
    the rule's pending period tells them apart. ``detail`` is the probe's
    evidence, never a grouping key.
    """

    unit: str
    detail: str


class RootDiagnostic(TypedDict):
    """`root_diagnostic` payload — services/ava_root_glue/diagnostics.py.

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
    """`schedule_stalled` payload — services/schedule_manager/manager.py.

    Emitted once after an enabled, non-completed schedule has had no live
    session for more than two hours. A live observation rearms a later outage.
    """

    schedule_id: int
    status: str
    stalled_seconds: float


class BackupOperationCustody(TypedDict):
    """`backup_operation_custody` payload — scheduled backup operation custody.

    ``operation`` names the operation kind (``logical-dump`` or
    ``logical-restore-drill``). ``custody`` is ``quarantined`` (closure proven; the next operation
    proceeds), ``blocked`` (closure unproven; the kind refuses new work until
    ``ava backup operations retire``) or ``retired`` (an operator retirement).
    ``detail`` is the bounded diagnostic; it is never an alert grouping key.
    """

    operation: str
    custody: str
    detail: str


class RecoveryDrillFailed(TypedDict):
    """`recovery_drill_failed` payload — the scheduled logical restore drill.

    ``drill`` identifies the recovery path that needs intervention. ``detail``
    is the bounded failure diagnostic retained in the event stream; it is not a
    Prometheus measurement or alert grouping key.
    """

    drill: str
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
    """`converge_file_preserved` payload — base/host/converge/preserve_report.py.

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
            "base/host/converge/preserve_report.py:report_converge_preserve "
            'telemetry.emit("telemetry", ...)'
        ),
    ),
    # Root-owned chain episodes and service recovery policy.
    "root_chain_broken": telemetry_event(
        "root_chain_broken",
        "root self-check found a managed unit no longer a live child of the root process — one alert per episode, held until intact",
        tier="anomaly",
    ),
    "root_restart_breaker_open": telemetry_event(
        "root_restart_breaker_open",
        "root health monitor restart breaker opened — repeated non-alive probe rounds held until a probe-alive round",
        tier="anomaly",
    ),
    "root_restart_failed": telemetry_event(
        "root_restart_failed",
        "root unit replacement failed at its down|up half — explicit failure state recorded; "
        "intent stays running and the health monitor retries under its backoff (task #4872)",
        payload=RootRestartFailed,
        tier="anomaly",
    ),
    "root_restart_cleared": telemetry_event(
        "root_restart_cleared",
        "root unit replacement succeeded — the recorded failure state was cleared (task #4872)",
        payload=RootRestartCleared,
        tier="noise",
    ),
    "custody_reconcile": telemetry_event(
        "custody_reconcile",
        "custody record reconcile pass — releases always report; a retained record reports on "
        "first sight and evidence change — with its birth and process-group evidence "
        "(task #4872)",
        payload=CustodyReconcile,
        tier="observation",
    ),
    "root_unit_failure_state": telemetry_event(
        "root_unit_failure_state",
        "root unit sits in an explicit failure state (intent running, and restart_failed, "
        "breaker open, or retained custody) — emitted every health round while it holds "
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
        site=("services/schedule_manager/manager.py:_report_stalled_schedules telemetry.emit"),
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
    "backup_operation_custody": telemetry_event(
        "backup_operation_custody",
        "backup operation quarantined, blocked on unproven closure, or retired",
        payload=BackupOperationCustody,
        tier="anomaly",
        site=("services/backup_scheduler/operation/custody.py:report (positional emit)"),
    ),
    "postgres_stop_escalated": telemetry_event(
        "postgres_stop_escalated",
        "a Postgres fast shutdown did not finish within its budget and was ended by an "
        "immediate shutdown plus a SIGKILL of the leftover descendants (usually a hung "
        "archive command)",
        tier="anomaly",
        site=(
            "cli/commands/lifecycle/service_stop.py:report_postgres_stop_escalation "
            'telemetry.emit("telemetry", ...)'
        ),
    ),
    "recovery_drill_failed": telemetry_event(
        "recovery_drill_failed",
        "scheduled logical restore drill failed",
        payload=RecoveryDrillFailed,
        tier="anomaly",
        site=("services/backup_scheduler/daemon.py:_run_due_local_dump_restore (positional emit)"),
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
        site="cli/commands/_probe.py:_report_non_critical_unready_services telemetry.emit",
    ),
    "schedule_verify_failed": telemetry_event(
        "schedule_verify_failed",
        "ava schedules verify found in-store schedule scripts that fail dry-import or "
        "call-signature binding, or the sweep itself failed",
        payload=ScheduleVerifyFailed,
        tier="anomaly",
        site="cli/commands/management/schedules_verify.py:_report_verify telemetry.emit",
    ),
    "lifecycle_pointer_done_torn": EventSpec(
        name="lifecycle_pointer_done_torn",
        category="log",
        tier="anomaly",
        doc="the TTL reaper's scan found lifecycle command(s) sitting at done while agents_meta.lifecycle_command_id still pointed at them (an out-of-band torn write, task #3678) — every resurrect of the named agent(s) defers until settled; attributes carry count and samples",
        site=(
            "services/ttl_reaper/lifecycle_fences.py:_scan_torn_lifecycle_pointers_blocking "
            "(positional emit)"
        ),
    ),
    "lifecycle_fences_settled_absent_machine": EventSpec(
        name="lifecycle_fences_settled_absent_machine",
        category="log",
        tier="observation",
        doc="the TTL reaper settled applied-but-unobserved force-terminate command(s) whose agent's home machine is absent from the machines registry (a decommissioned machine never runs the boot recovery that would observe its fences, task #4143); attributes carry count and samples",
        site=(
            "services/ttl_reaper/lifecycle_fences.py:settle_absent_machine_fences (positional emit)"
        ),
    ),
}
