# Idle heartbeat as an agent opt-out, not an escalation chain

## Context

Idle fleet agents can sit parked indefinitely: an agent that finished its task
but forgot to terminate, or one waiting on an event with no watcher, is
indistinguishable from a healthy idle agent. An earlier proposal
(`future/infra/heartbeat-design.md`) answered this with a two-tier system:
a liveness watchdog plus a self-check heartbeat with exponential backoff
(5m → 15m → 1h → 6h → 24h), a per-agent miss counter, a `HEARTBEAT_OK` ack
token, a new `heartbeat` inbound kind, and a graduated escalation chain
(warn → auto-resurrect → notify fleet lead → page a human) guarded against
zombie resurrect loops.

A prior shipped attempt at the same problem (PR #1420, the `_classify_forward_promise`
anti-stall classifier in `agent/graph/_llm.py`) ran a cheap LM on every text-only
idle turn to guess whether the agent had stalled mid-plan, and injected a nudge if
so. It was passive (the agent had no say) and paid an LM call per idle turn.

## Decision

Ship the smallest thing that makes "don't disturb me" an **active agent choice**:

- One gateway daemon (`services/heartbeat/`) polls idle agents on a **fixed**
  interval (default 15 min) and sends a plain `chat` inbound nudge to any that
  have been idle past a threshold (default 5 min).
- The agent opts out with `ava.self.pause_heartbeat(duration)`, which stamps
  `agents_meta.heartbeat_paused_until`; the daemon's SELECT skips paused agents.
- The nudge text itself tells the agent its three honest options: still working
  (do nothing), waiting (pause), or done (terminate). No ack token, no miss
  counter, no escalation.
- The anti-stall classifier (PR #1420) is removed.

## Alternatives rejected

- **The two-tier escalation proposal.** Exponential backoff, miss counters, and
  a warn→resurrect→notify→page chain are a lot of stateful machinery to maintain
  a property the simpler design gets for free. Liveness (did the process die?)
  is already answered exactly by the restarter reaper's `os.kill(pid, 0)` probe
  (`_reap_local_dead_running_idling`); a `last_active_at` timeout cannot tell a
  healthy parked agent from a dead one, so folding liveness into the heartbeat
  would re-introduce the ambiguity the pid probe already resolved. We kept the
  two concerns separate: the reaper owns liveness, the heartbeat owns idle nudging.

- **A new `heartbeat` inbound kind.** A nudge the agent should simply read and
  react to is an ordinary message. `kind='chat'` reuses the whole claim →
  HumanMessage path with zero schema surface; a new kind would need migration +
  claim dispatch handling for no behavioral gain.

- **The anti-stall LM classifier (PR #1420).** It guessed intent passively and
  burned an LM call per idle turn. Turning the choice over to the agent
  (`pause_heartbeat`) is the laziness-against-laziness inversion: a lazy agent
  that does not want to be pinged has a one-line way to stop it, and the cost is
  a DB write, not an inference.

- **A background asyncio task inside the gateway process** (instead of a separate
  daemon). Rejected for consistency with every other cluster daemon
  (restarter / labeler / memory-indexer all run as decoupled services with
  their own `/healthz` + watchdog revival); a separate process is independently
  restartable and observable.

## Consequences

- An idle agent that neither pauses nor terminates gets a nudge every interval —
  intentional: indefinite idling is the exact thing being discouraged. A
  well-behaved agent terminates when done and pauses when waiting.
- The heartbeat is a safety net, not liveness or escalation: a truly dead agent
  is handled by the restarter reaper, and there is no auto-resurrect / paging
  path here. If escalation is ever wanted, it is additive future work.
- `agents_meta` gains one nullable column (`heartbeat_paused_until`, migration
  0056); the daemon adds ~2 connections to the budget on the gateway only.
- Schema-versioned behavior change is cluster-wide: the gateway nudges agents on
  any machine because the inbound-insert trigger wakes them regardless of host.
