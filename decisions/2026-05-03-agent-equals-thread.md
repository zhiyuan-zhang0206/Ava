# Agent ≡ Thread (1:1)

## Context

The north star fixed the unit of work as "agent process = conversation thread."
The running shape lagged: a single default-thread, single-process loop with no
way to create peers, no lifecycle state, and a spawn call that fused "create the
agent" with "send it its first message." To reach an N-agent peer model the
lifecycle had to be made explicit — how an agent is born, how it dies, how it
comes back, and how a fresh process can prove it is the sole owner of its thread.

## Decision

One agent is permanently bound to one thread, identity shared at the database
level: `agents.id` is the same id as `threads.id`. An agent never runs a second
thread; "agent" and "thread" are the same concept.

Lifecycle is a three-state machine on the `agents` row —
`allocated → running → terminated` (and `terminated → allocated` on resurrect).
Spawn inserts the row as `allocated` and starts the process; the process claims
itself with a guarded transition `UPDATE … SET status='running' WHERE id=N AND
status='allocated'` — zero rows updated means another process already owns the
thread, and it raises. This is the race guard that enforces one-process-per-thread.

Lifecycle and message delivery are orthogonal. The spawn/resurrect calls manage
only the row and the process; they do not deliver a message. Delivery is a
separate inbound insert. The control plane is entirely the `agents` table plus
the `inbound_messages` table, and all callers (gateway, SDK, dev scripts) go
through one shared spawn function — agents are never started directly.

Supporting choices:
- Inbound `source` is one of three categories — `system`, `agent:N`, `ui:X` —
  which drive how the message is framed for the agent. An unrecognized source
  raises rather than falling back.
- Resurrect failures split into `AgentNotFound` (no such agent, like ENOENT) and
  `AgentAlive` (already allocated/running, like EBUSY) under one parent class.
- `send_message` checks the receiver's status first and reports it back to the
  sender, so the sender knows whether the message was queued to a running agent,
  a starting one, or a terminated one needing resurrect.

## Alternatives rejected

**Agent decoupled from thread (one agent runs many threads sequentially).**
Rejected. Merging the ids collapses an entire class of "which thread is this
agent on" bookkeeping and makes the process the thread. The flexibility of
agent-runs-thread-X-then-Y bought nothing the model needed.

**Spawn carries the first prompt (create + first-message fused).**
Rejected at the framework layer. Fusing them forces every caller to have a prompt
ready at creation and bakes in the assumption that an initial prompt must exist.
Splitting them lets a caller spawn-then-send when it wants work to start
immediately, or spawn-and-idle when the prompt is decided later. The SDK still
offers an optional `prompt` as an agent-facing convenience — each layer takes the
trade-off right for its own audience.

**Atomic cleanup when the process fails to start.**
Rejected. Adding compensating DB cleanup to the insert-then-spawn flow complicates
the code for a rare failure. A stranded `allocated` row is left as an ops signal
for monitoring / sweep to catch, not papered over inline.

**Bare status strings and a flat exception / generic delivery feedback.**
Rejected. Magic strings invite typos and drift, so status is an enum. A single
opaque resurrect error can't tell "wrong id" from "already alive," so the
hierarchy splits them. Delivery that ignores receiver state leaves the sender
blind, so feedback is status-branched.

## Consequences

- Identity is immutable: an agent is its thread for life. There is no rebinding,
  no agent-to-thread join to maintain.
- The race guard is the single point that guarantees one live process per thread;
  correctness of "no double-start" rests on that conditional update, not on
  external coordination.
- Two tables are the whole control plane. Lifecycle is observable and replayable
  from rows; spawn/resurrect/delivery are decoupled and independently testable.
- Failed starts leak `allocated` rows by design — this commits to an external
  monitoring/sweep path to reclaim them rather than transactional rollback.
- Callers must spawn before they can send; "create and prompt" is two steps at
  the framework boundary (and an optional one-liner only at the SDK).
- Source framing and dispatch fail fast on anything unrecognized, trading
  permissiveness for a stack trace that points straight at the bad caller.

Forward link (2026-08-22): the status model replaced the historical birth state
with unclaimed idling; see [agent status model](../docs/history/2026-08-22/agent-status-model.md).
