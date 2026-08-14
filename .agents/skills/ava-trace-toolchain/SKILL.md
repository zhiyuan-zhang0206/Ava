---
name: ava-trace-toolchain
description: Fetch, read, and render an OTel trace end to end — find a trace in Tempo, pull its full span set from the local mirror, analyze it, and build a self-contained HTML report. Use when asked to look at a trace, investigate an agent run, or build a trace report.
---

# OTel trace toolchain — fetch, read, render

Three steps turn a trace question ("what happened in agent 3048's turn at
14:00?") into an HTML report the user can open. Each step is a script under
`scripts/`, chained by JSON files; each runs standalone, so a step can be
skipped or re-run without redoing the others.

```
fetch_trace.py ──> trace_raw.json ──> read_trace.py ──> trace_read.json ──> render_trace.py ──> report/index.html ──> ava.ui.serve
```

The scripts live in this skill's own `scripts/` directory — the loader prints
its path (`ava.help(ava.skills.ava_trace_toolchain)`); in a repo worktree it is
`.agents/skills/ava-trace-toolchain/scripts/`. The commands below abbreviate
it as `<scripts>/`.

**Why this toolchain exists.** Traces are recorded by every agent into a local
OTLP-JSON mirror (`$AVA_HOME/traces/spans.jsonl` + rotated `spans-<ISO>.jsonl`, metadata-only — LLM content
is stripped at record time) and replayed to Tempo by `ava trace ship`. The
mirror is the durable, complete, offline source; Tempo is the indexed search
surface; Grafana is the interactive browser. The scripts encode the verified
commands and the conversions between the two (base64 vs hex ids, OTLP
envelopes, the 5 MB full-trace cap) so an agent never re-derives them as
throwaway scripts.

For writing a *persisted* trace doc (traces/ genre: scenario, step
sequence, evidence), see the `write-a-trace` skill — this toolchain produces
the evidence a trace doc cites.

## Step 1 — Fetch: find the trace, get its spans

Find (Tempo TraceQL, indexed, works cross-agent):

```bash
.venv/bin/python <scripts>/fetch_trace.py --search '{ resource.service.name = "ava" && span.traceloop.association.properties.agent_id = "3048" }'
```

Pull a full trace by id:

```bash
# primary: the local mirror — complete, no size cap, no network
.venv/bin/python <scripts>/fetch_trace.py --trace-id <hex-32> --source mirror --out trace_raw.json
# secondary: Tempo full-trace API — fails above the 5 MB cap
.venv/bin/python <scripts>/fetch_trace.py --trace-id <hex-32> --source tempo --out trace_raw.json
```

Rules:

- **Mirror first.** `--source mirror` greps `spans*.jsonl` (active + rotated) for the trace id
  (base64 form, converted inside the script) and returns every span. It needs
  filesystem access to `$AVA_HOME` (default `~/.ava`); on another machine,
  query Tempo over the network instead.
- **Tempo full fetch fails at 5 MB** — Tempo rejects `/api/traces/{id}` with
  `trace exceeds max size` for big agent-session traces. There is no
  pagination that returns a complete trace (see Pitfalls). Do not fight it:
  use the mirror, or narrow to a span subset with `--search`.
- **Find before you fetch.** Search first to get trace ids, then fetch the one
  trace you want. Trace ids from Tempo are hex; ids in the mirror are base64
  — the scripts convert; do the conversion yourself with
  `base64.b64decode(s).hex()` when cross-referencing by hand.

TraceQL verified against Tempo 3.0.2:

| Want | Query |
|---|---|
| All traces | `{ resource.service.name = "ava" }` |
| One trace | `{ trace:id = "<hex-32>" }` |
| Spans of one agent | `{ span.traceloop.association.properties.agent_id = "3048" }` |
| Spans in one node | `{ span.traceloop.association.properties.langgraph_node = "after_exec" }` |
| Spans by operation | `{ span.gen_ai.operation.name = "goto" }` |
| Exact span name | `{ name = "goto exec" }` |

Search parameters: `limit` (traces), `spss` (spans per span set, max 100),
`start`/`end` (unix seconds). Dotted attribute names work in `span.*` scope.

## Step 2 — Read: turn spans into understanding

```bash
.venv/bin/python <scripts>/read_trace.py trace_raw.json --out trace_read.json \
  [--with-content] [--with-events] [--gateway http://localhost:8000]
```

Produces `trace_read.json` and a text summary: the span tree (parent/child,
ordered), per-span offsets and durations, the LLM span list (name ends
`.chat`), the langgraph node sequence, and the root span's `session.id`
(agent) / `ava.checkpoint_id`.

- `--with-content` calls
  `GET /api/agents/{agent}/traces/{trace_id}/messages` on the gateway — the
  turn's full message content, resolved on demand from checkpoints (spans
  carry metadata only). `pruned: true` means the checkpoint was trimmed; that
  is the expected shape for old turns, not an error. 404 = the agent no
  longer exists.
- `--with-events` calls `GET /api/events?trace_id=<hex>&from=<ISO>` for the
  correlated event stream (audit/telemetry/log rows sharing the trace id).
- **Gateway auth.** The gateway API requires `Authorization: Bearer
  <AVA_CLUSTER_SECRET>` when the cluster has a secret set. The script reads
  `AVA_CLUSTER_SECRET` from `$AVA_HOME/.env` (or the environment) and sends
  it; pass `--gateway` when the gateway is not on this machine.
- The gateway runs on port 8000, not 8100 (8100 is an unrelated service).

## Step 3 — Render: build the frontend

```bash
.venv/bin/python <scripts>/render_trace.py trace_read.json --out report/
```

Writes a **self-contained** `report/index.html` — inline CSS/JS, no CDN, no
build step (it must open from the tailnet link offline). Contents: trace
header, waterfall timeline (depth-indented span bars, hover details), node
sequence, LLM span table, events, and the turn content when present. Serve it
the one supported way:

```python
import ava
ava.ui.serve("report", name="trace-<agent>-<short-id>", title="Trace <hex>")
```

The user gets the rendered page link. Complement, do not replace: Grafana at
http://100.64.0.2:3003 (anonymous viewer, Tempo datasource) is the
interactive browser for zooming around; the HTML report is the sharable,
annotated artifact.

## Environment cheat sheet

| Surface | Where | Notes |
|---|---|---|
| Mirror | `$AVA_HOME/traces/spans*.jsonl` | active `spans.jsonl` + rotated `spans-<ISO>.jsonl`; one OTLP/JSON request per line; ids base64; rotation + retention-pruned |
| Tempo query API | `http://localhost:3200` | `/api/search`, `/api/traces/{id}`, `/ready`, `/api/echo` |
| Grafana proxy | `http://100.64.0.2:3003/api/datasources/proxy/uid/tempo/...` | anonymous; the path to Tempo from other machines |
| Grafana UI | `http://100.64.0.2:3003` | Explore > Traces, anonymous viewer |
| Ship bridge | `ava trace ship [--target tempo] [--dry-run]` | gap-replay only (sidecar fans out live); gateway schedule id=5 optional for scheduled replay |
| Turn content | `GET /api/agents/{id}/traces/{hex}/messages` | gateway :8000, bearer auth, pruned semantics |
| Event stream | `GET /api/events?trace_id=<hex>&from=<ISO>` | gateway :8000, bearer auth, `items[]` + `meta.total` |

The LGTM stack is `deploy/local/lgtm/` (`start.sh` / `stop.sh`); it is a
read-only viewer — never point write paths at it, and never touch
`~/.ava/traces` from a script.

## Pitfalls (all observed, Tempo 3.0.2)

- **5 MB full-trace cap** — `/api/traces/{id}` refuses traces above 5 MB.
- **`spss` caps at 100** and **time-window paging is unreliable** (a window
  can come back empty while the trace exists outside it) — so "fetch the
  whole trace from search" is not a thing; use the mirror.
- **Regex `=~` on the `name` intrinsic misbehaves**: anchored patterns
  (`"^goto"`) match nothing, and `name =~ ... && <anything>` returns empty.
  Prefer exact `=` for names; `=~` on span attributes works.
- **Ids are base64 in the mirror, hex in Tempo** — convert, never paste
  across.
- **`/api/v2/search/tag/*` only enumerates intrinsics** (name/kind/status...),
  not attributes — it is not a schema explorer.
- **`search` returns only queried attributes** on its spans — the full
  attribute set comes from the mirror or the full-trace endpoint.

## Where this skill does not apply

- Writing or auditing `traces/*.md` genre docs — use `write-a-trace`.
- Deploying/operating the LGTM stack itself — see the memory notes and
  `deploy/local/lgtm/README.md`.
- Computer-use traces (`/api/computer/traces`) — a different subsystem.
