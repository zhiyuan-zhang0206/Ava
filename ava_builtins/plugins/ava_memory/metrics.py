"""ava_memory Grafana + inspector metrics — declared, not registered.

The processes that serve metrics import this module and take its ``contribute()``
declaration; the plugin name comes from the registry entry. Query templates target the unified event stream in Loki
(task #180: the PG ``events`` table was frozen at the LGTM cutover and
dropped with the archive cleanup — every metric reads the event stream
through LogQL, the same read the core panels use, task #1280).

Query dialect (task #1280): each template selects
``{service_name="unknown_service"}`` (the unified emitter's OTLP resource),
pipelines ``| json`` (event fields are structured metadata, NOT stream
labels), and filters on the flattened labels. Stat panels run as instant
queries over ``[$__range]`` (the whole panel window); Grafana timeseries
panels use a fixed ``[5m]`` window. Every count wraps in
``sum(...)``: the unknown_service family has >500 streams over a day, and an
unaggregated count_over_time hits Loki's per-query series cap.

Data provenance: every metric reads the ``passive_recall`` event
(agent/graph/recall/memory_recall.py), emitted with ``category='telemetry'``
(90d retention). A completed recall pass is an INFO row carrying ``search_ms``
(the search request, which includes the gateway's ranking of the retrieved
chunks) and ``injected`` (how many notes cleared the score threshold). A pass
that ran out of its deadline is an INFO row whose ``body`` starts
``skip: recall exceeded`` and carries neither number.
"""

from base.events.contract import PASSIVE_RECALL_KEYS
from base.packages.plugins.extensions import PluginContributions
from base.telemetry.metrics.logql import CATEGORY_MATCH, event_count
from base.telemetry.metrics.plugin_metrics import MetricSpec, ThresholdStep

# Attribute labels are derived from the payload-key contract (a renamed
# payload key fails loudly here instead of silently NULLing out) — the same
# pattern the core panels use (_LLM_ATTR etc.).
_PASSIVE_RECALL_ATTR = {k: f"attributes_{k}" for k in PASSIVE_RECALL_KEYS}


_COMPLETED = f'{CATEGORY_MATCH} | level="info" | {_PASSIVE_RECALL_ATTR["search_ms"]}!=""'
"""Pipeline selecting the passive-recall rows of completed passes."""

_DEADLINE_SKIP = (
    f'{CATEGORY_MATCH} | level="info" | {_PASSIVE_RECALL_ATTR["body"]} =~ "skip: recall exceeded.*"'
)
"""Pipeline selecting the passive-recall rows of passes that ran out of their deadline."""


def _passive_recall_average(field: str) -> str:
    """Average one numeric field of completed recall passes in a five-minute bucket."""
    attr = _PASSIVE_RECALL_ATTR[field]
    successful = f'{CATEGORY_MATCH} | level="info" | {attr}!=""'
    numerator = (
        f'sum(sum_over_time({{service_name="unknown_service", event_name={{event_name}}}} | json | '
        f"{successful} | unwrap {attr} [5m]))"
    )
    denominator = event_count(successful, "5m", matchers="event_name={event_name}")
    return f"{numerator} / {denominator}"


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        name="ava_memory_recall_runs",
        title="Memory recall runs",
        time_basis="per_minute",
        description=(
            "Completed passive-recall passes per minute (5-minute buckets / 5) — how "
            "often recall searches the memory pool and scores the results "
            "(event_name='passive_recall', level='info', category='telemetry', 90d "
            "retention)."
        ),
        event_name="passive_recall",
        category="telemetry",
        unit="ops",
        panel="timeseries",
        query=event_count(_COMPLETED, "5m", matchers="event_name={event_name}"),
        query_type="logql",
        target_names=["runs"],
        output=["grafana"],
        order=0,
    ),
    MetricSpec(
        name="ava_memory_recall_search_latency_ms",
        title="Memory recall search latency",
        description=(
            "Average successful passive-recall search duration in milliseconds — the "
            "search request including the gateway's ranking of the retrieved chunks "
            "(5-minute buckets; event_name='passive_recall', category='telemetry')."
        ),
        event_name="passive_recall",
        category="telemetry",
        unit="ms",
        panel="timeseries",
        query=_passive_recall_average("search_ms"),
        query_type="logql",
        target_names=["avg search ms"],
        output=["grafana", "inspector"],
        order=4,
    ),
    MetricSpec(
        name="ava_memory_recall_injected_notes",
        title="Memory recall injected notes",
        description=(
            "Average notes injected per completed passive-recall pass (5-minute "
            "buckets; event_name='passive_recall', category='telemetry') — how many "
            "of the searched notes clear the score threshold."
        ),
        event_name="passive_recall",
        category="telemetry",
        unit="short",
        panel="timeseries",
        query=_passive_recall_average("injected"),
        query_type="logql",
        target_names=["avg injected"],
        output=["grafana", "inspector"],
        order=5,
    ),
    MetricSpec(
        name="ava_memory_recall_empty_rate",
        title="Empty recall ratio",
        description=(
            "Share of completed recall passes that injected 0 notes — recall-quality "
            "signal: a high share means no searched note clears the score threshold "
            "(or the threshold is set too high) (event_name='passive_recall', "
            "category='telemetry')."
        ),
        event_name="passive_recall",
        category="telemetry",
        unit="percent",
        panel="timeseries",
        query=(
            f"100 * {event_count(_COMPLETED + ' | ' + _PASSIVE_RECALL_ATTR['injected'] + '="0"', '5m', matchers='event_name={event_name}')}"
            f" / {event_count(_COMPLETED, '5m', matchers='event_name={event_name}')}"
        ),
        query_type="logql",
        target_names=["empty %"],
        output=["grafana", "inspector"],
        order=1,
    ),
    MetricSpec(
        name="ava_memory_recall_deadline_skip_rate",
        title="Recall deadline skip ratio",
        description=(
            "Share of recall passes that ran out of memory_recall_deadline_seconds and "
            "skipped injection for that turn — a spiking rate points at a slow search "
            "endpoint or ranking model (event_name='passive_recall', "
            "category='telemetry')."
        ),
        event_name="passive_recall",
        category="telemetry",
        unit="percent",
        panel="timeseries",
        thresholds=[
            ThresholdStep(color="yellow", value=15.0),
            ThresholdStep(color="red", value=25.0),
        ],
        query=(
            f"100 * {event_count(_DEADLINE_SKIP, '5m', matchers='event_name={event_name}')}"
            f" / ({event_count(_COMPLETED, '5m', matchers='event_name={event_name}')}"
            f" + {event_count(_DEADLINE_SKIP, '5m', matchers='event_name={event_name}')})"
        ),
        query_type="logql",
        target_names=["skipped %"],
        output=["grafana"],
        order=2,
    ),
    MetricSpec(
        name="ava_memory_recall_deadline_skips",
        title="Recall deadline skips",
        time_basis="window",
        description=(
            "Total recall passes that ran out of their deadline in the current window "
            "— each left that turn without recall (event_name='passive_recall', "
            "category='telemetry')."
        ),
        event_name="passive_recall",
        category="telemetry",
        unit="short",
        panel="stat",
        query=event_count(_DEADLINE_SKIP, "$__range", matchers="event_name={event_name}"),
        query_type="logql",
        target_names=["skips"],
        output=["grafana"],
        order=3,
    ),
    MetricSpec(
        name="ava_memory_recall_agent_runs",
        title="Agent recall runs",
        description=(
            "Inspector-only: the same runs query parameterized by agent. The "
            '{{agent_id}} placeholder is rendered by the gateway as agent_id="<n>"; '
            "metrics carrying the placeholder must not also be emitted to Grafana "
            "panels."
        ),
        event_name="passive_recall",
        category="telemetry",
        unit="ops",
        panel="timeseries",
        query=event_count(
            f"{_COMPLETED} | {{{{agent_id}}}}",
            "$__interval",
            matchers="event_name={event_name}",
        ),
        query_type="logql",
        target_names=["runs"],
        output=["inspector"],
    ),
)


def contribute() -> PluginContributions:
    """What this plugin declares for the metric surfaces (Grafana dashboard, agent inspector)."""
    return PluginContributions(metrics=METRICS)
