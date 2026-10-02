"""Core frontend-telemetry panels — user-modeling interaction metrics.

Split out of ``base/telemetry/metrics/core/observability.py`` (task #3697 S1 line
budget): interaction volume per minute plus the three top-15 tables
(elements, page views, settings changes) over frontend_interaction
telemetry events.
"""

from __future__ import annotations

from base.events.contract import FRONTEND_INTERACTION_KEYS
from base.telemetry.metrics.core import catalog
from base.telemetry.metrics.logql import event_count
from base.telemetry.metrics.plugin_metrics import MetricSpec

_FRONTEND_ATTR = {k: f"attributes_{k}" for k in FRONTEND_INTERACTION_KEYS}


catalog.register_core_metric(
    MetricSpec(
        name="ava_obs_frontend_interactions",
        title="Frontend interactions",
        time_basis="per_minute",
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
        query=event_count(
            'category={category} | source="user"',
            "5m",
            matchers="event_name={event_name}",
        ),
        target_names=["interactions"],
        output=["grafana"],
        panel_id=34,
        section="Gateway & execution",
        order=8,
    )
)

catalog.register_core_metric(
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

catalog.register_core_metric(
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

catalog.register_core_metric(
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
