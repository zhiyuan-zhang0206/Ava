"""Core PR-flow panels (task #2139) — a separate registration module.

The four PR-flow tiles read what the daily macmini export job
(``scripts/pr_flow_export.py``, ``pr_flow_daily`` / ``pr_flow_run`` events)
publishes as OTLP gauges through ``shared/telemetry_otlp.py``: one absolute
sample per complete cluster-tz day in a rolling 30-day window, re-emitted on
every run, plus the point-in-time Trunk queue depth.

Every sample is a day-labeled gauge series (``day`` rides along as a datapoint
attribute, alongside the exporter process dimensions). Two query shapes follow
from that:

- ``max by (day)`` — collapse the per-run process/machine multiplicity (and a
  manual re-run) onto one series per day; a plain read would draw several
  overlapping lines per day.
- ``last_over_time(...[26h])`` — the exporter is a one-shot process, so a day
  series only receives a sample while the daily run keeps re-emitting it; 26h
  keeps the last sample alive across the daily cadence (a missed run drops the
  day instead of pinning a stale point).

The panel SQL/JSON mirror lives in
``deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json``
(row "PR flow") and is locked by ``tests/plugins/test_plugin_metrics_logql.py``.
"""

from __future__ import annotations

from shared import core_metrics
from shared.plugin_metrics import MetricSpec

core_metrics.register_core_metric(
    MetricSpec(
        name="core_pr_flow_ready_to_merge",
        title="PR ready to merged \u2014 median / p90 (by day)",
        description=(
            "Ready-for-review to merge latency by complete cluster-tz day "
            "\u2014 median and p90 seconds, sampled by the daily PR-flow export "
            "job (scripts/pr_flow_export.py) into the "
            "ava_pr_flow_daily_ready_to_merge_median_seconds / _p90_seconds "
            "gauges. One row per day; a day leaves the table once its last "
            "sample ages past the 26h lookback (a missed run shows as a gap)."
        ),
        event_name="pr_flow_daily",
        category="telemetry",
        unit="s",
        panel="table",
        query="max by (day) (last_over_time(ava_pr_flow_daily_ready_to_merge_median_seconds[26h]))",
        query_type="promql",
        targets=[
            "max by (day) (last_over_time(ava_pr_flow_daily_ready_to_merge_p90_seconds[26h]))"
        ],
        target_names=["median", "p90"],
        thresholds=[],
        panel_id=2401,
        section="PR flow",
        order=0,
        transformations=[
            {"id": "joinByField", "options": {"byField": "day", "mode": "outer"}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time 1": True, "Time 2": True},
                    "renameByName": {"Value #A": "median", "Value #B": "p90"},
                },
            },
        ],
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_pr_flow_queue_depth",
        title="Trunk queue depth",
        description=(
            "Trunk merge-queue depth sampled once per PR-flow export run "
            "(ava_pr_flow_run_queue_depth_ratio gauge, macmini daily job). The "
            "run event is emitted even when Trunk is unreachable, so the "
            "breadcrumb survives an outage while the depth sample stays absent."
        ),
        event_name="pr_flow_run",
        category="telemetry",
        unit="short",
        panel="timeseries",
        query="ava_pr_flow_run_queue_depth_ratio",
        query_type="promql",
        target_names=["queue depth"],
        panel_id=2402,
        section="PR flow",
        order=1,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_pr_flow_qa_rounds",
        title="QA rounds \u2014 mean / re-review share (by day)",
        description=(
            "QA traffic per merged PR by complete cluster-tz day \u2014 mean "
            "ava-qa receipt count and the share of merges whose head moved "
            "after the receipt (re-review), from the daily PR-flow export job "
            "(ava_pr_flow_daily_qa_rounds_mean_ratio / "
            "ava_pr_flow_daily_qa_rereview_share_ratio gauges)."
        ),
        event_name="pr_flow_daily",
        category="telemetry",
        unit="short",
        panel="table",
        query="max by (day) (last_over_time(ava_pr_flow_daily_qa_rounds_mean_ratio[26h]))",
        query_type="promql",
        targets=["max by (day) (last_over_time(ava_pr_flow_daily_qa_rereview_share_ratio[26h]))"],
        target_names=["mean rounds", "re-review share"],
        thresholds=[],
        panel_id=2403,
        section="PR flow",
        order=2,
        transformations=[
            {"id": "joinByField", "options": {"byField": "day", "mode": "outer"}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time 1": True, "Time 2": True},
                    "renameByName": {"Value #A": "mean rounds", "Value #B": "re-review share"},
                },
            },
        ],
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_pr_flow_flakes",
        title="Flake \u2014 new quarantines (by day)",
        description=(
            "Tests newly quarantined by Trunk\u2019s flaky detector per complete "
            "cluster-tz day (ava_pr_flow_daily_flake_new_quarantines_ratio "
            "gauge, daily PR-flow export job). A day whose flaky source was "
            "unreachable is omitted rather than shown as zero."
        ),
        event_name="pr_flow_daily",
        category="telemetry",
        unit="short",
        panel="table",
        query="max by (day) (last_over_time(ava_pr_flow_daily_flake_new_quarantines_ratio[26h]))",
        query_type="promql",
        target_names=["new quarantines"],
        thresholds=[],
        panel_id=2404,
        section="PR flow",
        order=3,
        transformations=[
            {"id": "joinByField", "options": {"byField": "day", "mode": "outer"}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True},
                    "renameByName": {"Value": "new quarantines"},
                },
            },
        ],
    )
)
