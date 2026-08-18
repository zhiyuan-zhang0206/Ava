# The rollout orchestrator is not one of its own Phase-B targets

## Context

`machines.list_agent_runners()` lists hosts purely by capability, and a single box
carries `gateway,agent-runner`. So the box orchestrating a rollout appeared in the
fan-out list it was itself driving, and that was documented as deliberate: one code
path for single-box and split, no host-mode branch.

For Phase 0's fetch and Phase A's pause it is deliberate and correct — both ops are
idempotent with what the local leg does. Phase B is different in kind. Its op is
`spawn_update`, whose first act is killing the ava-gateway session, and by the time it
runs the local leg has *already* checked this host out, migrated it and restarted it.
The leg is pure redundancy that kills the cluster's gateway.

Prod rollout 1785568439 (2026-08-01, target `df3c66b`) is the sequence in full:

1. the local leg completes; the gateway comes back on the new commit;
2. the readiness gate (#956) probes it and correctly reports `SERVING`;
3. Phase B fans out — including to this host, which kills its own gateway;
4. `gateway.log` goes silent 00:14:45 → 00:14:54, a **9 s** hole;
5. air and wsl reach their preflight inside it, take `[Errno 61] Connection refused`,
   and decline (`RESTART_DECLINED_EXIT_CODE`, rc=3) with services untouched;
6. both are reported `STALLED`. A declined host stays paused, and `PauseController`
   blocks the off-pin self-heal ahead of `PinController`, so nothing converges them
   until the settle lease lapses **~15 minutes** later.

The same race lost on the 07-30 roll, with a different host as the loser.

Nothing downstream of step 2 could have prevented this. The gate is not wrong: it
asked the right question, off-box, against the address the runners dial, and got a
truthful 200. The gateway then died *because of the rollout's own next action*.

## Decision

**Exclude this host from its own Phase-B fan-out** (`_phase_b_targets`, called between
the readiness gate and `_phase_b_outcome`). The exclusion is scoped to Phase B alone —
Phase 0 and Phase A keep the full capability list, because their idempotence is what
made the single-code-path argument right in the first place.

**And give the runner-side preflight a bounded budget** (`GATEWAY_PREFLIGHT_BUDGET_S`,
30 s) before it declares the gateway unreachable, covering the no-answer case the same
way it already covered a 5xx.

The two are not halves of one fix and must not be read that way. The exclusion removes
this hole; the budget is defense in depth for holes a future rollout opens some other
way. Shipping only the budget would leave a rollout that reliably kills its own gateway
and merely usually survives it.

## Why the budget is now the right call, when it was refused three weeks ago

`decisions/2026-07-30-accept-readiness-gate-residual-race.md` rejected a
runner-side retry on arithmetic, not on principle, and the arithmetic was sound for the
case it considered:

> A bound cheap enough to be free (3 x 5 s) only rescues a gateway that rebound within
> 15 s — which means uvicorn was still binding, and the gate cannot have returned
> `SERVING` in that state. A bound large enough to outlast a real death is ~90-120 s.
> **There is no cheap middle.**

That assumed the only way to be non-serving *after* a `SERVING` verdict is a real
death, whose recovery is bounded below by the watchdog's 60 s round. The observed
failure is not a death. The process that killed the gateway is the same process that
brings it back, on the self-update leg measured at 2-15 s. Recovery is therefore
bounded by that leg, not by the watchdog — and a cheap middle exists precisely in the
band the earlier reasoning had proved empty.

The surviving objections were priced against the real alternative rather than against
zero:

- *Paid on every runner in parallel rather than once at the gateway.* True, and it buys
  at most 30 s per runner on a path that today costs 900 s of settle-TTL for the whole
  fleet.
- *It stretches the window in which a host sits on a moved checkout with old
  processes.* Also true — the preflight runs after `git checkout --force && uv sync`.
  But that host is paused by Phase A for the duration, so nothing respawns onto the
  moved checkout, and the rollout holds the deploy lease throughout. 30 s of that,
  against a 15-minute mixed-version fleet, is not a close call.

The budget deliberately does **not** outlast a real gateway death. That case still
fails, still fails fast, and still reports `INCOMPLETE` — which is what makes the
earlier decision's "legible failure" argument survive this change intact.

## Alternatives rejected

**Sequence this host strictly last in Phase B, after every remote reports back.** Also
removes the race, and keeps the fan-out list uniform. Rejected because it preserves the
redundancy that caused the incident: the host would still be checked out twice, synced
twice and restarted twice per rollout, and the second restart would still drop every
agent it had just respawned. Ordering makes the redundancy safe; deleting it makes it
absent.

**Drop this host from `list_agent_runners()` when it is the caller.** Rejected: the
query has other consumers (the roster, the stopped-host reconcile) that all want the
full capability list, and a query whose result depends on who is asking is the kind of
implicit branch this codebase removed when it made `machine_role()` a capability set.
The exclusion belongs at the phase that cannot tolerate the host.

**Re-gate and re-fan-out at the orchestrator once, as
`2026-07-30-accept-readiness-gate-residual-race.md` prescribed for exactly this
observation.** Not built, and not because it is wrong — its cost argument still holds.
It is the recovery shape for a hole with no identifiable source. This hole had one, and
removing a cause is strictly better than adding a retry around it. That shape stays on
the shelf, unchanged, for a residual race that outlives this fix.

## Consequences

- A single box with no other agent-runner now runs an empty Phase B and reports
  `CLEAN`. Its readiness gate is still asked — the local leg starts the gateway with
  `--no-readiness-gate`, so removing the gate there would leave the question asked
  nowhere and let a rollout that never rebound the gateway report success.
- A rollout's own gateway restart is no longer a hole any runner can fall into, so a
  `STALLED` verdict once again means what it says: that host has a problem.
- `_probe_gateway_or_die` takes ~30 s to fail on a genuinely unreachable gateway,
  including on a plain `ava start`. That is the one place a healthy-path cost could
  hide, and it does not: the budget is spent only when the dial fails.
