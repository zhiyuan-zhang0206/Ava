"""ava_fleet Grafana + inspector metrics — registered at import time.

``scripts/gen_plugin_dashboard.py`` imports this module (inside a
PluginContext) to collect the registrations. Two metrics:

- ``ava_fleet_task_done_rate`` — dual-surface (grafana + inspector): a
  cluster-wide task-completion rate panel, and the same query the inspector
  surface renders per agent.
- ``ava_fleet_agent_task_done_rate`` — inspector-only: demonstrates the
  ``{{agent_id}}`` placeholder semantics reserved in the registry snapshot
  (the gateway renders it to ``agent_id="<n>"``); it is exported to
  the registry JSON but never becomes a Grafana panel.

Query dialect (task #1280 / #180): every metric reads the event stream from
Loki (the PG ``events`` table is a frozen archive since the LGTM cutover) —
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

from shared.events.contract import TASK_UPDATE_KEYS
from shared.plugin_metrics import MetricSpec, register_metric

# The event stream + json pipeline every template starts with. The selector
# matches the unified emitter's OTLP resource (gateway/loki_events._SELECTOR).
# event_name/agent_id are promoted stream labels (2026-08-23 cutover), so
# event-scoped queries match them inside the selector (via `_count`'s `event`
# matcher); `| json` stays for the level/category/attributes fields.
_SEL = '{service_name="unknown_service"}'

# Attribute labels are derived from the payload-key contract (a renamed
# payload key fails loudly here instead of silently NULLing out) — the same
# pattern the core panels use (_LLM_ATTR etc.).
_TASK_ATTR = {k: f"attributes_{k}" for k in TASK_UPDATE_KEYS}


def _count(pipeline: str, window: str, matchers: str | None = None) -> str:
    """One count_over_time series — every count wraps in sum(...) (see the
    module docstring for the series-cap note). ``matchers`` carries the promoted
    event_name stream-label matcher (e.g. ``'event_name={event_name}'``): it
    is matched inside the stream selector, not after ``| json``."""
    selector = _SEL if matchers is None else f'{{service_name="unknown_service", {matchers}}}'
    return f"sum(count_over_time({selector} | json | {pipeline} [{window}]))"


register_metric(
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
                _count(
                    'category={category} | ' + _TASK_ATTR['status'] + '="done"',
                    '5m',
                    matchers='event_name={event_name}',
                )
            }"
            f" / {
                _count(
                    'category={category} | ' + _TASK_ATTR['status'] + '!=""',
                    '5m',
                    matchers='event_name={event_name}',
                )
            }"
        ),
        query_type="logql",
        target_names=["done %"],
        output=["grafana", "inspector"],
    )
)

register_metric(
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
                _count(
                    'category={category} | ' + _TASK_ATTR['status'] + '="done" | {{agent_id}}',
                    '$__interval',
                    matchers='event_name={event_name}',
                )
            }"
            f" / {
                _count(
                    'category={category} | ' + _TASK_ATTR['status'] + '!="" | {{agent_id}}',
                    '$__interval',
                    matchers='event_name={event_name}',
                )
            }"
        ),
        query_type="logql",
        target_names=["done %"],
        output=["inspector"],
    )
)
