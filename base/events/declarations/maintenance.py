"""Scheduled reports, task maintenance and hierarchy-worker events."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class CiUsageDaily(TypedDict):
    """`ci_usage_daily` payload — schedules/c9-daily-report-schedule.py.

    One event per reconciliation day (05:00-05:00 cluster time): the CI
    workflow's runs and ceil-billed minutes for the window, split by
    attribution (PR-title [Ava-<id>] convention) and OS. `est_usd` is the
    private-repo overage equivalent at the GitHub-hosted rates — the repo is
    currently public, so minutes are the billing fact (scripts/ci/accounting.py).
    Per-agent detail lives in the attribution ledger, not here.
    """

    day: str  # cluster-tz date of the window end
    window_start: str  # UTC ISO
    window_end: str  # UTC ISO
    runs: int
    attributed_runs: int
    unattributed_runs: int
    total_minutes: int
    attributed_minutes: int
    linux_minutes: int
    macos_minutes: int
    appended_runs: int  # new ledger rows this reconciliation (0 = re-fire no-op)
    est_usd: float


class DebtSweepDaily(TypedDict):
    """`debt_sweep_daily` payload — schedules/debt-sweep-daily-schedule.py.

    One event per claimed 06:30 cluster-time slot. The mechanical scan may
    fail without blocking the clearing worker, so scan status is an explicit
    fact rather than an implied success from the worker action.
    """

    day: str  # cluster-tz date of the claimed slot
    scan_status: str  # ok | failed
    action: str  # spawned | resurrected | messaged
    worker_agent_id: int


class PrFlowDaily(TypedDict):
    """`pr_flow_daily` payload — scripts/pr_flow_export.py (macmini daily job).

    One event per complete cluster-tz day in the trailing window, re-emitted
    on every run so the whole window stays inside Prometheus's retention.
    Every numeric field is absolute per-day state, never a sum, and the OTLP
    disposition records each as an ObservableGauge
    (``base/telemetry/otlp/telemetry_otlp.py``) — a counter or histogram would accrue
    across re-emissions. Fields are absent when the day has no such sample
    (no merges -> no percentile values; an unreachable flaky source
    omits ``flake_new_quarantines`` rather than claiming zero).
    """

    day: str  # cluster-tz date (Asia/Shanghai fleet clock)
    merged_count: int  # PRs whose merged_at falls on the day
    ready_to_merge_median_seconds: NotRequired[float]
    ready_to_merge_p90_seconds: NotRequired[float]
    flake_new_quarantines: NotRequired[int]  # tests quarantined on the day


class PrFlowRun(TypedDict):
    """`pr_flow_run` payload — scripts/pr_flow_export.py (macmini daily job).

    One event per run: the point-in-time Trunk queue depth sample. Absolute
    state -> ObservableGauge (``ava_pr_flow_run_queue_depth_ratio``). The
    event is emitted even when the depth sample is unavailable, so the daily
    breadcrumb survives a Trunk outage; the missing field stays absent (an
    absent optional metric is not zero).
    """

    queue_depth: NotRequired[int]


class CiRunsDaily(TypedDict):
    """`ci_runs_daily` payload — scripts/ci/runs_export.py.

    One absolute-state sample per complete cluster-time day and repository.
    The collector re-emits its trailing window, so every number is an OTLP
    gauge rather than an accumulating counter. Classification labels overlap;
    `white_run_share` alone deduplicates instant skips, superseded runs, and
    abandoned PR heads in that priority order. Per-PR percentiles are absent
    when no completed PR has attributed runs. `first_pass_pr_share` is v1's
    run-level approximation: attributed non-noise, non-zombie runs have no
    red conclusion and no retry attempt.
    """

    repo: str
    day: str
    runs: int
    instant_skip_runs: int
    watchdog_runs: int
    proof_runs: int
    cancelled_runs: int
    superseded_runs: int
    superseded_zero_runs: int
    failed_runs: int
    retried_failed_runs: int
    self_healed_runs: int
    abandoned_runs: int
    zombie_runs: int
    prs_completed: int
    prs_with_runs: int
    per_pr_duration_median_minutes: NotRequired[float]
    per_pr_duration_p90_minutes: NotRequired[float]
    per_pr_runs_median: NotRequired[float]
    per_pr_runs_p90: NotRequired[float]
    per_pr_runs_executed_median: NotRequired[float]
    white_run_share: NotRequired[float]
    retry_share: NotRequired[float]
    noise_run_share: NotRequired[float]
    first_pass_pr_share: NotRequired[float]


class CiWorkflowWindow(TypedDict):
    """`ci_workflow_window` payload — scripts/ci/runs_export.py.

    Trailing-window absolute workflow state, keyed by repository and workflow
    name, emitted with the same daily sampler. Execution percentiles omit
    zombie wall-clock artifacts and zero-execution runs rather than treating
    an empty population as a real zero.
    """

    repo: str
    workflow: str
    runs: int
    failed_runs: int
    self_healed_runs: int
    retried_failed_runs: int
    cancelled_runs: int
    superseded_runs: int
    instant_skip_runs: int
    prs_appeared_on: int
    pr_appearance_share: NotRequired[float]
    retry_share: NotRequired[float]
    exec_median_seconds: NotRequired[float]
    exec_p90_seconds: NotRequired[float]


class CiRunsRun(TypedDict):
    """`ci_runs_run` payload — scripts/ci/runs_export.py.

    One sampler breadcrumb per repository: the fixed window's population and
    this run's GitHub-read budget. These are current observations, so all
    numbers are gauges even though their names are counts.
    """

    repo: str
    window_days: int
    window_runs: int
    window_prs: int
    api_requests: int


class TaskReminderDigest(TypedDict):
    """`task_reminder_digest` payload — task-maintenance daemon."""

    owner_id: int
    task_count: int
    task_ids: list[int]


class TaskEscalation(TypedDict):
    """`task_escalation` payload — task-maintenance daemon."""

    owner_id: int
    task_count: int
    task_ids: list[int]
    leg: Literal["delegator", "user"]


class HierarchyEnqueueFailed(TypedDict):
    """`hierarchy_enqueue_failed` payload — the compact-boundary build enqueue
    did not land (task #4674). The enqueue is best-effort by design: the
    compact round proceeds and the reconcile scan backstops, so this event is
    the observability signal that the event trigger is degraded."""

    agent_id: int
    error: str


class HierarchyRegenAlert(TypedDict):
    """`hierarchy_regen_alert` payload — one build job generated more nodes
    than the alert threshold (task #4674 guardrail; non-blocking)."""

    agent_id: int
    job_id: int
    generated: int
    threshold: int


class HierarchyRegenHalt(TypedDict):
    """`hierarchy_regen_halt` payload — generation stopped mid-run at the halt
    threshold; the remainder is skipped and the continuation waits out the
    retry backoff (task #4674 guardrail)."""

    agent_id: int
    job_id: int
    generated: int
    threshold: int


class HierarchyRegenBudgetTripped(TypedDict):
    """`hierarchy_regen_budget_tripped` payload — the fleet's 24h generated
    total crossed the daily budget and the worker stopped claiming until an
    operator resets the breaker with a note (task #4674 guardrail)."""

    window_nodes: int
    budget_nodes: int


class HierarchyRegenLowReuse(TypedDict):
    """`hierarchy_regen_low_reuse` payload — one build job reused almost none
    of an established tree's texts, the shape of a full re-cut (task #4674
    guardrail)."""

    agent_id: int
    job_id: int
    generated: int
    reused: int


EVENTS: dict[str, EventSpec] = {
    "ci_usage_daily": telemetry_event(
        "ci_usage_daily",
        "daily CI-minute reconciliation totals (C9)",
        payload=CiUsageDaily,
        site="schedules/c9-daily-report-schedule.py:_fire (positional emit)",
    ),
    "debt_sweep_daily": telemetry_event(
        "debt_sweep_daily",
        "daily tech-debt mechanical scan and clearing-worker dispatch",
        payload=DebtSweepDaily,
        site="schedules/debt-sweep-daily-schedule.py:_fire (positional emit)",
    ),
    "pr_flow_daily": telemetry_event(
        "pr_flow_daily",
        "daily PR-flow aggregates — ready->merged percentiles and "
        "flake discoveries (absolute gauges, one sample per complete day)",
        payload=PrFlowDaily,
        site="scripts/pr_flow_export.py:_emit_events (positional emit)",
    ),
    "pr_flow_run": telemetry_event(
        "pr_flow_run",
        "PR-flow sampler run — point-in-time Trunk queue depth (absolute state)",
        payload=PrFlowRun,
        site="scripts/pr_flow_export.py:_emit_events (positional emit)",
    ),
    "ci_runs_daily": telemetry_event(
        "ci_runs_daily",
        "daily CI-run aggregates",
        payload=CiRunsDaily,
        site="scripts/ci/runs_export.py:emit_snapshot (positional emit)",
    ),
    "ci_workflow_window": telemetry_event(
        "ci_workflow_window",
        "trailing workflow fragility",
        payload=CiWorkflowWindow,
        site="scripts/ci/runs_export.py:emit_snapshot (positional emit)",
    ),
    "ci_runs_run": telemetry_event(
        "ci_runs_run",
        "CI-run sampler breadcrumb",
        payload=CiRunsRun,
        site="scripts/ci/runs_export.py:emit_snapshot (positional emit)",
    ),
    "task_reminder_digest": telemetry_event(
        "task_reminder_digest",
        "overdue-task owner digest",
        payload=TaskReminderDigest,
        tier="noise",
        site="task_maintenance/daemon.py:_run_reminders",
    ),
    "task_escalation": telemetry_event(
        "task_escalation",
        "stalled-task escalation",
        payload=TaskEscalation,
        site="task_maintenance/daemon.py:_run_escalate",
    ),
    "task_usage_record_failed": telemetry_event(
        "task_usage_record_failed", "task usage recording failed", tier="anomaly"
    ),
    # labeler / trace housekeeping
    "label_generated": telemetry_event("label_generated", "label auto-generated", tier="noise"),
    "label_generate_failed": telemetry_event(
        "label_generate_failed", "label generation failed", tier="anomaly"
    ),
    "label_generate_skipped": telemetry_event(
        "label_generate_skipped", "label generation skipped", tier="noise"
    ),
    "label_generate_empty": telemetry_event(
        "label_generate_empty", "label generation empty", tier="noise"
    ),
    "label_generate_rejected": telemetry_event(
        "label_generate_rejected", "label generation rejected as not a label", tier="noise"
    ),
    "label_generate_retired": telemetry_event(
        "label_generate_retired",
        "label generation given up on after repeated failures",
        tier="noise",
    ),
    # hierarchy regen guardrails (task #4674): the understanding-tree build
    # queue's cost breakers — a reader fix that invalidated every input hash
    # turned into a fleet-wide full re-cut, so repair waves are bounded by
    # explicit config and made visible on the stream.
    "hierarchy_enqueue_failed": telemetry_event(
        "hierarchy_enqueue_failed",
        "a compact-boundary build job could not be enqueued (best-effort; the reconcile scan backstops)",
        payload=HierarchyEnqueueFailed,
        tier="anomaly",
        site="base/agents/history/checkpoint_cleanup.py:_enqueue_failed",
    ),
    "hierarchy_regen_alert": telemetry_event(
        "hierarchy_regen_alert",
        "one build job generated more nodes than the alert threshold (observability only)",
        payload=HierarchyRegenAlert,
        tier="anomaly",
        site="services/derived/hierarchy_worker/execute.py:_try_emit",
    ),
    "hierarchy_regen_halt": telemetry_event(
        "hierarchy_regen_halt",
        "generation stopped mid-run at the halt threshold; the remainder is skipped and the continuation waits out the backoff",
        payload=HierarchyRegenHalt,
        tier="anomaly",
        site="services/derived/hierarchy_worker/execute.py:_try_emit",
    ),
    "hierarchy_regen_budget_tripped": telemetry_event(
        "hierarchy_regen_budget_tripped",
        "the 24h fleet-wide generated-node total crossed the daily budget; the worker stopped claiming until an operator resets the breaker",
        payload=HierarchyRegenBudgetTripped,
        tier="anomaly",
        site="services/derived/hierarchy_worker/runner.py:_regen_budget_check",
    ),
    "hierarchy_regen_low_reuse": telemetry_event(
        "hierarchy_regen_low_reuse",
        "one build job reused almost none of an established tree's texts — the shape of a full re-cut",
        payload=HierarchyRegenLowReuse,
        tier="anomaly",
        site="services/derived/hierarchy_worker/execute.py:_try_emit",
    ),
}
