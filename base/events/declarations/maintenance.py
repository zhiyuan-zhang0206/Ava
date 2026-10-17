from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


# meiva:note
# One ci_usage_daily event per reconciliation day (05:00-05:00 cluster time): the CI workflow's runs and ceil-billed minutes for the window, split by attribution and OS; the attribution source is the legacy [Meiva-<id>] prefix, read from PR titles and commit subjects only when present (new PR titles carry none — user ruling 2026-10-16). est_usd is the private-repo overage equivalent at the GitHub-hosted rates; the repo is currently public, so minutes are the billing fact. Per-agent detail lives in the attribution ledger, not here.
# meiva:link base/events/docs/maintenance-payloads.meiva.okf.md
class CiUsageDaily(TypedDict):
    day: str
    window_start: str
    window_end: str
    runs: int
    attributed_runs: int
    unattributed_runs: int
    total_minutes: int
    attributed_minutes: int
    linux_minutes: int
    macos_minutes: int
    appended_runs: int
    est_usd: float


class DebtSweepDaily(TypedDict):
    day: str
    scan_status: str
    action: str
    worker_agent_id: int


# meiva:note
# PrFlowDaily is one event per complete cluster-tz day in the trailing window, re-emitted on every run so the whole window stays inside Prometheus retention; every numeric field is absolute per-day state (an ObservableGauge), never a sum, and fields are absent when the day has no such sample (no merges -> no percentiles; an unreachable flaky source omits flake_new_quarantines rather than claiming zero).
# meiva:link base/events/docs/maintenance-payloads.meiva.okf.md
class PrFlowDaily(TypedDict):
    day: str
    merged_count: int
    ready_to_merge_median_seconds: NotRequired[float]
    ready_to_merge_p90_seconds: NotRequired[float]
    flake_new_quarantines: NotRequired[int]


class PrFlowRun(TypedDict):
    queue_depth: NotRequired[int]


# meiva:note
# One absolute-state ci_runs_daily sample per complete cluster-time day and repository. Classification labels overlap; white_run_share alone deduplicates instant skips, superseded runs and abandoned PR heads in that priority order. Per-PR percentiles are absent when no completed PR has attributed runs. first_pass_pr_share is v1's run-level approximation: attributed non-noise, non-zombie runs have no red conclusion and no retry attempt.
# meiva:link base/events/docs/maintenance-payloads.meiva.okf.md
class CiRunsDaily(TypedDict):
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


# meiva:note
# ci_workflow_window reports trailing-window absolute workflow state keyed by repository and workflow name, emitted with the same daily sampler. Execution percentiles omit zombie wall-clock artifacts and zero-execution runs rather than treating an empty population as a real zero.
# meiva:link base/events/docs/maintenance-payloads.meiva.okf.md
class CiWorkflowWindow(TypedDict):
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
    repo: str
    window_days: int
    window_runs: int
    window_prs: int
    api_requests: int


class TaskReminderDigest(TypedDict):
    owner_id: int
    task_count: int
    task_ids: list[int]


class TaskEscalation(TypedDict):
    owner_id: int
    task_count: int
    task_ids: list[int]
    leg: Literal["delegator", "user"]


class UnderstandingEnqueueFailed(TypedDict):
    agent_id: int
    compact_version: int
    error: str


class UnderstandingChunkFailed(TypedDict):
    agent_id: int
    job_id: int
    attempts: int
    error: str


# meiva:note
# understanding_chunk_gap reports the stretches of a chunk that no node will describe — the runs after the first uncovered run of a chunk that overlapped existing nodes, and the turns a closing chunk's boundary snapshot lacks. The rest of the chunk was described.
# meiva:link base/events/docs/maintenance-payloads.meiva.okf.md
class UnderstandingChunkGap(TypedDict):
    agent_id: int
    job_id: int
    gaps: str


class UnderstandingSnapshotLag(TypedDict):
    agent_id: int
    waited_seconds: float


class UnderstandingChunkSkipped(TypedDict):
    agent_id: int
    job_id: int
    reason: str


class UnderstandingGroupFailed(TypedDict):
    agent_id: int
    level: int
    open_nodes: int
    error: str


class UnderstandingCallRecordFailed(TypedDict):
    agent_id: int
    job_id: int
    error: str


# meiva:note
# understanding_rebuild_failed marks a rebuild closed as failed after its last allowed attempt; the agent's leaves stay as they were (no partial replacement), and the failure is never silent.
# meiva:link base/events/docs/maintenance-payloads.meiva.okf.md
class UnderstandingRebuildFailed(TypedDict):
    agent_id: int
    rebuild_id: int
    attempts: int
    error: str


# meiva:note
# A level-1 node that landed before one that already has a parent reports understanding_leaf_out_of_order; nothing repairs it automatically and the agent's upper levels need a manual build.
# meiva:link base/events/docs/maintenance-payloads.meiva.okf.md
class UnderstandingLeafOutOfOrder(TypedDict):
    agent_id: int
    job_id: int
    span_end: int


# meiva:note
# understanding_backlog samples the chunk queue once per consumer round: rows waiting, rows in flight, and how long the oldest waiting row has waited. A growing age is a consumer that does not keep up.
# meiva:link base/events/docs/maintenance-payloads.meiva.okf.md
class UnderstandingBacklog(TypedDict):
    pending: int
    running: int
    oldest_pending_age_seconds: float


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
        site="scripts/ci/pull_requests/pr_flow_export.py:emit_events (positional emit)",
    ),
    "pr_flow_run": telemetry_event(
        "pr_flow_run",
        "PR-flow sampler run — point-in-time Trunk queue depth (absolute state)",
        payload=PrFlowRun,
        site="scripts/ci/pull_requests/pr_flow_export.py:emit_events (positional emit)",
    ),
    "ci_runs_daily": telemetry_event(
        "ci_runs_daily",
        "daily CI-run aggregates",
        payload=CiRunsDaily,
        site="scripts/ci/pull_requests/runs_export.py:emit_snapshot (positional emit)",
    ),
    "ci_workflow_window": telemetry_event(
        "ci_workflow_window",
        "trailing workflow fragility",
        payload=CiWorkflowWindow,
        site="scripts/ci/pull_requests/runs_export.py:emit_snapshot (positional emit)",
    ),
    "ci_runs_run": telemetry_event(
        "ci_runs_run",
        "CI-run sampler breadcrumb",
        payload=CiRunsRun,
        site="scripts/ci/pull_requests/runs_export.py:emit_snapshot (positional emit)",
    ),
    "task_reminder_digest": telemetry_event(
        "task_reminder_digest",
        "overdue-task owner digest",
        payload=TaskReminderDigest,
        tier="noise",
        site="task_maintenance/daemon.py:run_reminders",
    ),
    "task_escalation": telemetry_event(
        "task_escalation",
        "stalled-task escalation",
        payload=TaskEscalation,
        site="task_maintenance/daemon.py:run_escalate",
    ),
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
    "understanding_enqueue_failed": telemetry_event(
        "understanding_enqueue_failed",
        "a chunk-triggered understanding job could not be enqueued (best-effort; the next trigger covers the same stretch)",
        payload=UnderstandingEnqueueFailed,
        tier="anomaly",
        site="base/agents/history/hierarchy/chunks.py:enqueue_chunk",
    ),
    "understanding_chunk_failed": telemetry_event(
        "understanding_chunk_failed",
        "a queued understanding chunk failed (attempted once, never retried) and stays undescribed",
        payload=UnderstandingChunkFailed,
        tier="anomaly",
        site="base/agents/history/hierarchy/chunk_consumer.py:settle_chunk_job",
    ),
    "understanding_chunk_skipped": telemetry_event(
        "understanding_chunk_skipped",
        "a queued understanding chunk was closed without a node (unsupported provider path or an empty chunk)",
        payload=UnderstandingChunkSkipped,
        tier="anomaly",
        site="base/agents/history/hierarchy/chunk_consumer.py:settle_chunk_job",
    ),
    "understanding_chunk_gap": telemetry_event(
        "understanding_chunk_gap",
        "a chunk was described only in part: the listed stretches (overlap leftovers, turns missing from a closing snapshot) have no node",
        payload=UnderstandingChunkGap,
        tier="anomaly",
        site="base/agents/history/hierarchy/chunk_consumer.py:_report_gaps",
    ),
    "understanding_snapshot_lag": telemetry_event(
        "understanding_snapshot_lag",
        "a compaction stamped its boundary before the checkpoint held the last message (the bounded wait timed out)",
        payload=UnderstandingSnapshotLag,
        tier="anomaly",
        site="agent/hooks/understanding_chunks.py:await_snapshot",
    ),
    "understanding_group_failed": telemetry_event(
        "understanding_group_failed",
        "an upper-level understanding grouping check failed; the level's open nodes stay ungrouped until its next check threshold of new nodes arrives",
        payload=UnderstandingGroupFailed,
        tier="anomaly",
        site="base/agents/history/hierarchy/group_consumer.py:_check_level",
    ),
    "understanding_call_record_failed": telemetry_event(
        "understanding_call_record_failed",
        "the audit rows of an understanding call could not be written; the node or group is kept and only the call record is lost",
        payload=UnderstandingCallRecordFailed,
        tier="anomaly",
        site="base/agents/history/hierarchy/chunks.py:write_chunk_calls",
    ),
    "understanding_rebuild_failed": telemetry_event(
        "understanding_rebuild_failed",
        "an understanding rebuild failed on its last allowed attempt and was closed as failed",
        payload=UnderstandingRebuildFailed,
        tier="anomaly",
        site="base/agents/history/hierarchy/chunk_consumer.py:_process_rebuild",
    ),
    "understanding_leaf_out_of_order": telemetry_event(
        "understanding_leaf_out_of_order",
        "an understanding leaf landed before an already grouped one; the agent's upper levels need a manual build",
        payload=UnderstandingLeafOutOfOrder,
        tier="anomaly",
        site="base/agents/history/hierarchy/chunks.py:write_group_nodes",
    ),
    "understanding_backlog": telemetry_event(
        "understanding_backlog",
        "one sample of the understanding chunk queue per consumer round: pending, running and the oldest pending age",
        payload=UnderstandingBacklog,
        site="base/agents/history/hierarchy/chunk_consumer.py:understanding_loop_forever",
    ),
}
