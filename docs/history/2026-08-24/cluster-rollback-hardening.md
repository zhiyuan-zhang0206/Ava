# Cluster rollback hardening

## Decision

Cluster rollback is a fleet transition. It uses the rollout stop-the-world
primitive, rolls the gateway back locally, writes the rollback target to the
cluster pin, then asks remote agent-runners to self-update with `mode=none` and
polls them back. A non-converged runner keeps the deploy lease in its bounded
settle state; the rewritten pin remains its watchdog fallback. `--keep-pin` is
the explicit gateway-only exception.

The health probe classifies failed liveness and population checks by data-plane
reachability. Environment-class failures alert without becoming rollback
evidence; code-class failures retain the consecutive-failure path. Successful
backend rollout targets wait in a pending-known-good state until the health
probe records two healthy passes after the ten-minute observation minimum.

## Rationale

A gateway-only rollback with an unchanged pin leaves a cluster permanently
mixed: runner watchdogs correctly converge toward the still-new pin. Likewise,
data-plane restart windows and connection blips are not evidence that reverting
code will repair the cluster. Delaying LKG promotion keeps a real previous
rollback anchor available during the first health observations of a new release.

## Boundary

The observation window is finite. Once two healthy passes promote the pending
target to `last_known_good_sha`, a problem that only manifests later on that
same commit makes automatic rollback a no-op: the target equals the current
HEAD, so `ava cluster rollback` reports nothing to roll back to and exits 0.
The health probe still alerts on the failure, and the manual path remains
available — `ava cluster rollback --to <older sha>` (or `--to <tag>`), which
re-arms the observation window for the older commit. Automatic rollback's
purpose is defending the transition, not policing a long-settled release.
