"""Registry-defined Grafana panels for the daily CI-run sampler."""

from __future__ import annotations

from typing import Any

from shared.metrics.core import core_metrics
from shared.plugin_metrics import MetricSpec


def _day_query(field: str) -> str:
    """One latest absolute gauge per complete cluster-time day."""
    return f"max by (day) (last_over_time(ava_ci_runs_daily_{field}_ratio[26h]))"


def _workflow_query(field: str) -> str:
    """One latest absolute gauge per workflow in the trailing window."""
    return f"max by (workflow) (last_over_time(ava_ci_workflow_window_{field}_ratio[26h]))"


def _day_transformations(names: list[str]) -> list[dict[str, Any]]:
    """Join instant Prometheus targets into one Grafana table row per day."""
    exclusions = {f"Time {index}": True for index in range(1, len(names) + 1)}
    return [
        {"id": "joinByField", "options": {"byField": "day", "mode": "outer"}},
        {
            "id": "organize",
            "options": {
                "excludeByName": exclusions,
                "renameByName": {
                    f"Value #{chr(ord('A') + index)}": name for index, name in enumerate(names)
                },
            },
        },
    ]


def _workflow_transformations(names: list[str]) -> list[dict[str, Any]]:
    """Join instant Prometheus targets into one Grafana table row per workflow."""
    exclusions = {f"Time {index}": True for index in range(1, len(names) + 1)}
    return [
        {"id": "joinByField", "options": {"byField": "workflow", "mode": "outer"}},
        {
            "id": "organize",
            "options": {
                "excludeByName": exclusions,
                "renameByName": {
                    f"Value #{chr(ord('A') + index)}": name for index, name in enumerate(names)
                },
            },
        },
    ]


_DURATION_NAMES = ["median", "p90"]
core_metrics.register_core_metric(
    MetricSpec(
        name="core_ci_per_pr_duration",
        title="Per-PR CI duration — median / p90 (by day)",
        description=(
            "Attributed CI wall time per completed PR by complete cluster-time day: median and "
            "p90 minutes. Zombie wall-clock artifacts, instant skips, and watchdog reruns are "
            "excluded from duration sums."
        ),
        event_name="ci_runs_daily",
        category="telemetry",
        unit="m",
        panel="table",
        query=_day_query("per_pr_duration_median_minutes"),
        query_type="promql",
        targets=[_day_query("per_pr_duration_p90_minutes")],
        target_names=_DURATION_NAMES,
        thresholds=[],
        panel_id=2501,
        section="Dev/CI",
        order=0,
        transformations=_day_transformations(_DURATION_NAMES),
    )
)

_RUN_NAMES = ["median", "p90", "median executed"]
core_metrics.register_core_metric(
    MetricSpec(
        name="core_ci_per_pr_runs",
        title="Per-PR CI runs — median / p90 (by day)",
        description=(
            "Attributed CI trigger count per completed PR by complete cluster-time day. "
            "The executed median filters instant-skip, watchdog, QA-gate, and proof workflows."
        ),
        event_name="ci_runs_daily",
        category="telemetry",
        unit="short",
        panel="table",
        query=_day_query("per_pr_runs_median"),
        query_type="promql",
        targets=[_day_query("per_pr_runs_p90"), _day_query("per_pr_runs_executed_median")],
        target_names=_RUN_NAMES,
        thresholds=[],
        panel_id=2502,
        section="Dev/CI",
        order=1,
        transformations=_day_transformations(_RUN_NAMES),
    )
)

_FAILURE_NAMES = ["failed", "self-healed", "failed after retry", "first-pass share"]
core_metrics.register_core_metric(
    MetricSpec(
        name="core_ci_failures",
        title="CI failures & self-healing (by day)",
        description=(
            "CI-red, rerun-to-green, failed retry, and v1 first-pass PR share by complete "
            "cluster-time day. First pass excludes watchdog, QA-gate, proof, instant-skip, and zombie runs."
        ),
        event_name="ci_runs_daily",
        category="telemetry",
        unit="short",
        panel="table",
        query=_day_query("failed_runs"),
        query_type="promql",
        targets=[
            _day_query("self_healed_runs"),
            _day_query("retried_failed_runs"),
            _day_query("first_pass_pr_share"),
        ],
        target_names=_FAILURE_NAMES,
        thresholds=[],
        panel_id=2503,
        section="Dev/CI",
        order=2,
        transformations=_day_transformations(_FAILURE_NAMES),
    )
)

_FRAGILITY_NAMES = ["failed", "runs", "PR appearance share", "self-healed", "retry share"]
core_metrics.register_core_metric(
    MetricSpec(
        name="core_ci_workflow_fragility",
        title="Most fragile workflows (trailing window)",
        description=(
            "Trailing-window workflow ranking by failures, run volume, completed-PR appearance, "
            "self-healing, and retry share. The explicit watchdog label keeps its designed noise filterable."
        ),
        event_name="ci_workflow_window",
        category="telemetry",
        unit="short",
        panel="table",
        query=_workflow_query("failed_runs"),
        query_type="promql",
        targets=[
            _workflow_query("runs"),
            _workflow_query("pr_appearance_share"),
            _workflow_query("self_healed_runs"),
            _workflow_query("retry_share"),
        ],
        target_names=_FRAGILITY_NAMES,
        thresholds=[],
        panel_id=2504,
        section="Dev/CI",
        order=3,
        transformations=_workflow_transformations(_FRAGILITY_NAMES),
    )
)

_WHITE_NAMES = ["white share", "instant skip", "superseded", "abandoned", "retry share"]
core_metrics.register_core_metric(
    MetricSpec(
        name="core_ci_white_runs",
        title="White-run classes (by day)",
        description=(
            "The deduplicated white-run share and its instant-skip, superseded, abandoned, and retry "
            "components by complete cluster-time day."
        ),
        event_name="ci_runs_daily",
        category="telemetry",
        unit="short",
        panel="table",
        query=_day_query("white_run_share"),
        query_type="promql",
        targets=[
            _day_query("instant_skip_runs"),
            _day_query("superseded_runs"),
            _day_query("abandoned_runs"),
            _day_query("retry_share"),
        ],
        target_names=_WHITE_NAMES,
        thresholds=[],
        panel_id=2505,
        section="Dev/CI",
        order=4,
        transformations=_day_transformations(_WHITE_NAMES),
    )
)
