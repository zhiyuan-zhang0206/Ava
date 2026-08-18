"""ava_code Grafana metrics — registered at import time by the generator.

``scripts/gen_plugin_dashboard.py`` imports this module (inside a
PluginContext) to collect the registrations below; the plugin name comes from
the context. Query templates target the unified ``events`` table with
``{event_name}`` / ``{category}`` placeholders that the generator renders as
single-quoted literals.

Data provenance: ``syntax_fix`` events carry a ``fixes`` attribute (comma
list, e.g. ``"ruff_format"``) and are written with ``category='telemetry'`` —
``syntax_fix`` is in ``shared/telemetry.py``'s telemetry event set
(event_name-category final convention, 2026-08-05, tracker #762), so
``category_for_kind`` maps it to
``telemetry`` (90d retention). The category predicates below are updated to
match; the pre-convention rows (category='log') were backfilled by the accompanying
migration.
"""

from shared.plugin_metrics import MetricSpec, register_metric

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
        query=(
            'SELECT $__timeGroup(ts, $__interval) AS time, count(*) AS "fixes" '
            "FROM events "
            "WHERE event_name = {event_name} AND category = {category} AND $__timeFilter(ts) "
            "GROUP BY 1 ORDER BY 1"
        ),
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
        query=(
            "SELECT count(*) AS fixes "
            "FROM events "
            "WHERE event_name = {event_name} AND category = {category} AND $__timeFilter(ts)"
        ),
        output=["grafana"],
    )
)
