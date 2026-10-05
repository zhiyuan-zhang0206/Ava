# Heavy dependencies are an accepted trade, offset by layered memory reclaim

## Context

Ava's stack is deliberately heavyweight: every cluster (including each dev
worktree) runs its own Postgres 17 + Redis 8.2 instance, every agent carries a
LangGraph checkpoint, the frontend is a full Next.js build, and — macOS has no
fork-zygote — every spawned agent is its own OS process rather than a thread
or a forked child. At fleet scale (hundreds of agents) that per-process
resident cost threatened to dominate a box's RAM budget: an idle agent's heap
alone compresses to ~36MB resident, plus ~11MB for its per-agent MCP daemon,
plus 2 Postgres connections and a Redis subscription
(`decisions/2026-07-20-agent-hibernation.md`). At ~300 idle agents that
is enough to fill a 16GB box even before counting active ones. The stack was
not going to get lighter — Postgres/Redis/LangGraph are load-bearing, not
incidental — so the RAM problem had to be solved by *reclaiming* rather than
by shedding dependencies.

## Decision

Accept the heavy dependency stack, and invest in independent memory-reclaim
layers instead of trimming it. Verified against the code, the layers are:

1. **Hibernation** (flagship) — an agent idle past
   `AVA_HIBERNATE_IDLE_THRESHOLD_SECONDS` (default 450s) with no pending
   inbound gets its process killed outright, not suspended; a later inbound
   relaunches it (p50 919ms / p90 1.4s). This is the layer that turns the
   per-agent resident cost above into zero between wakes.
   `decisions/2026-07-20-agent-hibernation.md`.
2. **A heartbeat that feeds hibernation instead of racing it** —
   `AVA_HEARTBEAT_IDLE_THRESHOLD_SECONDS` (default 300s) sits below
   hibernation's 450s by design, so a normally-idling agent is nudged awake
   before it is ever swap-out eligible; hibernation's dominant reclaim case is
   instead agents that paused their own heartbeat, which sit at 0.5-12.5%
   duty cycle depending on pause length — near-total reclaim for the whole
   pause. `decisions/2026-06-22-heartbeat-opt-out-over-escalation.md`.
3. **A warm-pool floor so reclaim doesn't fight reuse** —
   `AVA_HIBERNATE_MIN_ACTIVE` (default 100) exempts a host's N
   most-recently-active agents from swap-out regardless of idle time, a
   per-host (not cluster-wide) knob sized to that box's own RAM.
   `decisions/2026-07-21-hibernate-warm-pool-floor.md`.
4. **A per-cluster data plane sized to be noise** — physically isolating each
   cluster's Postgres + Redis (`future/infra/embedded-per-cluster-data-plane.md`)
   costs ~100-150MB RAM per cluster (`shared_buffers` tuned down + Redis
   ~5MB) — roughly one agent's own resident cost, not a multiplier that
   compounds with fleet size.
5. **Shared, not per-agent, browser infrastructure** — one headed Chrome + one
   `chrome-devtools-mcp` upstream multiplexed to every agent over a Unix
   socket, instead of a browser (and its CDP collector buffers) per
   browser-using agent (`services/browser/mcp_daemon.py`).
6. **Lazy MCP server connections** — the per-agent MCP daemon process itself
   boots at agent start (overlapped with the rest of boot, not serialized
   after it), but does not eagerly connect to every configured MCP server;
   each server connection opens only on that tool's first call, with a 24h
   on-disk tool-schema cache avoiding repeat discovery round trips.
   `okf/mcps.ava.okf.md`.
7. **A fixed, small per-agent connection budget** — 2 pooled Postgres
   connections (shared with the LangGraph checkpoint saver) + one Redis
   subscription, with pgbouncer transaction pooling in front of the cluster's
   Postgres so connection count doesn't scale 1:1 with fleet size even before
   hibernation reclaims the agent entirely. `agent/db.ava.okf.md`.

The full list with the exact config defaults each cites is maintained in
[`conventions/runbook.md`](../conventions/runbook.md) (`Deployment
footprint & memory`) — this entry records the *decision* to pursue reclaim
over dependency-shedding; the runbook entry is the operational reference and
is expected to grow as new layers ship.

## Alternatives rejected

- **Trim the stack for something lighter** (sqlite instead of Postgres,
  in-process pub/sub instead of Redis, a lighter frontend). Rejected: the wake
  bus, durable checkpoint, and real-time event stream all need a real
  database and a real pub/sub; and per-cluster data-plane RAM is already
  ~100-150MB — noise next to N agents' own resident cost, not the thing
  worth optimizing.
- **A fork-from-warm / zygote agent model** to cut the ~36MB per-agent
  baseline directly instead of reclaiming it after the fact. Already
  rejected and recorded in `conventions/non-goals.md` ("Agent cold-start
  parallelization beyond the MCP layer") — spawn is fire-and-forget and never
  sits on a caller's latency path, so the cost/benefit doesn't support the
  added fork-state-sharing complexity (macOS has no COW zygote for a running
  interpreter). Hibernation attacks the same number from the other side:
  reclaim at rest, not cheaper birth.

## Consequences

- Memory is not, today, the wall for a well-run cluster. The hibernation
  entry's own consequences section already redirects attention to the *next*
  walls once memory is handled: the heartbeat's own wake-rate ceiling
  (~1.67/s, ≈750 agents on today's numbers) and LLM turn cost, which is
  linear in fleet size regardless of any reclaim layer above.
- This entry does not promise those next walls move — only that the heavy
  dependency stack has a real, verified, cited set of counter-measures rather
  than an unaddressed cost.
