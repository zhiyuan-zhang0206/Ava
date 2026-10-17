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

_LLM_ATTR = {k: f"attributes_{k}" for k in LLM_USAGE_KEYS}
_TURN_ATTR = {k: f"attributes_{k}" for k in TURN_END_KEYS}
_HALT_ATTR = {k: f"attributes_{k}" for k in HALT_KEYS}
_FIX_ATTR = {k: f"attributes_{k}" for k in SYNTAX_FIX_KEYS}
_FRONTEND_ATTR = {k: f"attributes_{k}" for k in FRONTEND_INTERACTION_KEYS}
_SDK_ATTR = {k: f"attributes_{k}" for k in SDK_CALL_KEYS}


def core_metrics() -> list[MetricSpec]:
    # meiva:ref base/telemetry/metrics/docs/core-observability.meiva.okf.md#query-measurement-scope
    # meiva:ref base/telemetry/metrics/docs/core-observability.meiva.okf.md#compaction-effectiveness-denominator
    specs: list[MetricSpec] = []
    specs.append(
        MetricSpec(
            name="meiva_obs_llm_cost_usd",
            title="LLM cost",
            time_basis="per_minute",
            description=(
                "LLM call cost per minute — unwrap of the cost_usd field every "
                "llm_usage payload carries (task #2626; the producer computes it "
                "from one versioned-catalog quote in base/lm/pricing/__init__.py, unpriced "
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
            name="meiva_obs_agent_llm_cost_usd",
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
            name="meiva_obs_llm_error_rate",
            title="LLM errors",
            time_basis="per_minute",
            description=(
                "LLM failure signals per minute (5-minute buckets / 5): "
                "llm_provider_error (provider rejection / classification error) "
                "and llm_turn_aborted (turn aborted after retries exhausted). "
                "The event_name field is spec metadata; the query covers both "
                "failure events (all telemetry)."
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
                event_count("category={category}", "5m", matchers='event_name="llm_turn_aborted"'),
            ],
            target_names=["provider_error", "turn_aborted"],
            output=["grafana", "inspector"],
            panel_id=22,
            section="LLM",
            order=6,
        )
    )

    specs.append(
        MetricSpec(
            name="meiva_obs_turn_ok_rate",
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
            name="meiva_obs_turn_duration_s",
            title="Turn duration (p50/p95)",
            description=(
                "p50 and p95 of the Prometheus meiva_turn_end_duration_seconds "
                "histogram. The p95 uses the same source and quantile as alert rule "
                "R18: histogram_quantile(0.95, sum by (le) "
                "(rate(meiva_turn_end_duration_seconds_bucket[10m])))."
            ),
            event_name="turn_end",
            category="telemetry",
            unit="s",
            panel="timeseries",
            query_type="promql",
            query=(
                "histogram_quantile(0.95, sum by (le) "
                "(rate(meiva_turn_end_duration_seconds_bucket[10m])))"
            ),
            targets=[
                "histogram_quantile(0.5, sum by (le) "
                "(rate(meiva_turn_end_duration_seconds_bucket[10m])))",
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

    specs.append(
        MetricSpec(
            name="meiva_obs_compaction_summary_history_ratio",
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
            name="meiva_obs_compaction_rate",
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
            targets=[
                event_count(
                    "category={category} "
                    f'| attributes_compact_kind="{kind}" | attributes_reminder_shown="{reminded}"',
                    "5m",
                    matchers="event_name={event_name}",
                )
                for kind, reminded in (
                    ("compact_summary", "true"),
                    ("compact_summary", "false"),
                    ("auto", "true"),
                    ("auto", "false"),
                )
            ],
            target_names=[
                "all/min",
                "soft after reminder/min",
                "proactive/min",
                "hard after reminder/min",
                "hard without reminder/min",
            ],
            output=["grafana"],
            thresholds=[],
            panel_id=51,
            section="Cost analysis",
            order=6,
        )
    )

    automatic_windows = event_count(
        'category={category} | attributes_compact_kind=~"compact_summary|auto" '
        '| attributes_reminder_shown=~"true|false"',
        "$__range",
        matchers="event_name={event_name}",
    )
    soft_windows = event_count(
        'category={category} | attributes_compact_kind="compact_summary" '
        '| attributes_reminder_shown=~"true|false"',
        "$__range",
        matchers="event_name={event_name}",
    )
    reminded_windows = event_count(
        'category={category} | attributes_compact_kind="compact_summary" '
        '| attributes_reminder_shown="true"',
        "$__range",
        matchers="event_name={event_name}",
    )
    specs.append(
        MetricSpec(
            name="meiva_obs_compaction_soft_share",
            title="Soft compactions (target >=95% after reminder)",
            description=(
                "Agent-authored completed compactions / (agent-authored + hard automatic). "
                "Manual compact requests and events predating reminder attribution are excluded. "
                "Zero soft completions with hard completions is 0%; no completions is no data. "
                "The second ratio counts only agent-authored completions after a soft reminder: "
                "this is the preparation coverage target, so proactive compactions cannot "
                "hide missing plugin delivery. Neither ratio proves files were saved."
            ),
            event_name="compaction_completed",
            category="telemetry",
            unit="percent",
            panel="stat",
            query_type="logql",
            query=f"100 * ({soft_windows} or (0 * {automatic_windows})) / {automatic_windows}",
            targets=[
                f"100 * ({reminded_windows} or (0 * {automatic_windows})) / {automatic_windows}"
            ],
            target_names=["agent-authored %", "after soft reminder %"],
            output=["grafana"],
            thresholds=[],
            panel_id=52,
            section="Cost analysis",
            order=7,
        )
    )

    specs.append(
        MetricSpec(
            name="meiva_obs_exec_success_rate",
            title="Exec outcomes",
            time_basis="per_minute",
            description=(
                "Exec outcome breakdown per minute (5-minute buckets / 5): ok = "
                "event_name='exec'; failures split by event_name (exec_failed / "
                "exec_timeout / exec_cancelled / exec_node_timeout; unknown exec* "
                "events fall into other). event_name='exec', category='telemetry'."
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
                    matchers='event_name="exec_failed"',
                ),
                event_count(
                    "category={category}",
                    "5m",
                    matchers='event_name="exec_timeout"',
                ),
                event_count(
                    "category={category}",
                    "5m",
                    matchers='event_name="exec_cancelled"',
                ),
                event_count("category={category}", "5m", matchers='event_name="exec_node_timeout"'),
                event_count(
                    "category={category}",
                    "5m",
                    matchers=(
                        'event_name=~"exec.*", '
                        'event_name!~"exec|exec_envelope|exec_failed|exec_timeout|'
                        'exec_cancelled|exec_node_timeout"'
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

    specs.append(
        MetricSpec(
            name="meiva_obs_syntax_fix_by_kind",
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

    specs.append(
        MetricSpec(
            name="meiva_obs_spawn_by_spawner",
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
            options={"xTickLabelRotation": -45},
        )
    )

    specs.append(
        MetricSpec(
            name="meiva_obs_lifecycle_counts",
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
            options={"xTickLabelRotation": -45},
        )
    )

    specs.append(
        MetricSpec(
            name="meiva_obs_sdk_call_top",
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

    specs.append(
        MetricSpec(
            name="meiva_obs_agent_llm_usage_table",
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

    specs.append(
        MetricSpec(
            name="meiva_obs_halt_breakdown",
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

    specs.append(
        MetricSpec(
            name="meiva_obs_delivery_stalled_count",
            title="Delivery stalled",
            time_basis="window",
            description=(
                "Windowed delivery_stalled total — a delivery-watchdog row signal "
                "(a raw window count, not a per-minute rate). "
                "event_name='delivery_stalled', category='telemetry'."
            ),
            event_name="delivery_stalled",
            category="telemetry",
            unit="short",
            panel="stat",
            query_type="logql",
            query=event_count(
                "category={category}", "$__range", matchers="event_name={event_name}"
            ),
            target_names=["stalled"],
            panel_id=32,
            section="Fleet",
            order=2,
            field_defaults={"color": {"mode": "palette-classic"}},
            width=6,
            output=["grafana"],
        )
    )

    specs.append(
        MetricSpec(
            name="meiva_obs_agent_delivery_stalled_count",
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

    specs.append(
        MetricSpec(
            name="meiva_obs_events_rate",
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
