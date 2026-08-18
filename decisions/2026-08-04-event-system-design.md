---
type: decision
title: Ava event system — unified event model (Event Stream)
description: Design ruling 2026-08-04: Ava's event system designed from scratch (target 1000 agents); every signal is one event (unified schema + trace_id); the four legacy mechanisms are four faces of the same event stream; storage scales by tier (Postgres → ClickHouse).
tags: [events, observability, telemetry, audit, architecture, o11y]
date: 2026-08-04
status: accepted
---

# Ava event system — unified event model (Event Stream)

> Design source: an agent workspace design note (built on a research task +
> the user's ruling "ignore migration cost, design for the best").
> This document records only the **model-layer** decisions (§0 one-sentence
> model + §1 unified model + the positioning of the four mechanisms);
> tiered architecture / component selection / rollout order live in the
> design document and its wave breakdown.

## Context

Ava's "events" were four coexisting, unrelated mechanisms (measured 2026-08-04):

| # | Mechanism | What it is | Stored in | Scale (measured) |
|---|-----------|------------|-----------|------------------|
| 1 | `event_log` table | Structured **operation audit**: who did what to whom | Postgres, no partitioning | 138k rows / 48 days |
| 2 | `agent_events` table | loguru **telemetry sink**: runtime observation (token/turn/exec/log lines) | Postgres, monthly partitions | 5.877M rows / 73 days, 2.14GB |
| 3 | OTel trace | LLM call **traces** (spans), OpenLLMetry-instrumented → JSONL mirror → Langfuse | Files `$AVA_HOME/traces/` | daily files, 7-day prune |
| 4 | SSE live stream | **non-persisted** live view: Redis pub/sub → gateway SSE → frontend | Redis (transient) | queue 2048, drops when full |

Pain points: three "event" notions mixed in docs and conversation; no registry,
no types for instrumentation (~40+ event names by naming convention, mostly
untyped payloads); bare `log` made up 50.8% of `agent_events`; `event_log` and
`agent_events` had no join key (traces fully separate from the event system).
At the target scale of 1000 agents (1 user × 3 machines × ~300 agents/machine)
the status quo does not scale.

## Decision

**Ava has one event stream. Every behavior — what an agent did, how the system
is healthy, what happened inside one LLM call — is a drop in the stream
(Event), sharing one shape (schema) and one bottle number (trace_id). Three
tools take water from the stream: the log bucket (ask "what happened"), the
metric ruler (measure "overall health"), and the trace mirror (see "inside one
call") — the industry's three signals. One display: Grafana. When the scale
grows, the channel widens from a stream (Postgres) to a river (ClickHouse),
but the water is the same — the concept does not change, only the pipe.**

Unified event model (all signals are one event; physical storage may differ,
the concept is single):

```yaml
Event:
  ts:            timestamptz          # when it happened
  trace_id:      string               # 🔑 join key: one turn = one trace, all related events carry it
  span_id:       string               # the specific step (LLM call / exec / node)
  agent_id:      uint32 | null        # owning agent (null = service-level: gateway/daemon)
  machine:       string               # machine (mandatory for multi-machine; missing today!)
  process:       string               # gateway / watchdog / agent-kernel / ...
  category:      audit | telemetry | log   # drives retention policy and permissions
  event_name:    string               # llm_usage / turn_end / spawn / send_message / log ... (OTel event.name, formerly "kind")
  level:         debug | info | warning | error
  source:        string               # agent:123 / user / system / self
  target_agent_id: uint32 | null      # the object of business events (spawn/message)
  attributes:    map                  # free-form fields (the old payload, stays flexible)
```

### Vocabulary (term alignment 2026-08-06)

Terms align with the industry 2026 model (OTel: events = logs with names,
LogRecord + event.name):

| This system | Aligned term | Meaning |
|-------------|--------------|---------|
| Event | LogRecord | every event = a named LogRecord (OTel 2026: events are logs with names) |
| kind | event.name | the event name = the LogRecord name field; code/DB field uniformly named `event_name` (`kind` is the old name, renamed) |
| SSE | live projection | SSE is a real-time projection of the stream's latest drops — not persisted, unlike durable events |
| category | Ava extension | OTel LogRecord has no such field; Ava uses it to drive retention (audit 365d+ / telemetry 90d / log 30d), query permissions, and alerting |

Three key points:

1. **`trace_id` is the mandatory join key of every event** (user ruling) — a
   log line, a metric anomaly, an event all jump to their call chain in one click.
2. **`machine` is a new mandatory dimension** — 1000 agents across 3 machines;
   without it "which machine" can never be answered.
3. **`category` is not decorative** — it drives three concrete things:
   **retention** (audit 365d+ / telemetry 90d / log 30d), **query permissions**
   (an audit-event anomaly = a security event), **alerting** (log noise does
   not alert).

### The four legacy mechanisms under the unified model

**The four things did not disappear — they were always four faces of the same
thing; the unified model states their relationship.**

| Legacy mechanism | Position under the unified model | category | Key changes |
|------------------|----------------------------------|----------|-------------|
| `event_log` table | the stream's **category=audit** part (business-operation facts) | audit | gains trace_id/machine; append-only immutability kept; TTL 365d+ |
| `agent_events` table | the stream's **category=telemetry + log** part (runtime observation) | telemetry + log | gains trace_id/machine; event_name folded into the registry; bare log demoted (L2 keeps WARNING+ only) |
| SSE live stream | the **real-time projection of the latest drops** to the frontend (not persisted) | — (transient) | same schema, same event_name conventions; not persisted, no retention |
| OTel trace | the **call-chain view** of events (trace_id strings them together) | — (trace channel) | `session_span` already carries session.id; the unified model makes metric anomalies jump to the chain |

### Event-name registry and naming

- event_name is globally unique and registered: `docs/events/registry.md`
  (full inventory of all pre-2026-08-04 event_names: audit 17 + telemetry ~60
  + log 1 + SSE role 27).
- Naming convention: `<domain>_<action>`, all-lowercase snake_case; bare logs
  forbidden; dynamic event_name values forbidden (agent_id etc. go into
  attributes); parentheses/hyphens forbidden (`exec(timeout)` →
  `exec_timeout`); new event_names must be registered before code is written.

## Alternatives rejected

| Option | Verdict |
|--------|---------|
| Keep the four mechanisms and unify terminology at the doc level | solves naming ambiguity only, not the missing trace join, the missing registry, the missing retention policy, or bare-log noise — storage and queries do not scale to 1000 agents |
| Physically merge the four into one table now | the "ignore migration cost" ruling bought design freedom, not an immediate migration; physical storage tiers (L1 Postgres / L2 ClickHouse), conceptual unification first |
| Hierarchical event_name namespaces (e.g. `ava.agent.turn_end`) | incompatible with the existing flat `llm_usage`/`turn_end` names, and OTel already has span hierarchy; flat + registry is enough |
| Fold `event_log` into `agent_events` (audit and telemetry in one table) | category already drives retention/permission differences — audit immutable 365d+ vs log 30d; one table would mix TTL and permission granularity; the two tables stay as audit / telemetry+log physical partitions |

## Consequences

- **Conceptual unification first, physical migration gradual**: Wave 1 lands
  the unified emitter + `events` table + trace_id throughout; the registry/docs
  (this document + `docs/events/registry.md`); Grafana alerting. Wave 2 builds
  the unified query API `/api/events` and TTL/retention. Wave 3 swaps in
  ClickHouse/Tempo/Mimir when scale triggers it — the interface stays.
- **All new instrumentation must**: use an explicit `event=`, a static
  event_name, register first; old event_names stay compatible during the
  migration window (label fallback only as migration compat).
- **Open items** (design doc §8): audit retention duration (365d recommended,
  user-configurable longer); bare-log demotion scope; metrics stay
  "collection = events, aggregate at query time".
- This document is a point-in-time snapshot; if superseded, open a new entry
  per the `decisions/README.md` rules and add a forward link here.

<!-- Superseded by: (none yet) -->
