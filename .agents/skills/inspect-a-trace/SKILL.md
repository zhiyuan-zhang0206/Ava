---
name: inspect-a-trace
description: Reconstructs one real agent run across checkpoints, Loki, Tempo, and Grafana. Use when investigating what an agent actually did, tracing a failure, correlating run IDs, or giving the user a live trace link.
---

# Inspect a trace

A run is not a document. It is four live surfaces holding four different
things, tied together by two ids. This skill is the correlation know-how: how
to get from "agent 3048 behaved badly around 14:00" to that stretch of the
conversation, its events, its span tree, and a link the user can open.

Nothing trace-shaped is committed to the repo — there is no `traces/` doc axis
(retired 2026-08-19, [why](../../../decisions/2026-08-19-retire-the-traces-doc-axis.md)).
The evidence is queried live, against the version that is actually running.

## The four surfaces

| Surface | Holds | Reach it with |
|---|---|---|
| **Postgres checkpoints** | the conversation itself — prompts, tool calls, results, system prompt | SQL + `shared.checkpoint` ([find-the-run](references/find-the-run.md)) |
| **Loki** | the unified event river — every `exec`, `llm_usage`, `turn_end`, `halt`, lifecycle event | LogQL, or `GET /api/events` ([event-stream](references/event-stream.md)) |
| **Tempo** | the span tree of one traced stretch — durations, langgraph node path, LLM call shape | TraceQL + the local JSONL mirror ([spans](references/spans.md)) |
| **Grafana** | the human-browsable view of both | a constructed URL (below) |

**Loki is the primary surface.** An agent is long-running and unbounded; its
event river is the only place that shows the whole life of it. Tempo is
drill-down for a *bounded* unit of work, and only once you already know which
piece of work you care about. Start in the river, narrow to a trace, then open
the spans.

## The two correlation ids

Everything hangs off these. Get one, and the rest follows.

- **`agent_id`** — the agent. It is also the LangGraph `thread_id`, as a decimal
  string (`checkpoints.thread_id = '3048'`), the Loki line field
  (`| agent_id="3048"`), and the root span's `session.id`.
- **`trace_id`** — one turn of graph work, lowercase 32-hex. It is the Loki line
  field (`| trace_id="<hex>"`), the Tempo trace id, and — stamped per turn onto
  the checkpoint the turn committed — `checkpoints.metadata->>'trace_id'`. The
  reverse link is the root span's `ava.checkpoint_id` attribute.

**One trace is exactly one turn.** The root span (`shared/trace.py:turn_span`)
wraps one `graph.ainvoke`, and the runloop invokes the graph once per turn —
the claim node ends the invocation at the turn boundary, so the trace exports
when the turn ends. The root keeps the name `ava-agent-{agent_id}` and the
`session.id` attribute, and carries `ava.turn` (int, from 1, **per process** —
it repeats across restarts; order turns across processes by timestamp or
`ava.checkpoint_id`, never by `ava.turn`). Runs recorded before 2026-08-20
used a session-scoped root (`session_span`) that wrapped the whole process
life — an old run in the mirror or Tempo can span many turns, so check the
root's bounds before reasoning about its span count or duration.

The checkpoint stamp is deliberately failure-tolerant
(`agent/_trace_checkpoint.py`), so **not every checkpoint carries a
`trace_id`**. A missing link is one lost correlation, not a broken system.

Spans are **metadata-only** (trace v2): prompts and completions are stripped at
record time (`shared/trace.py`). Content comes back from the checkpoint, on
demand, by trace id — never from Tempo.

## The path

```
symptom ──▶ Loki: one agent's event river ──▶ a trace_id ──▶ Tempo spans (timing / node path)
                                                └──────────▶ checkpoint messages (what was said)
```

1. **Find the run.** Narrow by agent and time window in Loki; or go through
   Postgres when you want the *content* rather than the events, or need
   history across compaction segments. → [references/find-the-run.md](references/find-the-run.md)
2. **Read the event stream.** The `{service_name="unknown_service"} | json`
   dialect, exec outcomes, `llm_usage`, halt reasons.
   → [references/event-stream.md](references/event-stream.md)
3. **Read the spans.** TraceQL to locate, the mirror to fetch whole, the
   toolchain scripts to read and render. → [references/spans.md](references/spans.md)
4. **Hand the user a link** (below).

## Hand the user a link

For anything a human should look at themselves, build the Grafana Explore URL
rather than pasting a span dump. Grafana runs on the LGTM host at
`http://localhost:3003` (anonymous viewer; `ava lgtm status` to check it is up,
`ava lgtm on` to bring it up). Override with `AVA_GRAFANA_URL` when it is on
another box.

```python
import json, os, urllib.parse

grafana = os.environ.get("AVA_GRAFANA_URL", "http://localhost:3003")
pane = {"explore": {"datasource": "tempo",
                    "queries": [{"refId": "A", "queryType": "traceId", "query": trace_id}],
                    "range": {"from": "now-1d", "to": "now"}}}
url = f"{grafana}/explore?schemaVersion=1&panes={urllib.parse.quote(json.dumps(pane, separators=(',', ':')))}&orgId=1"
```

`scripts/render_trace.py` builds exactly this link into its report. Datasource
uids are `tempo` / `loki` / `prometheus`; swap `datasource` and give a LogQL
`expr` query to link the event stream instead.

## The toolchain scripts

`scripts/` holds three chained steps, each runnable standalone. The loader
prints this skill's path (`ava.help(ava.skills.inspect_a_trace)`); in a repo
worktree it is `.agents/skills/inspect-a-trace/scripts/`, abbreviated
`<scripts>/` below.

```
fetch_trace.py ─▶ trace_raw.json ─▶ read_trace.py ─▶ trace_read.json ─▶ render_trace.py ─▶ report/ ─▶ ava.ui.serve
```

They encode the conversions you would otherwise re-derive every time (base64 vs
hex ids, OTLP envelopes, the 5 MB full-trace cap, gateway bearer auth). Details
and the verified query set: [references/spans.md](references/spans.md).

## Where this skill does not apply

- Fleet-wide trends and dashboards — those are pre-shaped questions; use the
  core-metrics dashboards (`shared/core_metrics_observability.py`) and the
  alert rules, not run-level correlation.
- Deploying or operating the LGTM stack — `deploy/lgtm/README.md`, `ava lgtm`.
- Computer-use traces (`/api/computer/traces`) — a different subsystem.
