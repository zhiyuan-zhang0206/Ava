"""ava_fleet Grafana + inspector metrics — declared, not registered.

``base/telemetry/metrics/grafana_dashboard_supply`` imports this module and takes its
``contribute()`` declaration for the rendered Ava Ops
dashboard (``ava lgtm render``, task #3697). Two metrics:

- ``ava_fleet_task_done_rate`` — dual-surface (grafana + inspector): a
  cluster-wide task-completion rate panel, and the same query the inspector
  surface renders per agent.
- ``ava_fleet_agent_task_done_rate`` — inspector-only: demonstrates the
  ``{{agent_id}}`` placeholder semantics reserved in the registry snapshot
  (the gateway renders it to ``agent_id="<n>"``); it is exported to
  the registry JSON but never becomes a Grafana panel.

Query dialect (task #1280 / #180): every metric reads the event stream from
Loki (the PG ``events`` table was frozen at the LGTM cutover and later
dropped) —
``{service_name="unknown_service"} | json`` then label filters on the
flattened event fields; the same read the core panels and the alert rules
use. Stat panels run as instant queries over ``[$__range]`` (the whole panel
window); Grafana timeseries use a fixed ``[5m]`` window. Every count wraps in
``sum(...)``: the unknown_service
family has >500 streams over a day, and an unaggregated count_over_time hits
Loki's per-query series cap.

Data provenance (verified against the live DB, 2026-08-04): ``task_update``
is category='audit' with a ``status`` attribute present only when the status
changed — the Loki presence filter is ``attributes_status != ""`` (the
json-extracted label exists only on lines that carry the field); ``spawn`` is
category='audit' (~1.1k rows/30d). ``audit`` never had a ``log`` phase, so
the category predicate is exact (no ``|log`` alternative).
"""

from base.events.contract import TASK_UPDATE_KEYS
from base.packages.plugins.extensions import PluginContributions
from base.telemetry.metrics.logql import event_count
from base.telemetry.metrics.plugin_metrics import MetricSpec

# Attribute labels are derived from the payload-key contract (a renamed
# payload key fails loudly here instead of silently NULLing out) — the same
# pattern the core panels use (_LLM_ATTR etc.).
_TASK_ATTR = {k: f"attributes_{k}" for k in TASK_UPDATE_KEYS}


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        name="ava_fleet_task_done_rate",
        title="Task completion rate",
        description=(
            "Share of task_update events with status='done' — only updates carrying "
            "a status count toward the denominator (event_name='task_update', "
            "category='audit')."
        ),
        event_name="task_update",
        category="audit",
        unit="percent",
        panel="timeseries",
        query=(
            f"100 * {
                event_count(
                    'category={category} | ' + _TASK_ATTR['status'] + '="done"',
                    '5m',
                    matchers='event_name={event_name}',
                )
            }"
            f" / {
                event_count(
                    'category={category} | ' + _TASK_ATTR['status'] + '!=""',
                    '5m',
                    matchers='event_name={event_name}',
                )
            }"
        ),
        query_type="logql",
        target_names=["done %"],
        output=["grafana", "inspector"],
    ),
    MetricSpec(
        name="ava_fleet_agent_task_done_rate",
        title="Agent task completion rate",
        description=(
            "Inspector-only: the same query parameterized by agent. The {{agent_id}} "
            'placeholder is rendered by the gateway as agent_id="<n>"; metrics '
            "carrying the placeholder must not also be emitted to Grafana panels."
        ),
        event_name="task_update",
        category="audit",
        unit="percent",
        panel="timeseries",
        query=(
            f"100 * {
                event_count(
                    'category={category} | ' + _TASK_ATTR['status'] + '="done" | {{agent_id}}',
                    '$__interval',
                    matchers='event_name={event_name}',
                )
            }"
            f" / {
                event_count(
                    'category={category} | ' + _TASK_ATTR['status'] + '!="" | {{agent_id}}',
                    '$__interval',
                    matchers='event_name={event_name}',
                )
            }"
        ),
        query_type="logql",
        target_names=["done %"],
        output=["inspector"],
    ),
)


def contribute() -> PluginContributions:
    """What this plugin declares for the metric surfaces (Grafana dashboard, agent inspector)."""
    return PluginContributions(metrics=METRICS)
