# Read the spans — Tempo, the mirror, and the toolchain

Spans give you what the event stream cannot — the tree, the durations, the
langgraph node path, and the shape of each LLM call. Come here once you already
have a `trace_id`.

**Check the trace's scope first.** The root span (`shared/trace.py:turn_span`)
wraps one graph invocation in `services/agent_host/host.py`. The host admits
work before invoking the graph, so an idle agent has no parked graph invocation.
The claim node returns at idle; waiting for a Redis wake happens in the host
scheduler outside the turn span.

Historical traces can have different boundaries. The retired process runner
could include an inbound idle wait, sometimes recorded as a `claim idle-wait`
child span. Runs before 2026-08-20 could have a session-scoped root spanning
many turns. Read their span boundaries before interpreting wall-clock duration.

Three sources, in the order you should reach for them:

| Source | Where | Good for |
|---|---|---|
| **Tempo** | the cluster's Tempo query URL — `AVA_TELEMETRY_TEMPO_QUERY_URL` when Tempo is remote (see below), else `http://localhost:3200` | finding traces (indexed, cross-agent) |
| **Local mirror** | `$AVA_HOME/traces/spans*.jsonl` | fetching one whole trace (complete, no size cap, no network) |
| **Grafana** | `http://localhost:3003`, datasource uid `tempo` | letting a human browse |

Spans are **metadata-only**. Prompts, completions, tool arguments and results
are stripped at record time (`shared/trace.py`) — nothing you do to Tempo will
produce them. Content comes from the checkpoint
([find-the-run](find-the-run.md)).

**Tempo may not be on this host.** The LGTM host runs Loki, Prometheus, and
Grafana natively; Tempo is per-cluster config (`AVA_TELEMETRY_TEMPO_ENDPOINT`
for intake, `AVA_TELEMETRY_TEMPO_QUERY_URL` for queries — see
`deploy/lgtm/README.md`), so on a split deployment the query URL is the remote
Tempo and nothing listens on the local 3200. The scripts inherit
`AVA_TELEMETRY_TEMPO_QUERY_URL`; `AVA_TRACE_TEMPO_URL` overrides. To find the
URL by hand, read it from the running Grafana
(`GET /api/datasources` — the `tempo` datasource's `url` field).

## Find: TraceQL

All verified against Tempo 3.0.2.

| Want | Query |
|---|---|
| All traces | `{ resource.service.name = "ava" }` |
| One trace | `{ trace:id = "<hex-32>" }` |
| Spans of one agent | `{ span.traceloop.association.properties.agent_id = "3048" }` |
| All of one agent's turn roots | `{ span.session.id = "3048" }` |
| One specific turn | `{ span.session.id = "3048" && span.ava.turn = 17 }` |
| Spans in one langgraph node | `{ span.traceloop.association.properties.langgraph_node = "after_exec" }` |
| Spans by operation | `{ span.gen_ai.operation.name = "goto" }` |
| Exact span name | `{ name = "goto exec" }` |

Search parameters: `limit` (traces), `spss` (spans per span set, max 100),
`start`/`end` (unix seconds). Dotted attribute names work inside `span.*`.

Span attributes worth knowing: the root carries `session.id` (the agent id)
and `ava.turn` (the per-process turn counter); children carry
`traceloop.association.properties.langgraph_node` / `.langgraph_step` /
`.langgraph_path`, `traceloop.association.properties.ls_model_name`, and
`gen_ai.operation.name`. Since #1964 the root is a placeholder ended at turn
START (so a trace always has its root even when the process dies mid-turn),
which means it can no longer carry `ava.checkpoint_id` (an ended span drops
attribute writes) — the trace↔checkpoint link now lives DB-side only
(`checkpoints.metadata.trace_id`; `read_trace.py --with-content` joins it via
the gateway trace endpoint). These attributes come from OpenLLMetry's
auto-instrumentation of the LLM SDKs and LangChain/LangGraph, not from
explicit calls in this repo — so a library upgrade can rename them. Check
what a real span carries before building a query on an attribute you have not
seen.

## Fetch

```bash
# find first — search returns trace ids
.venv/bin/python <scripts>/fetch_trace.py --search '{ resource.service.name = "ava" && span.traceloop.association.properties.agent_id = "3048" }'

# then pull the one trace. Mirror is primary: complete, no cap, no network.
.venv/bin/python <scripts>/fetch_trace.py --trace-id <hex-32> --source mirror --out trace_raw.json
# Tempo's full-trace API is the off-box fallback, and it fails above 5 MB.
.venv/bin/python <scripts>/fetch_trace.py --trace-id <hex-32> --source tempo --out trace_raw.json
```

`--source mirror` needs filesystem access to `$AVA_HOME` (default `~/.ava`); on
another machine, query Tempo over the network or through the Grafana datasource
proxy (`/api/datasources/proxy/uid/tempo/...`).

The mirror is written by the OTel Collector sidecar's file exporter — one
OTLP/JSON request per line, active `spans.jsonl` plus rotated
`spans-<ISO>(-size|-time)?.jsonl` (64 MiB size rotation; timberjack appends
the trigger reason). Old segments are gzipped to `.jsonl.gz` by the
agent-side compression pass and retention-pruned by day; `fetch_trace.py`
and `ava trace ship` read them transparently. `ava trace ship` replays a
shed window into Tempo when the sidecar's live fan-out missed it. At peak
volume (~2.9 GB/day) the collector's 24-backup cap truncates the mirror to
roughly the last half day of segments — grep/replay windows older than that
need Tempo, not the mirror.

## Read

```bash
.venv/bin/python <scripts>/read_trace.py trace_raw.json --out trace_read.json \
  [--with-content] [--with-events] [--gateway http://localhost:8000]
```

Produces the span tree (parent/child, ordered), per-span offsets and durations,
the LLM span list (names ending `.chat`), the langgraph node sequence, and the
root's `session.id`. `checkpoint_id` comes from the root's `ava.checkpoint_id`
on pre-#1964 traces; on newer traces the root no longer carries it — pass
`--with-content` to resolve it from the checkpoints table via the gateway.

- `--with-content` fetches the messages from
  `GET /api/agents/{agent}/traces/{trace_id}/messages` — the checkpoint's full
  message list, system prompt included. `pruned: true` is the expected shape
  for an old run whose checkpoint was trimmed, not an error.
- `--with-events` fetches the correlated event rows via
  `GET /api/events?trace_id=<hex>&from=<ISO>`.
- Gateway auth: bearer `AVA_CLUSTER_SECRET`, read from `$AVA_HOME/.env` or the
  environment. The gateway is port **8000** (8100 is an unrelated service).

## Render

```bash
.venv/bin/python <scripts>/render_trace.py trace_read.json --out report/
```

Writes a self-contained `report/index.html` — inline CSS/JS, no CDN, no build
step, opens offline. Header, waterfall timeline, node sequence, LLM span table,
events, message content when present, and a Grafana Explore link. Serve it the one
supported way:

```python
import ava
ava.ui.serve("report", name="trace-<agent>-<short-id>", title="Trace <hex>")
```

The report and Grafana are complements: Grafana is the interactive browser, the
report is the sharable annotated artifact. Prefer just handing over the Grafana
link unless the annotation is the point.

## Pitfalls (all observed, Tempo 3.0.2)

- **5 MB full-trace cap** — `/api/traces/{id}` refuses larger traces outright.
- **`spss` caps at 100, and time-window paging is unreliable** — a window can
  come back empty while the trace exists outside it. "Reassemble a whole trace
  from search" is not a thing; use the mirror.
- **Regex `=~` on the `name` intrinsic misbehaves** — anchored patterns
  (`"^goto"`) match nothing, and `name =~ ... && <anything>` returns empty.
  Use exact `=` on names; `=~` on span attributes is fine.
- **Ids are hex everywhere in this stack** — the mirror (collector file
  exporter) and Tempo both carry 32/16-char hex. The base64 form only appears
  in legacy pre-#1266 agent-side mirror files; the scripts handle both, by
  hand it is `bytes.fromhex(s)` (or `base64.b64decode(s).hex()` for a legacy
  line). Never paste one form into the other.
- **`/api/v2/search/tag/*` only enumerates intrinsics** (name/kind/status), not
  attributes — it is not a schema explorer.
- **`search` returns only the attributes you queried** on its spans; the full
  set comes from the mirror or the full-trace endpoint.

Ingest is the sidecar fan-out only. Never point a write path at Tempo directly,
and never write into `$AVA_HOME/traces/` from a script — `ava trace ship` owns
the replay watermark and a hand-written line corrupts it.
