# Read the event stream — LogQL over Loki

Loki holds the unified event river: every agent, every service, one stream
family. This is where a run investigation starts, because it is the only
surface that covers an unbounded agent life rather than one bounded trace.

Reach it three ways: `GET /api/events` on the gateway (already shaped into
rows), `logcli`/`curl` against `http://127.0.0.1:3100`, or Grafana Explore.

## The dialect

There is exactly one stream selector, and it is not negotiable:

```logql
{service_name="unknown_service"} | json
```

Two rules the whole tree obeys, both learned the hard way:

- **`| json` comes first, always.** Event fields (`event_name`, `level`,
  `category`, `agent_id`, `trace_id`, …) are OTel *structured metadata*, not
  stream labels — a `{...}` selector cannot match them. Every alert rule and
  every core metric pipelines `| json` before any field filter.
- **Wrap every count in `sum(...)`.** The `unknown_service` family runs to
  hundreds of streams a day; an unaggregated `count_over_time` hits Loki's
  per-query series cap.

Retention is 168h. Older than that, the run is gone from Loki.

## The line shape

Each line's body is the whole event as JSON:

```
ts, trace_id, span_id, agent_id, machine, process, category, event_name,
level, source, target_agent_id, attributes
```

`| json` flattens the nested payload with underscores: `attributes.cost_usd`
becomes `attributes_cost_usd`, and numbers parse as numbers, so
`| attributes_age_s < 600` is a real numeric comparison.

`category` is `audit | telemetry | log`. `level` is
`debug | info | warning | error | critical`, lowercase. The `event_name`
vocabulary is the registry in `shared/events/contract.py` — read it there
rather than guessing a name.

**Multiple extractions in one `| json` stage is a parse error.** Each field
needs its own stage: `| json model="attributes.model" | json cost_usd="attributes.cost_usd"`.

## Narrowing to one run

```logql
{service_name="unknown_service"} | json | agent_id="3048"
```

```logql
{service_name="unknown_service"} | json | trace_id="<lowercase-32-hex>"
```

The trace filter gives one traced stretch's whole call chain. The gateway
lowercases the id before matching — so should you. Service-level (non-agent)
rows are `| agent_id=""`.

Over HTTP, the same two narrowings:

```
GET /api/events?agent_id=3048&hours=6
GET /api/events?trace_id=<hex>&from=<ISO-with-offset>
```

`from` must carry a timezone offset. Absent both `from` and `hours` the window
is forced to the last 24h — never unbounded. `limit` caps at 1000; paging is
`offset`, and an exact count is opt-in via `with_total=1`.

## What went wrong — the recipes

**Exec outcomes.** The outcome is the event name, not an attribute:
`exec` is success; failures are `exec_failed`, `exec_timeout`,
`exec_cancelled`, `exec_node_timeout`. Legacy spellings still appear in
old data: parenthesized names such as `exec(failed)`, and
`exec_thread_stuck` (emitted by the in-process thread backend, removed
2026-08-21).

```logql
sum(count_over_time({service_name="unknown_service"} | json | agent_id="3048" | event_name=~"exec_.+|exec[(].*" [1h]))
```

The failing code itself rides on `attributes_body`; `exec_failed` also carries
`attributes_exc_type`.

**LLM usage and spend.** `event_name="llm_usage"` carries `attributes_model`,
`attributes_calls`, `attributes_in_total`, `attributes_out_total`,
`attributes_cache_read`, `attributes_reasoning`, `attributes_latency_ms`,
`attributes_cost_usd`.

```logql
sum(sum_over_time({service_name="unknown_service"} | json | agent_id="3048" | event_name="llm_usage" | unwrap attributes_cost_usd [$__range]))
```

**Turn outcomes.** `event_name="turn_end"` carries `attributes_ok` (the string
`"true"`/`"false"`) and `attributes_duration_seconds`.

```logql
sum(count_over_time({service_name="unknown_service"} | json | agent_id="3048" | event_name="turn_end" | attributes_ok="false" [24h]))
```

**Halt reasons.** `event_name="halt"`, reason in `attributes_body`. The four
observed classes: `"no tool_call (idle)"`, `"system_halt (compact)"`,
`"lifecycle AgentTermination"`, `"lifecycle AgentRestart"`. Match a class with
`| attributes_body=~"lifecycle .*"`.

**Everything that raised a flag.**

```logql
{service_name="unknown_service"} | json | agent_id="3048" | level=~"warning|error|critical"
```

**Crash loops.** `event_name="resurrect"`, `category="audit"` — one row per
resurrection.

## Quantiles lie a little

Loki's `quantile_over_time` is per-series, and each event is its own series.
The core dashboards aggregate per-stream quantiles with `max()`, which tilts a
p50 toward the slowest agent process. `max` itself is exact; treat a p50 read
off a dashboard as an upper-ish estimate, not a median.

## Raw session logs are a different namespace

Agent stdout, shipped by promtail, uses `service` (not `service_name`) and has
no JSON structure:

```logql
{service="ava-agent-1818"}
{service=~"ava-agent-.+"} |~ "(?i)traceback"
{job="ava-sessions"} |= "error"
```

Go here for a stack trace the event stream only summarizes.

## Where things listen

| Component | URL |
|---|---|
| Loki | `http://127.0.0.1:3100` (`/loki/api/v1/query_range`, `/loki/api/v1/query`) |
| Grafana | `http://localhost:3003` (anonymous viewer; datasource uid `loki`) |
| Gateway | `http://localhost:8000/api/events` (bearer `AVA_CLUSTER_SECRET`) |

`ava lgtm status` reports the marker, containers, and readiness probes; the
whole stack only exists on the host holding the `$AVA_HOME/lgtm-host` marker.
