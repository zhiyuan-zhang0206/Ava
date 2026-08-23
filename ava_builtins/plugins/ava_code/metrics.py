"""ava_code Grafana metrics — registered at import time.

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
queries over ``[$__range]`` (the whole panel window); timeseries panels use
a fixed ``[5m]`` window. Every count wraps in
``sum(...)``: the unknown_service family has >500 streams over a day, and an
unaggregated count_over_time hits Loki's per-query series cap.

Data provenance: ``syntax_fix`` events carry a ``fixes`` attribute (comma
list, e.g. ``"ruff_format"``) and are written with ``category='telemetry'`` —
``syntax_fix`` is in ``shared/telemetry.py``'s telemetry event set
(event_name-category final convention, 2026-08-05, tracker #762), so
``category_for_kind`` maps it to ``telemetry`` (90d retention). The category
predicate keeps the ``|log`` alternative for pre-convention rows (the core
panels' pattern); the pre-convention PG rows were backfilled by the
accompanying migration, Loki rows keep their emit-time category.
"""

from shared.plugin_metrics import MetricSpec, register_metric

# The event stream + json pipeline every template starts with. The selector
# matches the unified emitter's OTLP resource (gateway/loki_events._SELECTOR).
_SEL = '{service_name="unknown_service"} | json'

# Category filter: the 2026-08-05 convention moved syntax_fix from log to
# telemetry — keep the |log alternative for pre-convention rows (core-panel
# pattern). {category_re} renders the category UNQUOTED for the regex.
_CAT = 'category=~"{category_re}|log"'


def _count(pipeline: str, window: str) -> str:
    """One count_over_time series — every count wraps in sum(...) (see the
    module docstring for the series-cap note)."""
    return f"sum(count_over_time({_SEL} | {pipeline} [{window}]))"


register_metric(
    MetricSpec(
        name="ava_code_syntax_fix_count",
        title="Syntax fix count",
        description=(
            "Syntax_fix events over time — how often the repair pipeline fixes "
            "syntax errors in LLM-produced code (event_name='syntax_fix', "
            "category='telemetry', 90d retention)."
        ),
        event_name="syntax_fix",
        category="telemetry",
        unit="short",
        panel="timeseries",
        query=_count(f"{_CAT} | event_name={{event_name}}", "5m"),
        query_type="logql",
        target_names=["fixes"],
        output=["grafana"],
    )
)

register_metric(
    MetricSpec(
        name="ava_code_syntax_fix_total",
        title="Syntax fixes (window)",
        description=(
            "Total syntax_fix events in the current window (event_name='syntax_fix', "
            "category='telemetry')."
        ),
        event_name="syntax_fix",
        category="telemetry",
        unit="short",
        panel="stat",
        query=_count(f"{_CAT} | event_name={{event_name}}", "$__range"),
        query_type="logql",
        target_names=["fixes"],
        output=["grafana"],
    )
)
