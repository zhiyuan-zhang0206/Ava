# A liveness record is written by the process whose liveness it records

## Context

The fleet roster's liveness fields had one writer: `ava start`, at the tail of a
supervised bring-up, via `shared.machines.register_self()`. That call does two
things to the unit's `machine_units` row — clears `stopped_at` and stamps
`last_seen_at` — and both are claims about a *running process*, made by a command
that then exits.

A host reaches "serving" by several paths that are not a completed `ava start`:

| path | ran `ava start`? |
|---|---|
| operator runs `ava start` | yes |
| OS-scheduled autostart job | brings sessions up; may not complete the full start |
| `agent-runner-watchdog` respawns the `ops` session | no |
| the restart leg of a rollout | not as a completed start |

On every path but the first the `stopped_at` latch set by the previous `ava stop`
survived, and nothing cleared it while the ops daemon answered every op. So
`ava cluster status` reported a runner **stopped** while its cron autostart
had in fact brought it up, and the `ava update` fan-out — which filters on that
same latch — dropped a live host from a migration-carrying rollout (the
2026-07-28 rollout: 3 targeted, 4 known).

This is the second half of a defect class whose first half was fixed the day
before ([[2026-07-28-process-state-over-disk-bookmarks.md]]): a fact about a live
process, sourced from something that is not that process.

## Decision

**The ops daemon calls `register_self()` at its own boot**, once its health server
is serving (`services/agent_ops/daemon.py:_register_boot`). The daemon is the only
long-running Ava process on an agent-runner and the one the gateway probes, so it
is the process the row is about.

Two consequences fall out of the choice, both deliberate:

- **Non-fatal.** It is the one step in that startup sequence that does not exit on
  failure. `assert_schema_current` exits because a schema-skewed daemon *dispatches
  ops incorrectly*; a failed registration refresh leaves dispatch entirely correct
  and only leaves the row stale. Exiting would hand the watchdog a respawn loop
  that takes the host dark for the gateway — the outage the call exists to prevent.
- **One definition of the dial URL.** Both writers resolve the address through
  `shared.machines.unit_dial_url(roles)`. Two writers of one row that each computed
  the shape themselves would be free to advertise different addresses for the same
  unit, and the loser is a host the gateway dials where nothing answers. The logic
  moved out of `cli/commands/_repo.py` unchanged.

## What `last_seen_at` means, and why it stays one field

The suspicion worth checking was that `last_seen_at` conflates two facts — "the ops
daemon is alive" and "something last wrote a heartbeat row" — and should split.
Auditing every reader says otherwise: **the second fact has no writer.** Nothing
heartbeats `machine_units` or `machines`. There is exactly one fact —

> the last time a process owning this unit announced the unit was up

— and the defect was that its only writer was not the process it spoke for. Adding
the daemon does not add a meaning; it makes the existing one true on the paths
where it was false, and true *more often* (a daemon boots more frequently than a
full `ava start` happens).

Liveness proper is `MachineStatus.online`, a live `status_probe`, which never reads
this column. No decision path reads it either — not the fan-out
(`list_agent_runners`, which filters on `stopped_at`), not the settle-hold release
(`ops/deploy_window.py:settle_hosts_converged`, which re-probes), not the pin
verdict. Its only consumers are two display sites. A second column would therefore
ship with no readers at all.

What *is* wrong is the name. `last_seen_at` invites heartbeat semantics that the
column has never had, and its two renderers already disagree about it: the CLI
titles it `up since`, the frontend `Last seen`. The honest end state is a rename to
`up_since_at`. Under the expand-contract rule that is its own migration pair plus a
generated-types and frontend pass, so it is deliberately not folded into this fix —
the field is display-only, and a rename buys clarity, not correctness.

## Alternatives rejected

**A heartbeat that refreshes `last_seen_at` on a timer.** Would make the column's
name true and give a freshness-based liveness signal. Rejected: the cluster already
has a liveness signal that is strictly better evidence — a synchronous
`status_probe` that proves the daemon is *answering ops*, not merely that a writer
loop is alive — and a heartbeat would add a write per host per interval to the
central DB to produce a weaker answer. It also invites readers to test freshness
instead of probing, which is how a roster starts lying during a rollout when hosts
are legitimately busy.

**Have the watchdog clear the latch when it respawns the ops daemon.** Closes the
respawn path only, and puts the claim back in a process other than the one it is
about — the same shape as the bug. The watchdog would also have to know it
respawned rather than merely found the daemon alive.

**Clear the latch from the gateway when a probe answers.**
`clear_stopped_marker()` already does exactly this, from the rollout's fan-out
reconcile. It stays, and stays needed, but it cannot be the primary fix: it clears
only the *composed* `machines` row, because a probe proves that some unit answers
at the machine's dial URL, not which one — blanket-clearing the unit rows would
resurrect a peer unit that really is stopped. Only the unit itself knows it is the
one that came up.

## Consequences

- A gateway-only unit (split deployment, `~/.ava_gateway`) runs no ops daemon, so
  its row is still written only by `ava start` and can still carry a stale latch.
  The gateway process could register itself the same way; not done here to keep the
  change to the daemon whose absence caused the incident.
- `register_self()` from the daemon rewrites the unit's capability flags and dial
  URL from the daemon's boot-time view of `machine_role()`, which is the same
  env-and-file source `ava start` reads. A capability file edited while the daemon
  runs is picked up at its next boot, not before.
- `stopped_at` remains a latch that can outlive its condition on the narrow paths
  above, so the rollout's `_resolve_fanout_targets` reconcile and the roster's
  `STALE-STOP` cell stay as-is. The probe remains the authority; this change just
  makes the common case stop producing the disagreement.

---

The rename this decision named and deferred was carried out in #981: the column
is `up_since_at` on both roster tables, and the frontend now titles it "Up since"
like the CLI always did. This file is left as written — `last_seen_at` above is
the name the column had at the time.
