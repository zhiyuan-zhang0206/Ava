# ruff: noqa: RUF001 — RUF001: Chinese UI text uses full-width punctuation; S608: query templates interpolate only registry-derived key fragments (shared/events/contract.py SQL constants), never user input
"""Core ops-dashboard panels — the hand-written core section of
``dashboards/ops/ava-ops-main.json``, migrated to core-metric registrations
(Task #882) and from Postgres-event SQL to Loki LogQL (Task #1280).

The 16 hand-written core panels (ids < 1000) of the ops dashboard are
registered here as core metrics, in their original gridPos order: the 6 stat
panels first (two rows of three 8-wide), then the 8 charts in two 12-wide
columns. Rendered output stays panel-for-panel equivalent to the
pre-migration dashboard — only the panel ids (now 1..16), the leading
``core`` row header (id 900) and the version field change.

Query dialect (Task #1280): every panel reads the event stream from Loki
instead of the retired PG ``events`` table — the same read the alert rules
(R1-R7) use. Each LogQL template selects ``{service_name="unknown_service"}``
(the unified emitter's OTLP resource), pipelines ``| json`` (event fields are
structured metadata, NOT stream labels), and filters on the flattened labels
(``attributes.in_total`` -> ``attributes_in_total``). Stat/table panels run as
instant queries over ``[$__range]`` (the whole panel window); timeseries and
barchart panels run as range queries bucketed by ``[$__interval]``. The one
panel that does NOT read events — ``core_live_agents`` (the ``agents_meta``
table, still live in PG) — stays SQL by design.

Per-panel provenance:

- **6 stat panels** (LLM calls / Warning / Error / Live agents /
  LLM cost (24h) / Tokens (24h)): explicit 8x4 grid (three per row). The
  generator's stat color default (fixed blue) matches four of them; Warning
  (fixed orange) and Error (fixed red) set the color via ``field_defaults``,
  and LLM cost carries the original ``decimals: 2``. Error keeps the
  original ``unit: "s"`` and the ``noValue: "0"`` option.
- **8 chart panels** (SSE backlog / LLM throughput / Token usage —
  Output + Reasoning / Cache hit / Input+Output+Gen-stage TPS / LLM calls /
  bucket / Event health / Token usage — Input): default 12x7 grid with the
  red-80 threshold step, except the three TPS panels which have no
  thresholds at all (``thresholds=[]`` suppresses the default green base).
  ``custom`` sets only the keys that differ from the generator defaults
  (fillOpacity 25 / axisLabel / stacking normal A on the two token-usage
  panels); SSE backlog and LLM calls / bucket match the barchart defaults
  and need no custom.
- **event_name/category**: taken from each query's semantics (llm_usage/telemetry,
  delivery_stalled/telemetry, ...). The level/status-based queries (Warning,
  Error, Live agents, Event health) filter on ``level``/``status`` rather
  than an event name — their event_name values ("warning" / "error" /
  "lifecycle" / "event") are descriptive registry metadata only.
- **LogQL naming**: LogQL aggregates carry no series labels, so every
  multi-series panel names its targets via ``target_names`` (rendered as
  Grafana fieldConfig displayName overrides); the series labels from the SQL
  aliases are preserved exactly (``"stalled <60s"`` etc.).
- **SQL fixes** (the metric-template whitelist — see
  ``shared/plugin_metrics.validate_metric_sql`` — rejects ``<``/``>``/``/``
  inside quoted identifiers): the SSE backlog aliases ``"stalled <60s"`` /
  ``"stalled >600s"`` (subquery columns, outer references and output
  labels) are renamed to ``"stalled <60s"`` / ``"stalled >600s"`` —
  the values are unchanged, only the series labels lose the ``<``/``>``
  glyphs (no per-series rename mechanism exists short of fieldConfig
  overrides, which the spec/generator do not carry). The LLM throughput
  alias ``"tokens/s"`` becomes ``"tokens/s"``, and the rendered series
  label is preserved exactly via ``field_defaults`` displayName
  (``"tokens/s"``), so that panel renders identically to the original.
"""

from __future__ import annotations

from shared import core_metrics
from shared.events.contract import DELIVERY_STALLED_KEYS, GATEWAY_LATENCY_KEYS, LLM_USAGE_KEYS
from shared.plugin_metrics import MetricSpec, ThresholdStep

# ── LogQL fragments (Task #1280) ──────────────────────────────────────────────
# The event stream + json pipeline every template starts with. Attribute
# labels are derived from the payload-key contract (a renamed payload key
# fails loudly here instead of silently NULLing out).
_SEL = '{service_name="unknown_service"} | json'
_LLM_ATTR = {k: f"attributes_{k}" for k in LLM_USAGE_KEYS}
_DELIVERY_ATTR = {k: f"attributes_{k}" for k in DELIVERY_STALLED_KEYS}
_GATEWAY_ATTR = {k: f"attributes_{k}" for k in GATEWAY_LATENCY_KEYS}


def _count(pipeline: str, window: str) -> str:
    """One count_over_time series — every count wraps in sum(...): the
    unknown_service family has >500 streams over a day, and an unaggregated
    count_over_time hits Loki's per-query series cap (alert-rules note)."""
    return (
        f'sum(count_over_time({{service_name="unknown_service"}} | json | {pipeline} [{window}]))'
    )


# ── stat panels (8-wide, three per row) ───────────────────────────────

core_metrics.register_core_metric(
    MetricSpec(
        name="core_llm_calls",
        title="LLM calls",
        event_name="llm_usage",
        category="telemetry",
        unit="short",
        panel="stat",
        query=_count('category=~"{category_re}|log" | event_name={event_name}', "$__range"),
        query_type="logql",
        target_names=["calls"],
        width=8,
        height=4,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_warning",
        title="Warning",
        event_name="warning",
        category="telemetry",
        unit="short",
        panel="stat",
        query=_count('category=~"{category_re}|log" | level="warning"', "$__range"),
        query_type="logql",
        target_names=["warning"],
        field_defaults={"color": {"mode": "fixed", "fixedColor": "orange"}},
        width=8,
        height=4,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_error",
        title="Error",
        event_name="error",
        category="telemetry",
        unit="s",
        panel="stat",
        query=_count('category=~"{category_re}|log" | level=~"error|critical"', "$__range"),
        query_type="logql",
        target_names=["error"],
        options={"noValue": "0"},
        field_defaults={"color": {"mode": "fixed", "fixedColor": "red"}},
        width=8,
        height=4,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_unresolved_warning",
        title="Unresolved Warning",
        event_name="warning_resolved",
        category="log",
        unit="short",
        panel="stat",
        query=_count(
            'category=~"{category_re}|log" | level="warning" | attributes_resolved_by=""',
            "$__range",
        ),
        query_type="logql",
        target_names=["unresolved_warning"],
        field_defaults={"color": {"mode": "fixed", "fixedColor": "orange"}},
        width=8,
        height=4,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_unresolved_error",
        title="Unresolved Error",
        event_name="error_resolved",
        category="log",
        unit="short",
        panel="stat",
        query=_count(
            'category=~"{category_re}|log" | level=~"error|critical" | attributes_resolved_by=""',
            "$__range",
        ),
        query_type="logql",
        target_names=["unresolved_error"],
        options={"noValue": "0"},
        field_defaults={"color": {"mode": "fixed", "fixedColor": "red"}},
        width=8,
        height=4,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_live_agents",
        title="Live agents",
        event_name="lifecycle",
        category="telemetry",
        unit="short",
        panel="stat",
        # NOT migratable to Loki: reads the live agents_meta table (agent
        # lifecycle state), not the event stream — stays on the PG
        # datasource by design (task #1280 note).
        query="""SELECT count(*) AS "live agents" FROM agents_meta WHERE status IN ('running','idling')""",
        width=8,
        height=4,
    )
)


core_metrics.register_core_metric(
    MetricSpec(
        name="core_llm_cost_24h",
        title="LLM cost (24h)",
        event_name="llm_usage",
        category="telemetry",
        unit="currencyUSD",
        panel="stat",
        # cost_usd rides in every llm_usage payload (task #2626) — unwrap it
        # instead of mirroring MODEL_PRICING into SQL (405 ruling, 2026-08-14).
        query=(
            f'sum(sum_over_time({{service_name="unknown_service"}} | json | '
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"unwrap {_LLM_ATTR['cost_usd']} [$__range]))"
        ),
        query_type="logql",
        target_names=["llm cost"],
        field_defaults={"decimals": 2},
        width=8,
        height=4,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_tokens_24h",
        title="Tokens (24h)",
        event_name="llm_usage",
        category="telemetry",
        unit="short",
        panel="stat",
        query=(
            f'sum(sum_over_time({{service_name="unknown_service"}} | json | '
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"unwrap {_LLM_ATTR['in_total']} [$__range]))"
            f' + sum(sum_over_time({{service_name="unknown_service"}} | json | '
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"unwrap {_LLM_ATTR['out_total']} [$__range]))"
        ),
        query_type="logql",
        target_names=["tokens"],
        width=8,
        height=4,
    )
)


# ── chart panels (12-wide, two per row) ──────────────────────────────

core_metrics.register_core_metric(
    MetricSpec(
        name="core_sse_backlog",
        title="SSE backlog — delivery_stalled (by stall seconds)",
        event_name="delivery_stalled",
        category="telemetry",
        unit="short",
        panel="barchart",
        query=_count(
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"{_DELIVERY_ATTR['age_s']} < 60",
            "$__interval",
        ),
        targets=[
            _count(
                f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
                f"{_DELIVERY_ATTR['age_s']} >= 60 | {_DELIVERY_ATTR['age_s']} < 600",
                "$__interval",
            ),
            _count(
                f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
                f"{_DELIVERY_ATTR['age_s']} >= 600",
                "$__interval",
            ),
        ],
        query_type="logql",
        target_names=["stalled <60s", "stalled 60-600s", "stalled >600s"],
        thresholds=[ThresholdStep(color="red", value=80.0)],
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_llm_throughput",
        title="LLM throughput tokens/s",
        event_name="llm_usage",
        category="telemetry",
        unit="tok/s",
        panel="timeseries",
        # tokens/s = rate of (in + out + reasoning) — rate over unwrap is
        # the per-second sum, the LogQL equivalent of
        # Σ(in+out+reasoning) ÷ interval_sec.
        query=(
            f'sum(rate({{service_name="unknown_service"}} | json | '
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"unwrap {_LLM_ATTR['in_total']} [$__interval]))"
            f' + sum(rate({{service_name="unknown_service"}} | json | '
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"unwrap {_LLM_ATTR['out_total']} [$__interval]))"
            f' + sum(rate({{service_name="unknown_service"}} | json | '
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"unwrap {_LLM_ATTR['reasoning']} [$__interval]))"
        ),
        query_type="logql",
        target_names=["tokens/s"],
        custom={"fillOpacity": 25, "axisLabel": "tokens/s"},
        thresholds=[ThresholdStep(color="red", value=80.0)],
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_token_output_reasoning",
        title="Token usage — Output + Reasoning",
        event_name="llm_usage",
        category="telemetry",
        unit="short",
        panel="timeseries",
        query=(
            f'sum(sum_over_time({{service_name="unknown_service"}} | json | '
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"unwrap {_LLM_ATTR['out_total']} [$__interval]))"
        ),
        targets=[
            f'sum(sum_over_time({{service_name="unknown_service"}} | json | '
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"unwrap {_LLM_ATTR['reasoning']} [$__interval]))"
        ],
        query_type="logql",
        target_names=["out", "reasoning"],
        custom={
            "fillOpacity": 25,
            "stacking": {"mode": "normal", "group": "A"},
            "axisLabel": "tokens",
        },
        thresholds=[ThresholdStep(color="red", value=80.0)],
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_cache_hit",
        title="Cache hit (token-weighted / max agent / min agent)",
        event_name="llm_usage",
        category="telemetry",
        unit="percent",
        panel="timeseries",
        query=(
            f'100 * sum(sum_over_time({{service_name="unknown_service"}} | json | '
            f"category={{category}} | event_name={{event_name}} | "
            f"unwrap {_LLM_ATTR['cache_read']} [$__interval]))"
            f' / sum(sum_over_time({{service_name="unknown_service"}} | json | '
            f"category={{category}} | event_name={{event_name}} | "
            f"unwrap {_LLM_ATTR['in_total']} [$__interval]))"
        ),
        targets=[
            # max agent: per-agent ratio, then the max across agents per bucket
            (
                f'100 * max(sum by (agent_id) (sum_over_time({{service_name="unknown_service"}} '
                f'| json | category={{category}} | event_name={{event_name}} | agent_id!="" | '
                f"unwrap {_LLM_ATTR['cache_read']} [$__interval]))"
                f' / sum by (agent_id) (sum_over_time({{service_name="unknown_service"}} '
                f'| json | category={{category}} | event_name={{event_name}} | agent_id!="" | '
                f"unwrap {_LLM_ATTR['in_total']} [$__interval])))"
            ),
            (
                f'100 * min(sum by (agent_id) (sum_over_time({{service_name="unknown_service"}} '
                f'| json | category={{category}} | event_name={{event_name}} | agent_id!="" | '
                f"unwrap {_LLM_ATTR['cache_read']} [$__interval]))"
                f' / sum by (agent_id) (sum_over_time({{service_name="unknown_service"}} '
                f'| json | category={{category}} | event_name={{event_name}} | agent_id!="" | '
                f"unwrap {_LLM_ATTR['in_total']} [$__interval])))"
            ),
        ],
        query_type="logql",
        target_names=["overall", "max agent", "min agent"],
        custom={"axisLabel": "cache hit %"},
        thresholds=[ThresholdStep(color="red", value=80.0)],
    )
)


# The three TPS panels share one shape: avg = Σ(tokens) / Σ(ms) * 1000 over
# calls that carry the timing attribute; max/min = the fastest/slowest agent
# per bucket (per-agent ratio, then max/min across agents). The existence
# filter (SQL `attributes ? 'latency_ms'`) is `attributes_<key>!=""` — the
# json-extracted label exists only on lines that carry the field.


def _tps(
    name: str,
    title: str,
    description: str,
    attr_key: str,
    attr_label: str,
    tps_label: str,
) -> None:
    tok = _LLM_ATTR[attr_key]
    timing = _LLM_ATTR[attr_label]
    avg = (
        f'sum(sum_over_time({{service_name="unknown_service"}} | json | '
        f'category={{category}} | event_name={{event_name}} | {timing}!="" | '
        f"unwrap {tok} [$__interval]))"
        f' / sum(sum_over_time({{service_name="unknown_service"}} | json | '
        f'category={{category}} | event_name={{event_name}} | {timing}!="" | '
        f"unwrap {timing} [$__interval])) * 1000"
    )
    per_agent = (
        f'sum by (agent_id) (sum_over_time({{service_name="unknown_service"}} | json | '
        f'category={{category}} | event_name={{event_name}} | agent_id!="" | {timing}!="" | '
        f"unwrap {tok} [$__interval]))"
        f' / sum by (agent_id) (sum_over_time({{service_name="unknown_service"}} | json | '
        f'category={{category}} | event_name={{event_name}} | agent_id!="" | {timing}!="" | '
        f"unwrap {timing} [$__interval])) * 1000"
    )
    core_metrics.register_core_metric(
        MetricSpec(
            name=name,
            title=title,
            description=description,
            event_name="llm_usage",
            category="telemetry",
            unit="tok/s",
            panel="timeseries",
            query_type="logql",
            query=avg,
            targets=[f"max({per_agent})", f"min({per_agent})"],
            target_names=[f"avg {tps_label}", f"max {tps_label}", f"min {tps_label}"],
            custom={"fillOpacity": 25, "axisLabel": tps_label},
            thresholds=[],
        )
    )


_tps(
    "core_input_tps",
    "Input TPS (avg / max agent / min agent)",
    "Input throughput = Σ(in_total) ÷ Σ(latency_sec), tok/s. in_total is the "
    "log_llm_usage total input tokens (incl. cache_read; ~99.5% cache hits "
    "over the last 24h, so input TPS ≈ cache/prefill read throughput). "
    "latency is the llm_usage wall-clock (latency_ms). avg = token-weighted "
    "over all calls; max/min = the fastest/slowest agent per bucket "
    "(agent_id IS NOT NULL, only buckets with valid calls count).",
    "in_total",
    "latency_ms",
    "in tokens/s",
)

_tps(
    "core_output_tps",
    "Output TPS (avg / max agent / min agent)",
    "Output throughput = Σ(out_total) ÷ Σ(latency_sec), tok/s. out_total is "
    "the log_llm_usage total output tokens, incl. reasoning (Anthropic "
    "thinking / OpenAI reasoning both count into output_tokens; observe.py "
    "does not add them again) — i.e. pure decode generation speed (~79 tok/s "
    "measured over the last 24h). latency is the llm_usage wall-clock "
    "(latency_ms). avg = token-weighted over all calls; max/min = the "
    "fastest/slowest agent per bucket (agent_id IS NOT NULL, only buckets "
    "with valid calls count).",
    "out_total",
    "latency_ms",
    "out tokens/s",
)

_tps(
    "core_gen_stage_tps",
    "Gen-stage output TPS (avg / max agent / min agent)",
    "Gen-stage output throughput = Σ(out_total) ÷ Σ(decode_sec), tok/s. "
    "decode_ms is the 2026-08-04 new instrumentation (stream last chunk − "
    "first chunk arrival, excluding network/queue/prefill); out_total is the "
    "log_llm_usage total output tokens (incl. reasoning). Only calls after "
    "the decode_ms instrumentation went live are counted (attributes ? "
    "'decode_ms'); non-streaming fallback / empty-stream calls with "
    "decode_ms=NULL do not count. avg = token-weighted over all calls; "
    "max/min = the fastest/slowest agent per bucket (agent_id IS NOT NULL, "
    "only buckets with valid calls count).",
    "out_total",
    "decode_ms",
    "out tokens/s",
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_llm_calls_per_bucket",
        title="LLM calls / bucket",
        event_name="llm_usage",
        category="telemetry",
        unit="short",
        panel="barchart",
        query=_count('category=~"{category_re}|log" | event_name={event_name}', "$__interval"),
        query_type="logql",
        target_names=["calls"],
        thresholds=[ThresholdStep(color="red", value=80.0)],
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_event_health",
        title="Event health — WARNING+ERROR vs total",
        event_name="event",
        category="telemetry",
        unit="short",
        panel="timeseries",
        query=_count(
            'category=~"{category_re}|log" | level=~"warning|error|critical"', "$__interval"
        ),
        targets=[_count('category=~"{category_re}|log"', "$__interval")],
        query_type="logql",
        target_names=["warn+error", "total"],
        custom={"axisLabel": "events"},
        thresholds=[ThresholdStep(color="red", value=80.0)],
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_token_input",
        title="Token usage — Input",
        event_name="llm_usage",
        category="telemetry",
        unit="short",
        panel="timeseries",
        query=(
            f'sum(sum_over_time({{service_name="unknown_service"}} | json | '
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"unwrap {_LLM_ATTR['in_total']} [$__interval]))"
        ),
        query_type="logql",
        target_names=["in"],
        custom={
            "fillOpacity": 25,
            "stacking": {"mode": "normal", "group": "A"},
            "axisLabel": "tokens",
        },
        thresholds=[ThresholdStep(color="red", value=80.0)],
    )
)


# ── gateway latency (Task #1091) ─────────────────────────────────────────
# Producer: gateway/_latency.py — one `gateway_latency` event per (route, 60s
# bucket) carrying p50/p95/max/count. The panels below read the aggregates;
# the first is the cluster-wide overview, the second the per-route p95.

core_metrics.register_core_metric(
    MetricSpec(
        name="core_gateway_latency",
        title="Gateway latency — p50/p95/max",
        event_name="gateway_latency",
        category="telemetry",
        unit="ms",
        panel="timeseries",
        query=(
            f'max(max_over_time({{service_name="unknown_service"}} | json | '
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"unwrap {_GATEWAY_ATTR['p50_ms']} [$__interval]))"
        ),
        targets=[
            f'max(max_over_time({{service_name="unknown_service"}} | json | '
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"unwrap {_GATEWAY_ATTR['p95_ms']} [$__interval]))",
            f'max(max_over_time({{service_name="unknown_service"}} | json | '
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"unwrap {_GATEWAY_ATTR['max_ms']} [$__interval]))",
        ],
        query_type="logql",
        target_names=["p50", "p95", "max"],
        custom={"axisLabel": "ms"},
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_gateway_latency_by_route",
        title="Gateway latency p95 by route",
        event_name="gateway_latency",
        category="telemetry",
        unit="ms",
        panel="timeseries",
        # One series per route — the attributes_route label carries the name.
        query_type="logql",
        query=(
            f'max by (attributes_route) (max_over_time({{service_name="unknown_service"}} | json | '
            f'category=~"{{category_re}}|log" | event_name={{event_name}} | '
            f"unwrap {_GATEWAY_ATTR['p95_ms']} [$__interval]))"
        ),
        custom={"axisLabel": "ms"},
    )
)
