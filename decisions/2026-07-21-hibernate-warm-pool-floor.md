# Hibernation warm-pool floor

## Context

`hibernate_idle_threshold_seconds` (see [`docs/history/2026-07-20-agent-hibernation.md`](2026-07-20-agent-hibernation.md))
already gives hibernation a time-based gate: only an agent idle past `H` is
swap-out-eligible. That gate is necessary but not sufficient on its own — it
protects an agent mid-exchange, but says nothing about an agent that is simply
*likely to be messaged again soon*. Once a fleet's resident memory already fits
comfortably in a box's budget, hibernating every single idle-past-`H` agent buys
no additional RAM; it only adds a cold-start (~1s, see the linked doc) to the
next wake of an agent that was probably going to be used again shortly. The
gate that exists (`H`) is a *time* threshold; the gap is a *count* threshold —
"keep the most likely-to-be-useful N resident no matter how long they've been
idle," the standard warm-pool shape.

## Decision

Add `hibernate_min_active` (`AVA_HIBERNATE_MIN_ACTIVE`, default **100**, `int`,
`ge=0`): this host's N most recently active agents are exempt from swap-out
regardless of idle time. "Active" is ranked by `agents_meta.last_active_at`
among `status IN ('running', 'idling')` **only** — a `hibernating` row (already
swapped out) and a `terminated` row never enter the ranking window, so neither
can occupy a floor slot and displace a genuinely active agent, no matter how
recent their own `last_active_at` happens to be. `_select_local_swap_out_candidates`
(`ops/controllers/hibernate.py`) implements the rank with a single windowed
query (`row_number() OVER (ORDER BY last_active_at DESC)` scoped to this
machine's running/idling rows) and adds `rank > min_active` to its existing
predicate — swap-out candidates are drawn only from the tail beyond the floor,
oldest-`last_active_at` first. `min_active=0` ranks nothing out (rank starts at
1), so it is exactly the pre-floor, unrestricted behavior — the floor is
additive, not a new default posture.

`hibernate_min_active` is `scope=host` (not `cluster-pinned`, unlike its
sibling `hibernate_idle_threshold_seconds`). The idle threshold is a *temporal
policy* that should be uniform cluster-wide (what "idle too long" means is not
a property of any one box); the floor is a *resource capacity* knob — how many
agents a box's own RAM budget can afford to keep warm is a property of that
box, not the cluster. A small agent-runner and a large one reasonably want
different floors. It is `remote_writable=True` (alongside `hibernate_enabled`,
`ops_concurrency`, `watchdog_interval_seconds`) so a fleet operator can tune a
specific machine's floor from the config panel without SSHing in.

## Alternatives rejected

- **Rank across all statuses, not just running/idling.** Rejected: a
  `terminated` row's `last_active_at` is stale-but-real (its last turn before
  it died), and a `hibernating` row's is frozen at swap-out time — either could
  spuriously rank above a genuinely active agent and consume a floor slot for
  an agent that isn't even resident, defeating the floor's purpose (keeping N
  *resident* agents warm).
- **A percentage-of-fleet floor instead of an absolute count.** Rejected as
  unnecessary indirection: an absolute N maps directly to a memory budget
  (`N × per-agent resident footprint`, ~36-47MB per the linked doc) an operator
  can reason about; a percentage requires knowing the fleet size to reason
  about the same thing and moves with fleet growth in a way the operator did
  not ask for.

## Consequences

- The floor is evaluated fresh every reconcile tick (the same 1s cadence as
  the rest of `HibernateController`) via a single indexed window-function
  query per host — no new polling cadence, no additional query per agent.
- `hibernate_min_active` requires all processes to restart to pick up a
  changed value (`restart_required=all`), same as `hibernate_idle_threshold_seconds`
  — both are read once into the process-local `settings` singleton, not
  live-reloaded.
