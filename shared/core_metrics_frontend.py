"""Core frontend-telemetry panels — user-modeling interaction metrics.

Split out of ``shared/core_metrics_observability.py`` (task #3697 S1 line
budget): interaction volume per minute plus the three top-15 tables
(elements, page views, settings changes) over frontend_interaction
telemetry events.
"""

from __future__ import annotations

from shared import core_metrics
from shared.events.contract import FRONTEND_INTERACTION_KEYS
from shared.plugin_metrics import MetricSpec

_SEL = '{service_name="unknown_service"}'
_FRONTEND_ATTR = {k: f"attributes_{k}" for k in FRONTEND_INTERACTION_KEYS}


def _count(pipeline: str, window: str, matchers: str | None = None) -> str:
    """One count_over_time series — every count wraps in sum(...): the
    unknown_service family has >500 streams over a day, and an unaggregated
    count_over_time hits Loki's per-query series cap (alert-rules note)."""
    selector = _SEL if matchers is None else f'{{service_name="unknown_service", {matchers}}}'
    return f"sum(count_over_time({selector} | json | {pipeline} [{window}]))"


core_metrics.register_core_metric(
    MetricSpec(
        name="ava_obs_frontend_interactions",
        title="Frontend interactions (per minute)",
        description=(
            "Frontend interaction volume per minute (5-minute buckets / 5, "
            "total frontend_interaction events): the entry panel of "
            "user-modeling telemetry, doubling as volume monitoring — an "
            "abnormal interaction spike (an instrumentation loop bug) is "
            "immediately visible here. event_name='frontend_interaction', "
            "category='telemetry', source='user'."
        ),
        event_name="frontend_interaction",
        category="telemetry",
        unit="short",
        panel="timeseries",
        query_type="logql",
        query=_count(
            'category={category} | source="user"',
            "5m",
            matchers="event_name={event_name}",
        )
        + " / 5",
        target_names=["interactions"],
        output=["grafana"],
        panel_id=34,
        section="Gateway & execution",
        order=8,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="ava_obs_frontend_top_elements",
        title="Frontend interactions (Top 15 elements)",
        description=(
            "Top 15 in-window interactions grouped by attributes.element — "
            "ranking of the interaction points users click/trigger most "
            "(spawn/composer-send/setting-change/page-view/...). "
            "event_name='frontend_interaction', category='telemetry'."
        ),
        event_name="frontend_interaction",
        category="telemetry",
        unit="short",
        panel="table",
        query_type="logql",
        query=(
            f'topk(15, sum by ({_FRONTEND_ATTR["element"]}) (count_over_time({{service_name="unknown_service", event_name={{event_name}}}} | json | '
            f'category={{category}} | source="user" [$__range])))'
        ),
        target_names=["{{attributes_element}}"],
        output=["grafana"],
        panel_id=35,
        section="Gateway & execution",
        order=9,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="ava_obs_frontend_page_views",
        title="Frontend page views (Top 15)",
        description=(
            "Top 15 in-window page views (element='page-view') grouped by "
            "attributes.page — which screens users spend the most time on. "
            "event_name='frontend_interaction', category='telemetry'."
        ),
        event_name="frontend_interaction",
        category="telemetry",
        unit="short",
        panel="table",
        query_type="logql",
        query=(
            f'topk(15, sum by ({_FRONTEND_ATTR["page"]}) (count_over_time({{service_name="unknown_service", event_name={{event_name}}}} | json | '
            f'category={{category}} | source="user" | '
            f'{_FRONTEND_ATTR["element"]}="page-view" [$__range])))'
        ),
        target_names=["{{attributes_page}}"],
        output=["grafana"],
        panel_id=36,
        section="Gateway & execution",
        order=10,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="ava_obs_frontend_settings_changes",
        title="Settings changes (Top 15)",
        description=(
            "Top 15 in-window user setting changes (element='setting-change') "
            "grouped by attributes.key — which settings/layout/preferences "
            "users adjusted (display.* / behavior.* keys). "
            "event_name='frontend_interaction', category='telemetry'."
        ),
        event_name="frontend_interaction",
        category="telemetry",
        unit="short",
        panel="table",
        query_type="logql",
        query=(
            f'topk(15, sum by ({_FRONTEND_ATTR["key"]}) (count_over_time({{service_name="unknown_service", event_name={{event_name}}}} | json | '
            f'category={{category}} | source="user" | '
            f'{_FRONTEND_ATTR["element"]}="setting-change" [$__range])))'
        ),
        target_names=["{{attributes_key}}"],
        output=["grafana"],
        panel_id=37,
        section="Gateway & execution",
        order=11,
    )
)
