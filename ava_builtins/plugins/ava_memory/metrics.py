"""ava_memory Grafana + inspector metrics — registered at import time.

``scripts/gen_plugin_dashboard.py`` imports this module (inside a
PluginContext) to collect the registrations below; the plugin name comes from
the context. Query templates target the unified event stream in Loki
(task #180: the PG ``events`` table is a frozen archive since the LGTM
cutover — every metric reads the event stream through LogQL, the same read
the core panels use, task #1280).

Query dialect (task #1280): each template selects
``{service_name="unknown_service"}`` (the unified emitter's OTLP resource),
pipelines ``| json`` (event fields are structured metadata, NOT stream
labels), and filters on the flattened labels. Stat panels run as instant
queries over ``[$__range]`` (the whole panel window); Grafana timeseries
panels use a fixed ``[5m]`` window. Every count wraps in
``sum(...)``: the unknown_service family has >500 streams over a day, and an
unaggregated count_over_time hits Loki's per-query series cap.

Data provenance: the only memory-domain event in the stream is
``recall_filter`` (agent/graph/_memory_filter.py, the passive-recall relevance
filter), emitted with an explicit ``event="recall_filter"`` and mapped to
category='telemetry' (90d retention; event_name-category final convention, 2026-08-05,
tracker #762 — the metrics below were moved off the legacy ``recall-filter``
spelling + category='log' pair that stopped matching the stream after the W8
rename). Two levels carry distinct meaning:

- INFO — the filter judged N candidates: body ``"N candidate(s) -> M kept"``.
  M=0 means nothing was judged relevant enough to inject (an empty recall).
- WARNING — the filter failed (model error / timeout / unparseable reply /
  invented path): recall injected nothing that turn.

The kept count lives in an unstructured body string, so the whitelist-safe
signal for recall quality is the Loki regex match ``attributes_body =~ ".*-> 0 kept.*"``
over INFO rows (the body label is the json-flattened ``attributes.body``) —
the empty-recall share, reported as its inverse framing (the hit-rate proxy).
"""

from shared.events.contract import RECALL_FILTER_KEYS
from shared.plugin_metrics import MetricSpec, ThresholdStep, register_metric

# The event stream + json pipeline every template starts with. The selector
# matches the unified emitter's OTLP resource (gateway/loki_events._SELECTOR).
_SEL = '{service_name="unknown_service"} | json'

# Attribute labels are derived from the payload-key contract (a renamed
# payload key fails loudly here instead of silently NULLing out) — the same
# pattern the core panels use (_LLM_ATTR etc.).
_RECALL_ATTR = {k: f"attributes_{k}" for k in RECALL_FILTER_KEYS}

# Category filter: keep the |log alternative for pre-convention rows (the
# core panels' pattern). {category_re} renders the category UNQUOTED for the
# regex.
_CAT = 'category=~"{category_re}|log"'


def _count(pipeline: str, window: str) -> str:
    """One count_over_time series — every count wraps in sum(...) (see the
    module docstring for the series-cap note)."""
    return f"sum(count_over_time({_SEL} | {pipeline} [{window}]))"


register_metric(
    MetricSpec(
        name="ava_memory_recall_filter_runs",
        title="Memory recall filter runs",
        description=(
            "Recall_filter INFO events over time — how often passive memory recall "
            "filters retrieval results for relevance (event_name='recall_filter', "
            "level='info', category='telemetry', 90d retention)."
        ),
        event_name="recall_filter",
        category="telemetry",
        unit="ops",
        panel="timeseries",
        query=_count(f'{_CAT} | event_name={{event_name}} | level="info"', "5m"),
        query_type="logql",
        target_names=["runs"],
        output=["grafana"],
    )
)

register_metric(
    MetricSpec(
        name="ava_memory_recall_filter_empty_rate",
        title="Empty recall ratio",
        description=(
            "Share of INFO runs where filtering kept 0 notes (body 'N candidate(s) -> 0 "
            "kept') — recall-quality signal: a high share means retrieval/filtering "
            "usually judges nothing relevant (event_name='recall_filter', "
            "category='telemetry')."
        ),
        event_name="recall_filter",
        category="telemetry",
        unit="percent",
        panel="timeseries",
        query=(
            f"100 * {
                _count(
                    _CAT
                    + ' | event_name={event_name} | level="info" | '
                    + _RECALL_ATTR['body']
                    + ' =~ ".*-> 0 kept.*"',
                    '5m',
                )
            }"
            f" / {_count(_CAT + ' | event_name={event_name} | level="info"', '5m')}"
        ),
        query_type="logql",
        target_names=["empty %"],
        output=["grafana", "inspector"],
    )
)

register_metric(
    MetricSpec(
        name="ava_memory_recall_filter_anomaly_rate",
        title="Recall filter error ratio",
        description=(
            "Share of recall_filter WARNINGs across all runs — when the filter call "
            "fails (model error / timeout / unparseable reply) the round's recall "
            "injection is empty; a spiking rate points at the filter model or "
            "provider (event_name='recall_filter', category='telemetry')."
        ),
        event_name="recall_filter",
        category="telemetry",
        unit="percent",
        panel="timeseries",
        thresholds=[
            ThresholdStep(color="yellow", value=15.0),
            ThresholdStep(color="red", value=25.0),
        ],
        query=(
            f"100 * {_count(_CAT + ' | event_name={event_name} | level="warning"', '5m')}"
            f" / {_count(_CAT + ' | event_name={event_name}', '5m')}"
        ),
        query_type="logql",
        target_names=["anomaly %"],
        output=["grafana"],
    )
)

register_metric(
    MetricSpec(
        name="ava_memory_recall_filter_failures",
        title="Recall filter failures (window)",
        description=(
            "Total recall_filter WARNINGs in the current window — filter unavailable, "
            "an unparseable reply, or an unknown model path all leave the round "
            "without injection (event_name='recall_filter', category='telemetry')."
        ),
        event_name="recall_filter",
        category="telemetry",
        unit="short",
        panel="stat",
        query=_count(f'{_CAT} | event_name={{event_name}} | level="warning"', "$__range"),
        query_type="logql",
        target_names=["failures"],
        output=["grafana"],
    )
)

register_metric(
    MetricSpec(
        name="ava_memory_recall_filter_agent_runs",
        title="Agent recall filter runs",
        description=(
            "Inspector-only: the same runs query parameterized by agent. The "
            '{{agent_id}} placeholder is rendered by the gateway as agent_id="<n>"; '
            "metrics carrying the placeholder must not also be emitted to Grafana "
            "panels."
        ),
        event_name="recall_filter",
        category="telemetry",
        unit="ops",
        panel="timeseries",
        query=_count(
            f'{_CAT} | event_name={{event_name}} | level="info" | {{{{agent_id}}}}',
            "$__interval",
        ),
        query_type="logql",
        target_names=["runs"],
        output=["inspector"],
    )
)
