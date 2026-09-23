# ruff: noqa: RUF001 — multiplication signs in the cost formulas
"""Core cost-analysis panels — the LLM spend family of the ops dashboard.

Split out of ``shared/metrics/core/core_metrics_panels.py`` (task #3697 S1 line budget):
the window-cost stat, the two pace projections, and the three cost
breakdowns (per-minute barchart, top-20 by model / by agent). Every panel
reads usage-time attributes_cost_usd snapshots from telemetry llm_usage
events (the 2026-08-23 overhaul, task #384); the pace tiles extrapolate
the window spend over the panel range.
"""

from __future__ import annotations

from shared.events.contract import LLM_USAGE_KEYS
from shared.metrics.core import core_metrics
from shared.plugin_metrics import MetricSpec

_SEL_EV = '{service_name="unknown_service", event_name={event_name}}'
_LLM_ATTR = {k: f"attributes_{k}" for k in LLM_USAGE_KEYS}


def _llm_cost(window: str) -> str:
    """Usage-time LLM cost snapshots over one Grafana/Loki range vector."""
    return (
        f"sum(sum_over_time({_SEL_EV} | json | "
        f"category={{category}} | "
        f"unwrap {_LLM_ATTR['cost_usd']} [{window}]))"
    )


core_metrics.register_core_metric(
    MetricSpec(
        name="core_llm_cost_24h",
        title="LLM cost (window)",
        event_name="llm_usage",
        category="telemetry",
        unit="currencyUSD",
        panel="stat",
        # cost_usd rides in every llm_usage payload (task #2626) — unwrap it
        # instead of mirroring MODEL_PRICING into SQL (405 ruling, 2026-08-14).
        query=_llm_cost("$__range"),
        query_type="logql",
        target_names=["llm cost"],
        field_defaults={"decimals": 2},
        width=8,
        height=4,
        panel_id=7,
        section="core",
        order=6,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_llm_cost_today_estimate",
        title="LLM cost estimate — day pace",
        description=(
            "Projected full-day LLM spend from usage-time cost snapshots over the "
            "dashboard time window. Formula: window spend × 86,400 / elapsed "
            "seconds in the panel range."
        ),
        event_name="llm_usage",
        category="telemetry",
        unit="currencyUSD",
        panel="stat",
        query=f"({_llm_cost('$__range')}) * 86400 / $__range_s",
        query_type="logql",
        target_names=["today est."],
        field_defaults={"decimals": 2},
        width=8,
        height=4,
        panel_id=38,
        section="Cost analysis",
        order=0,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_llm_cost_month_estimate",
        title="LLM cost estimate — 30-day pace",
        description=(
            "Projected 30-day LLM spend from usage-time cost snapshots over the "
            "dashboard time window. Formula: window spend × 2,592,000 / elapsed "
            "seconds in the panel range."
        ),
        event_name="llm_usage",
        category="telemetry",
        unit="currencyUSD",
        panel="stat",
        query=f"({_llm_cost('$__range')}) * 2592000 / $__range_s",
        query_type="logql",
        target_names=["month est."],
        field_defaults={"decimals": 2},
        width=16,
        height=4,
        panel_id=39,
        section="Cost analysis",
        order=1,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_llm_cost_daily",
        title="LLM cost / minute",
        description=(
            "Usage-time LLM cost snapshots grouped into $__interval buckets, "
            "normalized to a per-minute USD rate (bucket sum / interval "
            "seconds * 60)."
        ),
        event_name="llm_usage",
        category="telemetry",
        unit="currencyUSD",
        panel="barchart",
        query=_llm_cost("$__interval") + " / ($__interval_ms / 60000)",
        query_type="logql",
        target_names=["cost usd"],
        thresholds=[],
        options={"tooltip": {"mode": "single", "sort": "none"}},
        panel_id=41,
        section="Cost analysis",
        order=2,
        width=24,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_llm_cost_by_model",
        title="LLM cost by model (Top 20)",
        description=(
            "Top 20 models by windowed usage-time cost snapshots. The model name "
            "is the llm_usage payload's attributes_model label."
        ),
        event_name="llm_usage",
        category="telemetry",
        unit="currencyUSD",
        panel="table",
        query=(
            f'topk(20, sum by (attributes_model) (sum_over_time({{service_name="unknown_service", event_name={{event_name}}}} '
            f"| json | category={{category}} | "
            f'attributes_model!="" | unwrap {_LLM_ATTR["cost_usd"]} [$__range])))'
        ),
        query_type="logql",
        target_names=["{{attributes_model}}"],
        thresholds=[],
        panel_id=42,
        section="Cost analysis",
        order=3,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_llm_cost_by_agent",
        title="LLM cost by agent (Top 20)",
        description="Top 20 agents by windowed usage-time LLM cost snapshots.",
        event_name="llm_usage",
        category="telemetry",
        unit="currencyUSD",
        panel="table",
        query=(
            f'topk(20, sum by (agent_id) (sum_over_time({{service_name="unknown_service", event_name={{event_name}}, agent_id!=""}} '
            f"| json | category={{category}} | "
            f"unwrap {_LLM_ATTR['cost_usd']} [$__range])))"
        ),
        query_type="logql",
        target_names=["{{agent_id}}"],
        thresholds=[],
        panel_id=43,
        section="Cost analysis",
        order=4,
    )
)
