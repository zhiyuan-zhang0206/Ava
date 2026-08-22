# Agent hibernation — an ops-only memory swap-out state

## Context

The motivation, in priority order:

1. **A semantic gap in the state machine.** Between `idling` (alive, waiting) and
   `terminated` (dead, needs resurrect) there was no state for "alive in principle
   but not worth a resident process right now". Agents routinely
   `ava.self.pause_heartbeat` for 30 minutes to a day; during that window an agent
   is guaranteed to do nothing, yet it holds a full process. The alternative — a
   "pause forever" knob — is an abuse surface (an agent can silence itself
   indefinitely); hibernation gives the reclaim without the knob, decided purely
   by ops from `last_active_at`.

2. **The memory account at fleet scale.** An idle agent's heap compresses to
   ~36MB resident (macOS has no fork-zygote, so it is per-process and
   unshareable), plus ~11MB for its per-agent MCP daemon, plus 2 PG connections
   and a Redis pub/sub. At ~300 idle agents that is enough resident memory to fill
   a 16GB box, and the heartbeat's periodic wake (every ~7.5 min for a non-paused
   agent) turns each idle agent into recurring OS-swap fault-back churn.
   Hibernation makes a paused/long-idle agent's footprint go genuinely to zero
   between wakes. (pgbouncer client-connection slots are a *footnote*, not a
   driver — `max_client_conn` is just a knob; pgbouncer multiplexes many clients
   onto few backends by design.)

3. **Determinism — no false-positive on active agents.** The swap-out predicate is
   `idling` past a threshold with no pending inbound, so an agent mid-exchange
   (sub-threshold turn gaps) is never touched.

Cold start is cheap enough to make the trade: p50 919ms / p90 1.4s (not the 5–15s
originally assumed), with no wake-throughput degradation to >20 concurrent boots.
So the process can be killed while an agent sits idle and relaunched on demand for
~1s of latency.

**The dominant reclaim case is paused agents, which reframed the benefit model.**
The initial model treated the heartbeat as a fixed metronome and computed a
duty cycle over a ~450s average wake period. But agents pause their heartbeat for
long stretches, during which they take *zero* wakes — so hibernation is full
dead-weight reclaim there (duty cycle ≈ 12.5% for a 1h pause, ≈ 0.5% for a day),
and the metronome model was an underestimate. This set the default `H` (below).

## Decision

Introduce a new agents_meta status, **`hibernating`** (a continuous-form name in
the family of `running`/`idling`, not the terminal `-ed` of
`terminated` — hibernation is a persisting live state), that exists **only in the
ops layer**. When an agent has been `idling` longer than `H`
(`AVA_HIBERNATE_IDLE_THRESHOLD_SECONDS`, default **450s**) with no pending inbound, a
per-machine controller in the restarter daemon (`HibernateController`) sends it
SIGUSR1; the agent converts that to a clean process exit that parks its row
`hibernating` (pages kept, no exit event). The same controller polls for a
`hibernating` row that has a pending inbound (a heartbeat nudge, a chat, a task)
and relaunches it — a clean restart with **no lifecycle marker**.

`H=450s` is deliberately **above** `heartbeat_idle_threshold_seconds` (300s): a
non-paused idle agent is nudged awake by the heartbeat before it ever crosses `H`,
so hibernation reclaims mainly agents whose heartbeat is suppressed (a pause), for
which the process is dead weight the entire pause. Observed pauses are rarely
shorter than 5–7.5 min, so this value produces almost no swap-out-then-immediately-back
churn — each hibernation is long-lived. Tuning `H` below the heartbeat threshold
would additionally reclaim non-paused idle agents between heartbeats, at the cost
of a cold start on each heartbeat wake; that is left as a future dial.

Two hard invariants:

1. **Invisible to agents and the frontend.** Both project `hibernating` → `idling`
   (the SDK at the `ava/_gateway_client` boundary, the frontend at its cache
   ingest). There is no SDK API to request/observe hibernation; the wake inserts
   no note/resurrect message. A peer sees a swapped-out agent exactly as it sees
   an idle one — parked, wakes on a message. Like a K8s caller who sees capacity
   and load, never whether a node cold-started. The gateway `GET /api/agents`
   endpoint stays truthful (returns the raw value) so ops tooling can still see
   it; only the two consumers project.

2. **The heartbeat still wakes hibernating agents.** It is the liveness signal;
   swapping a process out must not silence it. `hibernating` joins `idling` in the
   heartbeat's selection, and its due-time is unchanged because swap-out does not
   touch `last_active_at`.

## Alternatives rejected

- **Reuse `terminate(reason='hibernate')` instead of a new status.** Rejected on
  a functional defect, not a preference: entering `terminated` fires the
  `cascade_close_agent_pages` trigger, so every ~7-minute swap-out would destroy
  the agent's UI pages. Working around it needs a discriminator column — which is
  a second status implemented as a column, buying none of the change back while
  deepening the very `terminate` overload this set out to fix. The heartbeat
  WHERE would also have to grow an `OR (terminated AND reason=hibernate)` clause.
  A new enum value is ~6 change points (CHECK+migration, enum, heartbeat WHERE,
  spawn cap, SDK/frontend projection); the reapers need none (see below).

- **Route the exit via the SystemExit message ("signal:SIGUSR1").** Rejected
  after the e2e proved it fails: asyncio converts the signal handler's SystemExit
  into a task `CancelledError` before `main()`'s coroutine finally runs, so
  `sys.exc_info()` there shows CancelledError, not the signal. (The existing
  SIGTERM path never noticed because it routes to `terminated` either way.) The
  fix is a module flag set synchronously in the handler, read first by
  `_exit_reason`; the message check remains only as a fallback for a direct,
  non-asyncio SystemExit.

- **Immediate swap-in at each wake source (extend `resurrect_if_terminated`, a
  gateway-side inbound interceptor).** Rejected in favour of a **poll** in the
  same controller: the wake sources are not one choke point (the heartbeat daemon
  inserts inbound with raw SQL, not through a gateway handler), so an interceptor
  would still need per-source wiring and could miss a future source. A poll over
  "`hibernating` on this machine with a pending inbound" catches every source
  automatically, is robust against a lost pg_notify, and naturally launches on
  the agent's home machine (respecting the boot placement gate — agent 1513). The
  cost is up to one restarter poll (~1s) of extra latency before relaunch, which
  is immaterial for an agent that is by definition idle past `H`.

- **A per-agent hibernation allow-list.** Rejected as redundant. `H` already is
  the policy: an agent in an active exchange has sub-`H` turn gaps and stays
  resident; only a genuinely long-idle agent is swapped out. An allow-list adds a
  state to maintain and buys nothing `H` does not.

## Consequences

- **The three respawn reapers are deliberately unchanged.** `hibernating` falls
  outside all of their scan sets (unclaimed `idling` / boot-phase `running|idling` /
  post-message `running|idling`),
  so a swapped-out agent — which carries the now-dead pid of its old process — is
  never reaped to `terminated`. This is load-bearing (a reaper touching it would
  tear the mechanism down within 30s) and is locked by a test.
- **The finalize guard is `status IN ('running','idling')`.** SIGUSR1 delivered
  during the idle-wait unwinds through `_wait_for_batch`'s finally, which flips
  IDLING→RUNNING before the process-end notify — the same phantom-running
  `mark_agent_exited_op` already handles. Only `idling` is ever selected for
  swap-out, so no in-flight turn is interrupted.
- **Two "live" notions diverge by design.** `count_live_agents` (the spawn cap /
  fleet-size guard) counts `hibernating`; `list_live_agent_ids` (the quiesce set of
  agents with a live process) does not, so a rollout skips hibernating agents —
  pure upside, they relaunch on new code when next woken.
- **Memory stops being the wall; the next walls are the heartbeat wake rate
  (~750 agents at the current 1.67/s ceiling) and LLM turn cost (linear in fleet
  size).** Hibernation trades ~0.4 core of cold-start CPU for the resident
  footprint — a good trade, but the thing to quantify next is wake rate and token
  cost, not memory.
- **Ships enabled at `H=450s`** (`AVA_HIBERNATE_ENABLED` is the kill switch;
  swap-in keeps running even when disabled so toggling off drains existing
  hibernating agents rather than stranding them). At 450s (above the 300s
  heartbeat threshold) the reclaimed set is essentially the paused agents; tuning
  `H` below 300s to also reclaim non-paused idle agents between heartbeats is a
  future dial, weighed against the cold start it adds to each heartbeat wake.
