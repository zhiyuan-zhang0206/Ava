# Agent-runner as server: agents as turn tasks, not processes

> **Status: ratified; Phase 1 partially built, behind `AVA_RUNNER_MODE`.**
> Phase 0 (per-turn graph invocation) is shipped. Of Phase 1: the per-turn
> settings + plugin-config views, turn-scoped identity and log attribution, the
> claim node's hosted idle branch, the wake dispatcher, and the `agent-host`
> daemon that runs turns are all built (`services/agent_host/`). What is NOT
> built is the lifecycle integration around them — spawn still forks a process
> per agent, restart/terminate still route through the process path, and
> hibernation, the per-agent lease renewer and the lease-zombie reaper are
> untouched. So `hosted` is a runnable daemon but not yet an end-to-end cluster
> mode; the flag defaults to `process` and the service is gated off the roster
> until a cluster opts in. Migration step 3's deletions remain future work.
>
> One Phase 1 property was NOT achieved and is worth knowing before a soak: the
> host builds ONE compiled graph per process, because `build_graph` mutates
> process-global plugin registration. `_build_llm_retry()` is evaluated at build
> time, so the per-model retry cap and the per-agent retry-wave de-phasing
> offset become cluster-level in hosted mode (issue #174).
>
> **Where the host lives, and the alternative rejected.** `services/agent_host/`
> imports the agent kernel, which the `services must not import the agent kernel`
> import-linter contract otherwise forbids; the exemption is enumerated per
> target module in `pyproject.toml`. The alternative considered was relocating
> the host under `agent/` so no exemption were needed. It was rejected because it
> trades one boundary problem for a worse one: `agent/` would then contain a
> supervised service daemon — pidfile, healthz, watchdog respawn — and start
> knowing about process supervision, which is the direction the layering exists
> to prevent. Every other service reaches agents by SPAWNING them; the hosted
> runner is the one whose job description is to BE the kernel, so the exemption
> names a real distinction rather than papering over a violation.

## The invariant

> **An agent is a row plus a checkpointer thread. A *running turn* is the only
> thing that consumes runtime resources — and a turn is an asyncio task inside
> the agent-runner, not a dedicated OS process.**

Today the unit of agent existence is a **process**: one OS process per agent,
alive from spawn to terminate, resident while idle. This doc proposes making the
unit of existence the **turn task**: the agent-runner daemon hosts every local
agent's turns as asyncio tasks; an idle agent is *no task at all* — its identity
lives entirely in `agents_meta` + its checkpointed LangGraph thread, exactly
where it already lives today (a process restart already rebuilds everything from
there; the process was never the real home of agent state).

This is the philosophy.md "strong invariant over managed ambiguity" thread
applied to the process model: hibernation, lease renewal, zombie reaping,
idle-RSS budgeting, and per-process wake plumbing are all machinery managing the
ambiguity *"this agent exists but is doing nothing — how much should that
cost?"*. Making idle = no task makes the question structurally unaskable: idle
costs nothing because there is nothing.

## Why

Measured on prod (2026-08, single box, 41 agents):

- **41 agent processes, ~2.07 GB RSS total, ~52 MB average** — almost all of it
  identical interpreter + import state (LangGraph, psycopg, the SDK), duplicated
  per process. The marginal agent costs ~50 MB *while doing nothing*.
- **Hibernation exists only to fight this** — `ops/controllers/hibernate.py`
  swaps idle processes out via SIGUSR1 (`agent/lifecycle.py`), guarded by
  `hibernate_min_active` (`shared/config/daemon.py`), with a dedicated
  `hibernating` status, wake-on-inbound respawn, and reaper exemptions. All of
  it is machinery whose only purpose is making idle processes cheaper.
- **Liveness bookkeeping is per-process** — every agent runs
  `agent/loop.py:_renew_agent_lease_loop` (a 60 s DB write, forever, idle
  included), and `ops/controllers/respawn.py` reaps lease zombies. 41 heartbeat
  loops to prove 41 mostly-idle processes are alive.
- **The eternal process broke tracing** — one `graph.ainvoke` runs for the
  process's whole life (`agent/loop.py`), so the root span
  (`shared/trace.py:session_span`) never ended and never exported. Phase 0
  fixes this by making the *turn* the invocation unit; this doc is the same
  boundary applied to scheduling, not just tracing.
- **Windows pays a process-model tax** — no `setsid`, no double fork, no init
  to inherit orphans (`shared/winproc.py`); the zero-visible-window and
  kill-tree machinery exists because agents are processes that must be spawned
  and killed from services.
- **Supervision multiplies** — watchdog, restarter, and healthchecks each
  reason about N agent processes; the respawn/zombie logic is some of the
  subtlest code in `ops/`.

## What it is NOT built on: Gunicorn / Celery — decided against

The obvious question — "isn't this just a worker server? use Gunicorn" — has a
shape mismatch:

- **Gunicorn** is an HTTP-shaped pre-fork server: workers block on a shared
  accept socket, requests are stateless, any worker can take any request, and a
  request outliving a timeout is a bug. Our unit is a **message-woken, stateful
  turn**: dispatch is "agent N has pending inbound" (not socket accept), the
  same agent's turns must serialize while different agents run concurrently,
  and a turn legitimately runs for days in autonomous loops. We would be
  fighting its dispatch model, not reusing it.
- **Celery / dramatiq / arq** are semantically closer (task queues) but bring a
  broker contract plus ack/retry/result-backend semantics that we would have to
  systematically disable — at-least-once redelivery of a *turn* is exactly the
  duplicate-execution ambiguity our claim CAS
  (`agent/graph/_claim_batch.py`) exists to kill. A heavy dependency whose core
  features are all liabilities here fails the "no new dependencies unless
  necessary" bar.
- **What Gunicorn's master actually provides — worker supervision — Ava already
  has**: the watchdog, restarter, ops server, and healthchecks
  (`services/`) already supervise long-running daemons, and the agent-runner is
  already one of them. The missing piece is only the *thin* layer: a dispatcher
  that turns an inbound wake into a scheduled turn task. That layer is small,
  bespoke-shaped, and ours to write.

## Phases

### Phase 0 — per-turn graph invocation (landing separately)

The claim node gotos END at a turn boundary; the runloop
(`agent/_runloop.py`) re-invokes `graph.ainvoke` on the same checkpointer
thread unless exit is requested. The process model is untouched — this only
makes "turn" a real code boundary (and fixes the root-span export). Everything
below assumes it.

### Phase 1 — single-worker hosting (the actual migration)

The agent-runner daemon hosts **all local agents' turns as asyncio tasks in its
own process**. No new worker-management code: the runner is already a
supervised, rolling-restarted daemon; it stops spawning agent processes and
starts scheduling turn tasks. One process replaces 41.

An idle agent is no task. A wake (pending inbound) causes the dispatcher to
create a turn task for that agent; the task runs the turn(s) until the pending
queue drains, then ends.

### Phase 2 — K-worker pool (threshold-triggered, maybe never)

If Phase 1 demonstrates real CPU contention (turns are I/O-dominated today —
LLM calls and exec awaits — so the GIL is unlikely to bite soon) or
unacceptable blast radius (one agent's pathological turn harming neighbors),
split into K worker processes with **per-agent affinity** (an agent's turns
always land on the same worker, preserving serialization) behind the same
dispatcher. This restores process-level isolation in units of N/K agents.
Phase 2 is *designed for* but **not built** until a measured threshold is
crossed — building it speculatively would recreate the supervision complexity
Phase 1 deletes.

## The two big work items

### (a) Delivery: pull → push inversion

Today the push already exists at the transport level — Redis pub/sub
(`_wait_for_batch` → `wait_for_inbound`) — but it terminates in a **per-process
idle wait**: every idle agent process holds its own subscription, its own
`idling` row flip, its own lease renewals, and the delivery watchdog
(`services/delivery_watchdog/`) re-publishes wakes for idling owners whose
publish got lost. The inversion: the **dispatcher** (in the runner) holds one
subscription over all local agents; a wake for agent N materializes a turn task
for N (if none is running). The claim node's *dispatch-by-kind* pipeline
survives intact — what disappears is the idle half (`_wait_for_batch`, the
`idling`-status wait loop): a turn task is only created when there is something
to claim, so claim always finds work or ends the turn.

### (b) Settings agent-scoping

Today per-agent config works because each agent is a process:
`shared/config`'s settings singleton binds once at process boot, and
`ava.self.restart(config_overlay)` (`ava/self.py:restart`) applies an overlay
by writing `agents_meta.config_overlay` and exiting — the respawned process
boots with the merged view. In one shared process, a singleton is wrong for
every `per_agent=True` field.

The replacement is a **per-turn settings view bound to a contextvar before the
turn task is created**. Empirically verified: LangGraph runs each node in its
own asyncio task that copies the *loop-level* context — a contextvar bound
*inside* a node does not reach the next node, but one bound **before the turn
task spawns** propagates into every node task. So the dispatcher resolves, at
turn-task creation:

```
config_overlay (agents_meta)  >  birth_config (frozen fields)  >  cluster default (live fields)
```

and binds the resolved view. The registry metadata this needs **already
exists**: every field declares `per_agent` and `lifecycle: frozen | live`
(`shared/config/__init__.py`); the resolution rule above is exactly the
documented lifecycle semantics, moved from process boot to turn start. The
enforcement moves with it: a lint (extending the existing registry-driven
lints) forbids reading `per_agent` fields through the process-global singleton
from turn-scoped code — they must come through the turn view.

## Restart-semantics reconciliation

Every current use of process restart/terminate, mapped:

| Today (process op) | Server model |
|---|---|
| `ava.self.restart(config_overlay)` — DB write + process exit + respawn with merged config | DB write (unchanged, incl. `validate_config_overlay`) + **end current turn; next turn's view rebinds from `agents_meta`**. Still agent-scoped by construction — the view is per-turn, so exactly one agent is affected, same as today. |
| `ava.self.restart()` — plain restart (fresh context/process for a wedged agent) | Cancel the agent's turn task + drop its cached per-agent state (turn view, any memoized handles); next wake starts clean. The checkpointer thread — the real state — is untouched, exactly as today. |
| Code upgrade (`ava cluster update` restarting every agent) | **Runner rolling restart** — one process restart applies new code to all agents at once. Strictly better: today's update must restart N processes and wait for N turn boundaries; the runner drains/checkpoints and restarts once. |
| `ava.self.terminate()` | Mark `terminated`, end the turn task, never schedule again. The exit-notify HTTP hop (`POST /api/agents/{id}/exited`) becomes an in-process transition. |
| Wedged-state recovery (reaper force-kill of lease zombies) | `asyncio.Task.cancel()` on the turn task — **strictly weaker than the SIGKILL it replaces, and this is a real capability regression of hosted mode, not a footnote.** A cancel lands only at the task's next await point. That covers the overwhelming majority of a wedge (waiting on the model, on I/O, on a lock) and does **not** cover a task blocked inside a C call, which asyncio cannot interrupt at all; process mode's SIGKILL always works. The host therefore waits a bounded `_CANCEL_UNWIND_TIMEOUT_S` and then REPORTS the straggler (`host_turn_uncancellable`, carrying the agent id and how long the cancel was pending) rather than hanging — so a wedged turn is visible in the event river instead of only inferable from an agent that stopped answering. The remaining escape hatch is restarting the host, which takes down **every hosted agent on that runner**, not just the stuck one. Bounding that blast radius is the first concrete argument for Phase 2 that is not CPU contention (issue #184). The zombie *class* does shrink either way: no process can silently die while `idling`, because idle has no process. |
| Hibernation (`ops/controllers/hibernate.py`, SIGUSR1, `hibernating` status, `hibernate_min_active`, wake-respawn) | **Deleted.** Idle = no task = the end state hibernation was approximating. The SIGUSR1 path in `agent/lifecycle.py` retires with it. |
| Lease renewal (`_renew_agent_lease_loop`, per-agent 60 s writes) + lease-zombie reaping | Per-agent leases collapse into **worker bookkeeping**: the runner heartbeats once (it already does, for the watchdog); "is this agent's turn alive" becomes an in-process fact about a task. What must survive: the *turn-claimed-but-owner-died* case — covered by the runner's own lease over its claimed turns, released on crash-restart recovery. |

## Blast radius / fault model

What Phase 1 **loses** relative to process-per-agent, stated honestly:

- **Per-agent OOM/crash isolation.** Today a segfaulting C extension or a
  runaway allocation kills one agent. In Phase 1 it kills the runner — every
  in-flight turn aborts (checkpointed; they resume on restart, the same
  recovery path a box reboot exercises today). Mitigations: the accumulation
  caps being added independently (exec output cap, issue #45) shrink the
  biggest allocation vector; Phase 2 restores isolation in units of N/K if
  this bites in practice.
- **A CPU-hogging turn steals the loop.** Today the OS scheduler arbitrates;
  in one asyncio process a turn that blocks the loop (sync CPU inside a node)
  starves neighbors. Exec already runs out-of-process; remaining sync hotspots
  must move behind `asyncio.to_thread` or the Phase 2 trigger fires.
- **Days-long turns are fine.** A turn task holding for days (autonomous
  loops; `recursion_limit` stays) costs one task + its awaits — it does not
  hold a worker slot the way a Gunicorn request would. This is precisely why
  the request-pool shape was rejected.

## Explicitly unaffected

- **Pty shell sessions** — already double-forked, detached, init-inherited
  (`shared/posixproc.py`); they were never children of the agent process in
  any meaningful sense and do not move.
- **Schedule sessions** — consumers of the gateway API; unchanged.
- **The gateway API surface** — `POST /api/agents` and the whole
  `gateway/routers/agents.py` contract are unchanged; "spawn" creates the row
  and (locally or via runner forward) registers the agent with the dispatcher
  instead of forking a process.
- **The data plane and the LGTM stack** — untouched.

## Migration path

1. **Phase 0 lands** (separate PR, already implemented).
2. **Phase 1 behind a runner-level mode flag**: the runner gains the
   dispatcher + turn-task scheduler; a cluster runs either all-process or
   all-hosted (no mixed mode — mixed would need double bookkeeping for the
   lease story). Dev worktree clusters soak first, prod flips last.
3. **Delete after soak**: hibernation controller + status, SIGUSR1 path,
   per-agent lease loop, idle-wait machinery, the per-agent respawn/zombie
   logic — each deletion is its own reviewable PR.
4. **Rollback**: flip the mode flag back. Nothing in the DB schema changes
   shape (agents_meta keeps status/lease columns until step 3's deletions,
   which happen only after prod soak); a rollback is a runner restart in the
   old mode.

Test strategy per phase: Phase 0 carries its own suite (turn boundary,
exit/compact edges, hibernate signal untouched). Phase 1's lock-in tests:
same-agent turn serialization; cross-agent concurrency; overlay rebind at turn
boundary (restart-with-overlay e2e, asserting *only* the target agent's view
changed); wedged-turn cancel; runner crash mid-turn → checkpoint resume;
delivery wake → task creation latency (replacing today's wake→claim probes);
the full existing e2e set (message_flow, self_restart, self_terminate) which
must pass unchanged in hosted mode.

## Observability tie-in

Turn spans (Phase 0) become naturally task-scoped: one turn = one task = one
exported trace, and the dispatcher stamps queue-wait vs run-time. The ratified
doctrine is unchanged by this doc: the **unified event river is the primary
observation surface for long-running agents; traces are drill-down for bounded
units** — the turn is exactly the bounded unit that makes the drill-down
truthful. Per-agent RSS dashboards give way to per-runner RSS + per-turn
duration/queue-depth, which is the fleet-level view we actually wanted.
