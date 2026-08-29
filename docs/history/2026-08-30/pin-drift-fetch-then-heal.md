# Pin-drift self-heal fetches before judging an unknown pin (task #1996)

## Context

The agent-runner watchdog's pin controller reconciles a host's prod-source HEAD
against the cluster pin (`cluster_target_sha`). When the pin commit is not
present in the local object store, `prod_source_pin_relation` answers
"unknown" — and `check_pin_drift` used to log and defer forever without ever
fetching. That made the convergence story for a runner excluded from a
rollout's fan-out false: such a host never has the new pin fetched, so it read
unknown every tick and stayed off-pin indefinitely (QA M1 debt #621).

## Decision

The unknown branch now fetches the track ref (`origin/<track_branch>`) into the
prod-source checkout before judging, then re-runs the relation:

- unknown → fetch → **behind / diverged**: heal proceeds as before (guards,
  spawn self-heal update to the pin).
- unknown → fetch → **ahead**: converged at-or-above the pin — warn, no
  downgrade (the pin=floor semantics from the 2026-08-25 incident are
  untouched).
- unknown → fetch fails / pin still absent: defer as before, retry next tick.

The fetch is best-effort and bounded (30s ceiling via `run_bounded`, tree-kill
on expiry), touches only the object store and FETCH_HEAD — never the working
tree — so it is safe to run in the watchdog tick and does not race a
concurrent checkout. The heal itself still goes through `trigger_update`,
whose own fetch + checkout are unchanged.

## Consequences

- An excluded runner converges on the next tick after the network recovers,
  instead of deferring forever.
- A wedged network costs at most the fetch ceiling per tick in the unknown
  state, and the deferral (with the same warning, now saying a fetch was
  attempted) still stands — never a blind force-checkout.
- New shared primitive `shared.cluster_drift.prod_source_fetch(*refs, repo)`
  for the fetch; tests cover fetch-then-heal, fetch-failure deferral, and
  no-downgrade-when-ahead.
