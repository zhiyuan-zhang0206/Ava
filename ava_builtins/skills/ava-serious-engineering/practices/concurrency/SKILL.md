---
name: concurrency
description: "Use when a system has multiple execution flows — threads, async tasks, processes, agents — sharing state or resources. Covers shared-state discipline, async boundaries, connection lifecycles, and backpressure."
---

# Concurrency

## One-Sentence Core
> Shared mutable state is the root of concurrency bugs: isolate state per execution flow, communicate by message or immutable value, and make failure modes (dead connections, full queues) explicit rather than silent.

## Core Principles

- **Shared state is incorrect state**:Any mutable state read/written by multiple execution flows is a bug magnet — ordering becomes nondeterministic, same input produces different outputs, bugs become unreproducible. — **Why**:Thomas & Hunt's Tip 57; every "fails 1 time in 10" bug in production is almost certainly a race (Tip 58). — **How**:Prefer immutable data and message passing; when shared mutable state is unavoidable, confine it to one owner with explicit synchronization and document the invariant.

- **The actor discipline**:Each execution flow owns its state; communication happens only by message. — **Why**:Tip 59: actors (Erlang/Elixir) isolate state so no two flows ever mutate the same data concurrently. — **How**:Model concurrent components as owners of their state with message interfaces; never let one flow reach into another's internals.

- **Async boundaries are explicit**:Heavy synchronous work on an event loop blocks all other work on that loop. — **Why**:Real incident: a synchronous embedding call froze the gateway event loop for 5-18s, queuing every HTTP request (loading-conversation #671). — **How**:Long operations run off-loop (thread pool, worker process, async-native client); review flags sync IO in async handlers.

- **Connection lifecycles are failure states**:A connection can die without its wrapper knowing. — **Why**:Real incident: redis-py `is_connected` didn't observe `connection_lost`; a write to the dead transport crashed the agent process (#2613). — **How**:Treat every long-lived connection as possibly dead: check liveness at use time, reconnect with backoff, and make write failures raise instead of silently no-op.

- **Backpressure is a contract, not a tuning knob**:When producers outpace consumers, someone must drop, block, or buffer — silently is the worst choice. — **Why**:Real incident: SSE queue full with silent drops degraded remote-agent delivery (#1360); the fix was explicit batch pipeline + drop-oldest + reconnect. — **How**:Every bounded queue documents its overflow policy (block/drop-oldest/fail), and drops are observable via metrics.

## Checklist
- [ ] **MUST** Is there any mutable state shared between execution flows without a single owner and explicit synchronization?
- [ ] **SHOULD** Can every shared-state invariant be checked at a glance, or is it spread across flows?
- [ ] **MUST** Is there synchronous blocking work (IO, compute, embeddings) on an event loop or request thread?
- [ ] **MUST** For every long-lived connection (DB, Redis, message bus): what happens when it dies mid-operation?
- [ ] **MUST** Does every bounded queue/stream document its overflow behavior, and are drops visible in metrics?
- [ ] **MUST** Are random/intermittent failures ("works 9 times out of 10") treated as concurrency bugs until proven otherwise?
- [ ] **SHOULD** Do concurrent tests exist that would catch a race (parallel load, repeated runs, stress)?
- [ ] **MUST** Is each execution flow's state reachable by exactly one owner at a time?

## Anti-Patterns
- **The global mutable singleton**:"it's one process anyway" — convenient short-term, race magnet long-term → alternative: ownership + message passing
- **The sync call in the async handler**:works in dev, freezes under load → alternative: off-loop execution
- **The optimistic connection**:assuming a handle is alive because it was created → alternative: liveness check + reconnect at use
- **The silent overflow**:queue full → drop without metric → alternative: explicit overflow policy + observable drops
- **The flaky-untested race**:"it only fails 1 in 10 times" shipped without a stress test → alternative: parallel/stress test in CI

## Examples(bad → good)
- **Bad**: a module-level `state = {}` written by multiple async tasks, mutated inline.
  **Good**: an actor-style owner with a message interface (`queue.put(("update", k, v))`), or immutable state snapshots passed between flows.
- **Bad**: `client = redis.Redis(...)` cached forever; a write after a network blip crashes on a `None` transport.
  **Good**: writes go through a wrapper that reconnects on failure with backoff and raises a typed error if the connection is gone.

## Relationships
- `principles/error-handling` — "transient errors at the low layer" is the error-handling half of connection liveness
- `practices/performance` — **Division of labor**: concurrency = execution flows and shared state (correctness, lifecycles, backpressure); performance = capacity and measurement (latency, throughput, resource budgets). The two skills share the same incidents (event-loop blocking, full queues, dead connections), but concurrency asks "is it correct under concurrency" and performance asks "is it fast / cheap enough". Backpressure policy (block/drop/buffer) is the seam: performance sets the capacity, concurrency sets the semantics
- `principles/dependency-management` — shared state is a form of hidden coupling between flows
- `ai-era/context-explicitness` — multi-agent systems are concurrent systems; verifiable handoff contracts are the message-passing discipline of agents

## Sources
- references/03-pragmatic-programmer.md §7(Concurrency, Tips 56-60)
- Ava incident records: loading-conversation-rootcause (event-loop blocking), redis-dead-transport (connection lifecycle), sse-queue-full (backpressure)
