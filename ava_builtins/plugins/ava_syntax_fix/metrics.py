"""ava_syntax_fix Grafana metrics — declared, not registered.

``base/telemetry/metrics/grafana_dashboard_supply`` imports this module and takes its
``contribute()`` declaration for the rendered Ava Ops dashboard (``ava lgtm render``,
task #3697); the plugin name comes from the registry entry. Query templates target the unified event stream in Loki
(task #180: the PG ``events`` table was frozen at the LGTM cutover and
dropped with the archive cleanup — every metric reads the event stream
through LogQL, the same read the core panels use, task #1280).

Query dialect (task #1280): each template selects
``{service_name="unknown_service"}`` (the unified emitter's OTLP resource),
pipelines ``| json`` (event fields are structured metadata, NOT stream
labels), and filters on the flattened labels. Stat panels run as instant
queries over ``[$__range]`` (the whole panel window); timeseries panels use
a fixed ``[5m]`` window. Every count wraps in
``sum(...)``: the unknown_service family has >500 streams over a day, and an
unaggregated count_over_time hits Loki's per-query series cap.

Data provenance: ``syntax_fix`` events carry a ``fixes`` attribute (comma
list, e.g. ``"ruff_format"``) and are written with ``category='telemetry'`` —
``syntax_fix`` is in ``base/telemetry/emitter.py``'s telemetry event set
(event_name-category final convention, 2026-08-05, tracker #762), so
``category_for_kind`` maps it to ``telemetry`` (90d retention). The category
predicate keeps the ``|log`` alternative for pre-convention rows (the core
panels' pattern); the pre-convention PG rows were backfilled by the
accompanying migration, Loki rows keep their emit-time category.
"""

from base.packages.plugins.extensions import PluginContributions
from base.telemetry.metrics.logql import CATEGORY_WITH_LEGACY_LOG, event_count
from base.telemetry.metrics.plugin_metrics import MetricSpec

METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        name="ava_syntax_fix_count",
        title="Syntax fix count",
        time_basis="per_minute",
        description=(
            "Syntax_fix events per minute (5-minute buckets / 5) — how often "
            "the repair pipeline fixes syntax errors in LLM-produced code "
            "(event_name='syntax_fix', category='telemetry', 90d retention)."
        ),
        event_name="syntax_fix",
        category="telemetry",
        unit="short",
        panel="timeseries",
        query=event_count(CATEGORY_WITH_LEGACY_LOG, "5m", matchers="event_name={event_name}"),
        query_type="logql",
        target_names=["fixes"],
        output=["grafana"],
    ),
    MetricSpec(
        name="ava_syntax_fix_total",
        title="Syntax fixes",
        time_basis="window",
        description=(
            "Total syntax_fix events in the current window (event_name='syntax_fix', "
            "category='telemetry')."
        ),
        event_name="syntax_fix",
        category="telemetry",
        unit="short",
        panel="stat",
        query=event_count(CATEGORY_WITH_LEGACY_LOG, "$__range", matchers="event_name={event_name}"),
        query_type="logql",
        target_names=["fixes"],
        output=["grafana"],
    ),
)


def contribute() -> PluginContributions:
    """What this plugin declares for the metric surfaces (Grafana dashboard, agent inspector)."""
    return PluginContributions(metrics=METRICS)
