# Bootstrapping a cluster update out of a pre-fix fleet

How to run `ava cluster update` when some agent-runner still executes code that
predates a fix the wave depends on — the two-hop pattern. Read this before a
rollout in a mixed-version window; `conventions/runbook.md` documents the
ordinary single-wave path.

## The hazard

A runner's Phase A drain runs the runner's **own deployed code**. On code that
predates the held-wake fix, an idle hosted agent's admission is deferred
silently: the drain burns its whole hold (minutes) and the rollout aborts with
`hold retained for agent(s) [...]`. The fix ships *in* the wave, so it cannot
help the rollout that carries it — a whole-cluster rollout over a mixed fleet
re-triggers the stall deterministically.

## Hard rules

1. **Do not start a whole-cluster rollout while any target still runs pre-fix
   code with a non-empty cohort.** Split the fleet into hops instead.
2. **Order the windows:** Hop 1 (rollout) -> converge the excluded hosts ->
   PITR activation/rollback. PITR also takes the cluster update lock
   (`cli/commands/_pitr_activation.py`), so a PITR window defers the pin
   self-heal that converges the excluded hosts.
3. Rollouts are user-triggered; the operator (Cluster Operator) executes.

## The two-hop pattern

**Hop 1 — whole-cluster rollout over a cold subset.**

1. `ava cluster mark-staging <busy-runner>` for every runner excluded from
   this hop. A staging host stays online, roster-visible, and keeps working;
   it is only removed from the fan-out (no fetch, no pause, no Phase B).
2. Drain the gateway's own cohort: terminate the agent rows that would reach
   the held-wake path (`ava agents terminate <id>`), then poll each to
   `terminated` and confirm its lease stops renewing before starting.
3. `ava cluster update` (add `--force` only when the pre-flight posture
   refuses, e.g. a stale deploy window on some host).
4. Verify with `ava cluster status`: participating hosts read pin/code OK.

**Hop 2 — per-host convergence of the excluded runners (no second rollout).**

- Each excluded host's pin controller self-heals once the update lock is
  released and its guard window passes: it fetches the track ref, re-judges,
  and spawns a local, lock-free updater (`ops/controllers/pin.py` ->
  `ops/cluster_deploy.py::spawn_update`). Expect it within a watchdog tick or
  two (`AVA_WATCHDOG_INTERVAL_SECONDS`, default 60 s).
- If nothing moves after ~15 minutes on a host:
  1. `ava cluster watchdog-probe --role agent-runner` on that host — revives a
     dead watchdog only; it does not trigger an update itself.
  2. `ava cluster update --target <machine> [--target-sha <pin>]` — the
     gateway relay to that host's ops server. 202 lands the updater session
     name + log; 503 = its ops server unreachable; 502 = an updater may
     already be in flight (retry after it settles).
- When every host reads pin/code OK: `ava cluster unmark-staging <machine>`.

## What makes the per-host update safe in the mixed window

It takes **no cluster-wide lease** (`deployment_state.phase` stays `stable`),
so the rollout admission gate falls through to the legacy zero condition and
held wakes consume normally even on pre-fix code. That is the mechanism that
has always converged single hosts after aborted waves — the same path a
watchdog off-pin self-heal takes.

## Prep the operator can trust

- `ava cluster update --dry-run` now prints a read-only per-target cohort
  readiness report before dispatching
  (`cli/commands/_update_cohort.py`): empty cohorts, `idle-hosted` rows (the
  held-wake consumers that stall a pre-fix runner), and drain-blocking residue
  (`restarting`, expired-lease). Clear the blocking rows first; in a
  mixed-version window, prefer splitting the wave over any cohort that lists
  `idle-hosted` rows on a pre-fix host.
