"""Core observability metrics — the ava_observability pack, migrated to core (Task #882)
and from Postgres-event SQL to Loki LogQL (Task #1280).

The generic observability pack (originally the W18 ``ava_observability``
plugin, retired with this migration): cluster-wide and per-agent metrics over
the unified ``events`` stream in Loki — LLM spend and health, turn outcomes,
exec outcomes, code-repair triggers, agent lifecycle, SDK usage, delivery
health and the total event rate. Replaces the information content of the
retired Metrics page (base/telemetry/metrics/report.py).

Migrated plugin -> core (user ruling 2026-08-06: core metrics + plugin
metrics two-tier architecture): the 21 MetricSpec definitions below are returned
by ``core_metrics()`` and validated by ``catalog.validate_core_metric`` instead of
the data registry's admission — the same query safety validation, ``plugin``
auto-filled to "core" (``catalog.collect_core_metrics`` calls this module through the
``_CORE_DEFINITION_MODULES`` tuple); templates use the
{event_name}/{category} placeholders and, on inspector-only metrics, the
{{agent_id}} placeholder.

Query dialect (Task #1280): every query reads the event stream from Loki —
``{service_name="unknown_service"} | json`` then label filters on the
flattened event fields (the same read the alert rules and the core panels
use; agent_id/event_name are promoted index labels since the 2026-08-23
cutover, see base/telemetry/loki_index_labels.py — per-agent queries may match them
in the selector, the fleet-wide queries here keep the base form). Event
attributes unwrap via ``attributes_<key>``; the numeric payload
fields (in_total/out_total/cost_usd/...) come straight from the event JSON —
cost_usd has ridden in every llm_usage payload since #2626, so the cost
panels unwrap it instead of mirroring MODEL_PRICING into SQL (405 ruling,
2026-08-14). Multi-series panels name their targets with per-target
``legendFormat`` values because LogQL aggregates carry no labels.

Data provenance (verified against the live prod stream, 2026-08-04):
- llm_usage ~41.3k rows/7d, category=telemetry; attributes carry model /
  in_total / out_total / cache_read / reasoning / latency_ms / cost_usd.
- turn_end ~41.4k rows/7d: attributes ok ('true'/'false') + duration_seconds;
  ok=true 166.7k / ok=false 4.9k all-time (~97%).
- exec 36.5k ok / 1235 exec_failed / 1 exec_node_timeout per 7d;
  failure event names emitted as exec_failed / exec(timeout) /
  exec(cancelled) / exec_node_timeout (legacy parenthesized spellings
  coexist in the stream — both counted; exec_thread_stuck stopped being
  emitted when the thread backend was removed, PR3).
- syntax_fix 9.8k rows/7d, category=telemetry (final caliber, 2026-08-05,
  tracker #762 — pre-caliber rows were backfilled by the accompanying migration);
  attributes.fixes is a comma list,
  e.g. "invalid_escape(38),ruff_format"; fixes seen: ruff_format, ruff,
  invalid_escape, missing_imports, chinese_punct, bracket_matching,
  escape_inner_quotes, fstring_expr, nested_triple_quote, backslash_trailing.
- sdk_call 58.7k rows/7d: attributes.fn ("shell.run" style), category=telemetry.
- halt 3.9k rows/7d: attributes.body = 'no tool_call (idle)' /
  'system_halt (compact)' / 'lifecycle AgentTermination' / 'lifecycle AgentRestart'.
- spawn (audit) 404 rows/7d: the events.source column carries the spawner
  ('agent:N' dominant, plus 'user' / 'cron').
- delivery_stalled 10.4k rows/7d, category=telemetry (the delivery watchdog
  emits it explicitly with attributes age_s).
"""

from __future__ import annotations

from base.events.contract import (
    FRONTEND_INTERACTION_KEYS,
    HALT_KEYS,
    LLM_USAGE_KEYS,
    SDK_CALL_KEYS,
    SYNTAX_FIX_KEYS,
    TURN_END_KEYS,
)
from base.telemetry.metrics.logql import event_count
from base.telemetry.metrics.plugin_metrics import MetricSpec

# ── LogQL fragments (Task #1280) ──────────────────────────────────────────────
# Attribute labels are derived from the payload-key contract (a renamed
# payload key fails loudly here instead of silently NULLing out).
_LLM_ATTR = {k: f"attributes_{k}" for k in LLM_USAGE_KEYS}
_TURN_ATTR = {k: f"attributes_{k}" for k in TURN_END_KEYS}
_HALT_ATTR = {k: f"attributes_{k}" for k in HALT_KEYS}
_FIX_ATTR = {k: f"attributes_{k}" for k in SYNTAX_FIX_KEYS}
_FRONTEND_ATTR = {k: f"attributes_{k}" for k in FRONTEND_INTERACTION_KEYS}
_SDK_ATTR = {k: f"attributes_{k}" for k in SDK_CALL_KEYS}


# ── LLM ──────────────────────────────────────────────────────────────────────


def core_metrics() -> list[MetricSpec]:
    """This module's core metrics, in registration order."""
    specs: list[MetricSpec] = []
    specs.append(
        MetricSpec(
            name="ava_obs_llm_cost_usd",
            title="LLM cost",
            time_basis="per_minute",
            description=(
                "LLM call cost per minute — unwrap of the cost_usd field every "
                "llm_usage payload carries (task #2626; the producer computes it "
                "from one versioned-catalog quote in base/lm/pricing.py, unpriced "
                "models carry no cost_usd and are skipped); 30-minute buckets "
                "normalized to per-minute USD (bucket sum / 30). "
                "event_name='llm_usage', category='telemetry'."
            ),
            event_name="llm_usage",
            category="telemetry",
            unit="currencyUSD",
            panel="timeseries",
            query_type="logql",
            query=(
                f'sum(sum_over_time({{service_name="unknown_service", event_name={{event_name}}}} | json | '
                f"category={{category}} | "
                f"unwrap {_LLM_ATTR['cost_usd']} [30m]))"
            ),
            target_names=["cost usd"],
            output=["grafana"],
            panel_id=40,
            section="LLM",
            order=5,
        )
    )

    specs.append(
        MetricSpec(
            name="ava_obs_agent_llm_cost_usd",
            title="Agent LLM cost (USD)",
            description=(
                "Inspector-only (W13b): the same cost read as llm_cost_usd, "
                "filtered per agent — the {{agent_id}} placeholder is rendered by "
                'the gateway as agent_id="<n>".'
            ),
            event_name="llm_usage",
            category="telemetry",
            unit="currencyUSD",
            panel="timeseries",
            query_type="logql",
            query=(
                f'sum(sum_over_time({{service_name="unknown_service", event_name={{event_name}}}} | json | '
                f"category={{category}} | "
                f"{{{{agent_id}}}} | "
                f"unwrap {_LLM_ATTR['cost_usd']} [$__interval]))"
            ),
            target_names=["cost usd"],
            output=["inspector"],
        )
    )

    specs.append(
        MetricSpec(
            name="ava_obs_llm_error_rate",
            title="LLM errors",
            time_basis="per_minute",
            description=(
                "LLM failure signals per minute (5-minute buckets / 5): "
                "llm_provider_error (provider rejection / classification error), "
                "stream_stalled_retry (stalled stream retry), llm_turn_aborted "
                "(turn aborted after retries exhausted), stream_overloaded_retry "
                "(overload retry). The event_name field is spec metadata; the "
                "query covers all 4 failure events (all telemetry)."
            ),
            event_name="llm_usage",
            category="telemetry",
            unit="short",
            panel="timeseries",
            query_type="logql",
            query=event_count(
                "category={category}", "5m", matchers='event_name="llm_provider_error"'
            ),
            targets=[
                event_count(
                    "category={category}", "5m", matchers='event_name="stream_stalled_retry"'
                ),
                event_count("category={category}", "5m", matchers='event_name="llm_turn_aborted"'),
                event_count(
                    "category={category}", "5m", matchers='event_name="stream_overloaded_retry"'
                ),
            ],
            target_names=["provider_error", "stalled_retry", "turn_aborted", "overloaded_retry"],
            output=["grafana", "inspector"],
            panel_id=22,
            section="LLM",
            order=6,
        )
    )

    specs.append(
        MetricSpec(
            name="ava_obs_llm_stall_rate",
            title="Provider stalls",
            time_basis="per_minute",
            description=(
                "Provider stall health per minute (5-minute buckets / 5): "
                "stream_stalled_retry counted by vendor — a stalled stream "
                "segment (first-chunk / mid-stream gap / total-duration bound) "
                "was retried; the vendor dimension is the provider-health read "
                "added with the deepseek stall-wave mitigation (task #3884) — "
                "plus stream_stall_pair_terminated (two adjacent stalls: the "
                "stream segment and its non-streaming fallback both timed out, "
                "so the call was terminated early for the delayed stall-retry "
                "schedule). The pair event is deliberately outside "
                "LLM_ERROR_FAMILY (it co-emits 1:1 with the adjacent stall, "
                "which the family already counts) — this panel and the "
                "ava-ops-llm-stall-pair rule are its display surface (task "
                "#3948). event_name='stream_stalled_retry' + "
                "'stream_stall_pair_terminated', category='telemetry'."
            ),
            event_name="stream_stalled_retry",
            category="telemetry",
            unit="short",
            panel="timeseries",
            query_type="logql",
            query=(
                'sum by (attributes_vendor) (count_over_time({service_name="unknown_service", '
                "event_name={event_name}} | json | "
                "category={category} [5m]))"
            ),
            targets=[
                'sum(count_over_time({service_name="unknown_service", '
                'event_name="stream_stall_pair_terminated"} | json | '
                "category={category} [5m]))",
            ],
            target_names=["{{attributes_vendor}}", "stall pairs"],
            output=["grafana"],
            panel_id=53,
            section="LLM",
            order=8,
        )
    )

    specs.append(
        MetricSpec(
            name="ava_obs_turn_ok_rate",
            title="Turn success rate",
            description=(
                "Share of turn_end with ok='true' per bucket — turn-level health "
                "(LLM call failures, exhausted retries, and interrupted "
                "executions all set ok to false). "
                "event_name='turn_end', category='telemetry'."
            ),
            event_name="turn_end",
            category="telemetry",
            unit="percent",
            panel="timeseries",
            query_type="logql",
            query=(
                f"100 * {event_count(f'category={{category}} | {_TURN_ATTR["ok"]}="true"', '5m', matchers='event_name={event_name}')}"
                f" / {event_count('category={category}', '5m', matchers='event_name={event_name}')}"
            ),
            target_names=["ok_pct"],
            output=["grafana", "inspector"],
            panel_id=23,
            section="core",
            order=19,
        )
    )

    specs.append(
        MetricSpec(
            name="ava_obs_turn_duration_s",
            title="Turn duration (p50/p95)",
            description=(
                "p50 and p95 of the Prometheus ava_turn_end_duration_seconds "
                "histogram. The p95 uses the same source and quantile as alert rule "
                "R18: histogram_quantile(0.95, sum by (le) "
                "(rate(ava_turn_end_duration_seconds_bucket[10m])))."
            ),
            event_name="turn_end",
            category="telemetry",
            unit="s",
            panel="timeseries",
            query_type="promql",
            query=(
                "histogram_quantile(0.95, sum by (le) "
                "(rate(ava_turn_end_duration_seconds_bucket[10m])))"
            ),
            targets=[
                "histogram_quantile(0.5, sum by (le) "
                "(rate(ava_turn_end_duration_seconds_bucket[10m])))",
            ],
            target_names=["p95_s", "p50_s"],
            field_defaults={"color": {"mode": "palette-classic"}},
            custom={"lineInterpolation": "smooth", "spanNulls": True, "fillOpacity": 12},
            options={"legend": {"calcs": ["mean", "max", "last"], "displayMode": "table"}},
            output=["grafana"],
            panel_id=24,
            section="Gateway & execution",
            order=2,
        )
    )

    # ── compaction ───────────────────────────────────────────────────────────────

    specs.append(
        MetricSpec(
            name="ava_obs_compaction_summary_history_ratio",
            title="Compaction ratio (summary/history)",
            description=(
                "Mean percentage of discarded conversation characters retained in "
                "the replacement summary. The completed event is emitted where "
                "the history replacement is applied, not when a compact request "
                "or agent-authored summary is created; empty histories omit the "
                "ratio rather than manufacturing a zero denominator. "
                "event_name='compaction_completed', category='telemetry'."
            ),
            event_name="compaction_completed",
            category="telemetry",
            unit="percent",
            panel="timeseries",
            query_type="logql",
            # avg() collapses the per-event label explosion (trace/span/agent
            # labels) into one series: unwrapped, the panel renders dozens of
            # one-point lines — sparse near-empty (task #4204).
            query=(
                'avg(100 * avg_over_time({service_name="unknown_service", event_name={event_name}} | json | '
                "category={category} | unwrap attributes_summary_history_ratio [$__interval]))"
            ),
            target_names=["summary/history %"],
            output=["grafana"],
            thresholds=[],
            panel_id=50,
            section="Cost analysis",
            order=5,
        )
    )

    specs.append(
        MetricSpec(
            name="ava_obs_compaction_rate",
            title="Completed compactions",
            time_basis="per_minute",
            description=(
                "Applied history replacements per minute (5-minute buckets / 5). "
                "Counts compaction_completed rather than compact requests, so the "
                "series represents replacements that actually occurred. "
                "event_name='compaction_completed', category='telemetry'."
            ),
            event_name="compaction_completed",
            category="telemetry",
            unit="short",
            panel="timeseries",
            query_type="logql",
            query=event_count("category={category}", "5m", matchers="event_name={event_name}"),
            target_names=["compactions/min"],
            output=["grafana"],
            thresholds=[],
            panel_id=51,
            section="Cost analysis",
            order=6,
        )
    )

    # ── exec ─────────────────────────────────────────────────────────────────────

    specs.append(
        MetricSpec(
            name="ava_obs_exec_success_rate",
            title="Exec outcomes",
            time_basis="per_minute",
            description=(
                "Exec outcome breakdown per minute (5-minute buckets / 5): ok = "
                "event_name='exec'; failures split by event_name (exec_failed / "
                "exec(timeout) / exec(cancelled) / exec_node_timeout, legacy "
                "parenthesized spellings counted in; unknown exec* events fall "
                "into other). event_name='exec', category='telemetry'."
            ),
            event_name="exec",
            category="telemetry",
            unit="short",
            panel="timeseries",
            query_type="logql",
            query=event_count("category={category}", "5m", matchers="event_name={event_name}"),
            targets=[
                event_count(
                    "category={category}",
                    "5m",
                    matchers='event_name=~"exec_failed|exec[(]failed[)]"',
                ),
                event_count(
                    "category={category}",
                    "5m",
                    matchers='event_name=~"exec_timeout|exec[(]timeout[)]"',
                ),
                event_count(
                    "category={category}",
                    "5m",
                    matchers='event_name=~"exec_cancelled|exec[(]cancelled[)]"',
                ),
                event_count("category={category}", "5m", matchers='event_name="exec_node_timeout"'),
                # other: every exec* event outside the known spellings. Stream
                # selector matchers are full-string regexes, so the selector
                # keeps exactly the named spellings out (the pre-selector
                # pipeline form was substring-based and matched nothing).
                # exec_envelope is excluded by PM ruling (2026-08-31): it is the
                # execution envelope, not an outcome-accounting event — its
                # volume (~3x the ok rate) would dominate and misread as unknown
                # exec failures; its transfer-cost display is tracked separately
                # (task #2174).
                event_count(
                    "category={category}",
                    "5m",
                    matchers=(
                        'event_name=~"exec.*", '
                        'event_name!~"exec|exec_envelope|exec_failed|exec[(]failed[)]|exec_timeout|'
                        "exec[(]timeout[)]|exec_cancelled|exec[(]cancelled[)]|"
                        'exec_node_timeout"'
                    ),
                ),
            ],
            target_names=[
                "ok",
                "failed",
                "timeout",
                "cancelled",
                "node_timeout",
                "other",
            ],
            output=["grafana"],
            panel_id=25,
            section="Gateway & execution",
            order=4,
            position=(12, 120),
        )
    )

    # ── code repair (syntax_fix) ─────────────────────────────────────────────────

    specs.append(
        MetricSpec(
            name="ava_obs_syntax_fix_by_kind",
            title="Syntax fix triggers by kind",
            time_basis="per_minute",
            description=(
                "Syntax-fix trigger counts per minute (5-minute buckets / 5), "
                "bucketed by the fix kinds in attributes.fixes (substring regex "
                "matching; an event with several fix kinds counts once per kind, "
                "'none'/null/unknown goes to other). event_name='syntax_fix', "
                "category='telemetry' (90d retention)."
            ),
            event_name="syntax_fix",
            category="telemetry",
            unit="short",
            panel="timeseries",
            query_type="logql",
            query=event_count(
                f'category={{category}} | {_FIX_ATTR["fixes"]}=~".*ruff_format.*"',
                "5m",
                matchers="event_name={event_name}",
            ),
            targets=[
                event_count(
                    f'category={{category}} | {_FIX_ATTR["fixes"]}=~".*ruff.*" | {_FIX_ATTR["fixes"]}!~".*ruff_format.*"',
                    "5m",
                    matchers="event_name={event_name}",
                ),
                event_count(
                    f'category={{category}} | {_FIX_ATTR["fixes"]}=~".*invalid_escape.*"',
                    "5m",
                    matchers="event_name={event_name}",
                ),
                event_count(
                    f'category={{category}} | {_FIX_ATTR["fixes"]}=~".*missing_imports.*"',
                    "5m",
                    matchers="event_name={event_name}",
                ),
                event_count(
                    f'category={{category}} | {_FIX_ATTR["fixes"]}=~".*chinese_punct.*"',
                    "5m",
                    matchers="event_name={event_name}",
                ),
                event_count(
                    f'category={{category}} | {_FIX_ATTR["fixes"]}=~".*bracket_matching.*"',
                    "5m",
                    matchers="event_name={event_name}",
                ),
                # other: missing/none/unknown kinds — !~ matches lines where the
                # label is absent (empty), which is the SQL `IS NULL` branch.
                event_count(
                    f"category={{category}} | "
                    f'{_FIX_ATTR["fixes"]}!~".*(ruff|invalid_escape|missing_imports|chinese_punct|bracket_matching).*"',
                    "5m",
                    matchers="event_name={event_name}",
                ),
            ],
            target_names=[
                "ruff_format",
                "ruff",
                "invalid_escape",
                "missing_imports",
                "chinese_punct",
                "bracket_matching",
                "other",
            ],
            output=["grafana"],
            panel_id=26,
            section="Gateway & execution",
            order=5,
        )
    )

    # ── lifecycle (audit) ────────────────────────────────────────────────────────

    specs.append(
        MetricSpec(
            name="ava_obs_spawn_by_spawner",
            title="Agent spawns / minute (by source)",
            description=(
                "Spawn rate per minute: counts bucketed at $__interval and "
                "normalized (bucket count / interval seconds * 60). The source "
                "label carries the spawner and is rendered directly in the chart "
                "legend. event_name='spawn', category='audit'."
            ),
            event_name="spawn",
            category="audit",
            unit="short",
            panel="barchart",
            query_type="logql",
            query=(
                'sum by (source) (count_over_time({service_name="unknown_service", event_name={event_name}} | json | '
                "category={category} [$__interval])) "
                "/ ($__interval_ms / 60000)"
            ),
            output=["grafana"],
            panel_id=27,
            section="Fleet",
            order=0,
            target_names=["{{source}}"],
            # Rotate the crowded labels on the half-width Fleet pair (task #4204).
            options={"xTickLabelRotation": -45},
        )
    )

    specs.append(
        MetricSpec(
            name="ava_obs_lifecycle_counts",
            title="Agent lifecycle / minute",
            description=(
                "Lifecycle-event rate per minute: counts bucketed at $__interval and "
                "normalized (bucket count / interval seconds * 60), grouped by event "
                "name: spawn / terminate / restart / resurrect / fork. "
                "category='audit'."
            ),
            event_name="spawn",
            category="audit",
            unit="short",
            panel="barchart",
            query_type="logql",
            query=(
                'sum by (event_name) (count_over_time({service_name="unknown_service", event_name=~"^(spawn|terminate|restart|resurrect|fork)$"} | json | '
                "category={category} "
                "[$__interval])) / ($__interval_ms / 60000)"
            ),
            output=["grafana"],
            panel_id=28,
            section="Fleet",
            order=1,
            target_names=["{{event_name}}"],
            # Rotate the crowded labels on the half-width Fleet pair (task #4204).
            options={"xTickLabelRotation": -45},
        )
    )

    # ── SDK usage ────────────────────────────────────────────────────────────────

    specs.append(
        MetricSpec(
            name="ava_obs_sdk_call_top",
            title="SDK calls (Top 20)",
            description=(
                "Top 20 sdk_call grouped by attributes.fn — runtime call-"
                "frequency ranking, replacing the retired Metrics page's sdk_usage "
                "text ranking. event_name='sdk_call', category='telemetry'. Table "
                "panel (instant query over the whole window)."
            ),
            event_name="sdk_call",
            category="telemetry",
            unit="short",
            panel="table",
            query_type="logql",
            query=(
                f'topk(20, sum by ({_SDK_ATTR["fn"]}) (sum_over_time({{service_name="unknown_service", event_name={{event_name}}}} | json | '
                f'category={{category}} | unwrap {_SDK_ATTR["sample_rate"]} | __error__="" [$__range])))'
            ),
            target_names=["{{attributes_fn}}"],
            output=["grafana"],
            panel_id=29,
            section="Gateway & execution",
            order=7,
        )
    )

    # ── per-agent LLM usage (the retired Metrics page's per-agent breakdown) ─────

    specs.append(
        MetricSpec(
            name="ava_obs_agent_llm_usage_table",
            title="Per-agent LLM usage (Top 20)",
            description=(
                "Per-agent LLM call count / in / out tokens / cost (window-"
                "accumulated; cost via the cost_usd payload field, same as the "
                "LLM cost panel; agents without cost_usd count NULL). Restores "
                "the retired Metrics page's per-agent breakdown table "
                "(2026-08-06 user request: 'bring back the big pile of metrics "
                "like SDK calls'). event_name='llm_usage', category='telemetry', "
                "Top 20 by cost descending. One instant target per column; "
                "Grafana joins them on the shared agent_id label."
            ),
            event_name="llm_usage",
            category="telemetry",
            unit="short",
            panel="table",
            query_type="logql",
            query=(
                'sum by (agent_id) (count_over_time({service_name="unknown_service", event_name={event_name}, agent_id!=""} | json | '
                "category={category} [$__range]))"
            ),
            targets=[
                f'sum by (agent_id) (sum_over_time({{service_name="unknown_service", event_name={{event_name}}, agent_id!=""}} | json | '
                f"category={{category}} | "
                f"unwrap {_LLM_ATTR['in_total']} [$__range]))",
                f'sum by (agent_id) (sum_over_time({{service_name="unknown_service", event_name={{event_name}}, agent_id!=""}} | json | '
                f"category={{category}} | "
                f"unwrap {_LLM_ATTR['out_total']} [$__range]))",
                f'sum by (agent_id) (sum_over_time({{service_name="unknown_service", event_name={{event_name}}, agent_id!=""}} | json | '
                f"category={{category}} | "
                f"unwrap {_LLM_ATTR['cost_usd']} [$__range]))",
            ],
            target_names=["{{agent_id}}", "{{agent_id}}", "{{agent_id}}", "{{agent_id}}"],
            output=["grafana"],
            panel_id=30,
            section="LLM",
            order=7,
        )
    )

    # ── halt classification ──────────────────────────────────────────────────────

    specs.append(
        MetricSpec(
            name="ava_obs_halt_breakdown",
            title="Halt classes",
            time_basis="per_minute",
            description=(
                "Halt events classified by body per minute (5-minute buckets / "
                "5): idle ('no tool_call (idle)'), compact ('system_halt "
                "(compact)'), lifecycle ('lifecycle AgentTermination / "
                "AgentRestart'), other. event_name='halt', category='telemetry'."
            ),
            event_name="halt",
            category="telemetry",
            unit="short",
            panel="timeseries",
            query_type="logql",
            query=event_count(
                f'category={{category}} | {_HALT_ATTR["body"]}="no tool_call (idle)"',
                "5m",
                matchers="event_name={event_name}",
            ),
            targets=[
                event_count(
                    f'category={{category}} | {_HALT_ATTR["body"]}=~".*compact.*"',
                    "5m",
                    matchers="event_name={event_name}",
                ),
                event_count(
                    f'category={{category}} | {_HALT_ATTR["body"]}=~"lifecycle .*"',
                    "5m",
                    matchers="event_name={event_name}",
                ),
                # other: not idle/compact/lifecycle (missing body matches too —
                # the SQL `body IS NULL` branch).
                event_count(
                    f"category={{category}} | "
                    f'{_HALT_ATTR["body"]}!="no tool_call (idle)" | '
                    f'{_HALT_ATTR["body"]}!~".*compact.*" | '
                    f'{_HALT_ATTR["body"]}!~"lifecycle .*"',
                    "5m",
                    matchers="event_name={event_name}",
                ),
            ],
            target_names=["idle", "compact", "lifecycle", "other"],
            output=["grafana"],
            panel_id=31,
            section="Gateway & execution",
            order=6,
        )
    )

    # ── delivery health ──────────────────────────────────────────────────────────

    for event_name, title, target_name, panel_id, order in (
        ("delivery_stalled", "Delivery stalled", "stalled", 32, 2),
        ("delivery_poisoned", "Delivery poisoned", "poisoned", 52, 3),
    ):
        specs.append(
            MetricSpec(
                name=f"ava_obs_{event_name}_count",
                title=title,
                time_basis="window",
                description=(
                    f"Windowed {event_name} total — a delivery-watchdog row signal "
                    "(a raw window count, not a per-minute rate). "
                    f"event_name='{event_name}', category='telemetry'."
                ),
                event_name=event_name,
                category="telemetry",
                unit="short",
                panel="stat",
                query_type="logql",
                query=event_count(
                    "category={category}", "$__range", matchers="event_name={event_name}"
                ),
                target_names=[target_name],
                panel_id=panel_id,
                section="Fleet",
                order=order,
                field_defaults={"color": {"mode": "palette-classic"}},
                width=6,
                output=["grafana"],
            )
        )

    specs.append(
        MetricSpec(
            name="ava_obs_agent_delivery_stalled_count",
            title="Agent delivery backlog",
            description=(
                "Inspector-only (W13b): same as delivery_stalled_count, filtered "
                "per agent — {{agent_id}} is rendered by the gateway as "
                'agent_id="<n>", showing one agent\'s backlog level.'
            ),
            event_name="delivery_stalled",
            category="telemetry",
            unit="short",
            panel="timeseries",
            query_type="logql",
            query=event_count(
                "category={category} | {{agent_id}}",
                "$__interval",
                matchers="event_name={event_name}",
            ),
            target_names=["stalled"],
            output=["inspector"],
        )
    )

    # ── total stream rate ────────────────────────────────────────────────────────

    specs.append(
        MetricSpec(
            name="ava_obs_events_rate",
            title="Event rate (events/s)",
            description=(
                "Total event-stream rate per bucket — rate() over the whole "
                "stream, events per second (the SQL count * 1000 / interval_ms "
                "equivalent). The query covers all event_name/category (the "
                "event_name/category fields are nominal metadata, not in WHERE)."
            ),
            event_name="log",
            category="log",
            unit="ops",
            panel="timeseries",
            query_type="logql",
            query='sum(rate({service_name="unknown_service"} | json | __error__="" [1m]))',
            target_names=["events_per_s"],
            output=["grafana"],
            panel_id=33,
            section="core",
            order=15,
        )
    )
    return specs
