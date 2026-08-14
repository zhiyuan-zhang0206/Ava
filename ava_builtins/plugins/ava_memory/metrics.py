"""ava_memory Grafana + inspector metrics — registered at import time.

``scripts/gen_plugin_dashboard.py`` imports this module (inside a
PluginContext) to collect the registrations below; the plugin name comes from
the context. Query templates target the unified ``events`` table with
``{event_name}`` / ``{category}`` placeholders that the generator renders as
single-quoted literals.

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
signal for recall quality is ``body LIKE '%-> 0 kept%'`` over INFO rows — the
empty-recall share, reported as its inverse framing (the hit-rate proxy).
"""

from shared.events.contract import RECALL_FILTER_KEYS
from shared.plugin_metrics import MetricSpec, ThresholdStep, register_metric

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
        query=(
            'SELECT $__timeGroup(ts, $__interval) AS time, count(*) AS "runs" '
            "FROM events "
            "WHERE event_name = {event_name} AND category = {category} AND level = 'info' "
            "AND $__timeFilter(ts) "
            "GROUP BY 1 ORDER BY 1"
        ),
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
            "SELECT $__timeGroup(ts, $__interval) AS time, "
            "100.0 * count(*) FILTER (WHERE " + RECALL_FILTER_KEYS["body"] + " LIKE '%-> 0 kept%') "
            '/ NULLIF(count(*), 0) AS "empty %" '
            "FROM events "
            "WHERE event_name = {event_name} AND category = {category} AND level = 'info' "
            "AND $__timeFilter(ts) "
            "GROUP BY 1 ORDER BY 1"
        ),
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
            "SELECT $__timeGroup(ts, $__interval) AS time, "
            "100.0 * count(*) FILTER (WHERE level = 'warning') "
            '/ NULLIF(count(*), 0) AS "anomaly %" '
            "FROM events "
            "WHERE event_name = {event_name} AND category = {category} AND $__timeFilter(ts) "
            "GROUP BY 1 ORDER BY 1"
        ),
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
        query=(
            "SELECT count(*) AS failures "
            "FROM events "
            "WHERE event_name = {event_name} AND category = {category} AND level = 'warning' "
            "AND $__timeFilter(ts)"
        ),
        output=["grafana"],
    )
)

register_metric(
    MetricSpec(
        name="ava_memory_recall_filter_agent_runs",
        title="Agent recall filter runs",
        description=(
            "Inspector-only: the same runs query parameterized by agent. The "
            "{{agent_id}} placeholder is rendered by the gateway as agent_id = <n>; "
            "metrics carrying the placeholder must not also be emitted to Grafana "
            "panels."
        ),
        event_name="recall_filter",
        category="telemetry",
        unit="ops",
        panel="timeseries",
        query=(
            'SELECT $__timeGroup(ts, $__interval) AS time, count(*) AS "runs" '
            "FROM events "
            "WHERE event_name = {event_name} AND category = {category} AND level = 'info' "
            "AND {{agent_id}} AND $__timeFilter(ts) "
            "GROUP BY 1 ORDER BY 1"
        ),
        output=["inspector"],
    )
)
