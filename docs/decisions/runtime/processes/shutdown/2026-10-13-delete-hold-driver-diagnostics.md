# The maintenance hold records no driver process; `oob_triage` is deleted with it

## Context

A maintenance hold (`deploy-pause-owner.json`) used to carry a `driver`: the pid, birth time and
argv of the process tree that ran the `meiva stop` which took it, re-stamped at every ladder step
(`hold_driver`, `pause_owner.refresh_driver`, the `driver=` plumbing through `cohort.prepare` and
`pause_agents`). Its only reader was a probe, `liveness()`, shown as `driver=alive/dead/missing`
by `meiva status` and as `hold.driver` by `meiva status --json`. Nothing acted on a `dead` reading.
The controller that once released a stranded hold on it (`stranded_pause`) was deleted on
2026-09-29, and the multi-command ladder that needed a shepherd across commands went with
[the manual maintenance verbs](../../updates/execution/converge/2026-10-03-delete-manual-maintenance-verbs.md).
Today a hold is taken by `meiva stop` or `meiva restart` and released by `meiva start`, and nothing
runs the steps one at a time.

The question the probe answered ("is another stop still running?") already has a stronger
answer. `meiva stop`, `meiva restart` and `meiva start` run under one exclusive OS lock on the home
whose holder record names the last holder's pid; the lock dies with its process. The status
journal `run/lifecycle-op.json` records the pid, the phase timeline and the completion of the
running or last stop. Neither can be stale in the way a recorded argv and birth time can.

`scripts/host_ops/oob_triage.py` was the only consumer of `hold.driver`: an offline script that
classified a held host from `meiva status --json` (falling back to the raw journal) and rendered an
`meiva start` command. Its main read path took `payload["driver"]` and, on a missing key, fell back
silently to the raw journal and classified every active hold `undetermined`. Its `stranded` class
read a `failures` field that holds no longer carry.

## Decision

1. **Delete the driver.** `hold_driver` and its test helpers, `PauseOwnerSnapshot.driver`,
   `refresh_driver`, the `driver` / `refresh_driver` parameters of `begin_maintenance`,
   `change_maintenance`, `cohort.prepare` and `pause_agents`, the stamp `meiva stop` minted, and the
   now unused `create_time_matches` primitive.
2. **Shrink the hold report, keep it.** `meiva status` and `meiva status --json` still print the
   hold's status, operation, acquired-at, maintenance payload and scope, offline and without
   probing anything. `status` and `maintenance.phase` are what the fleet update's start-of-work
   refusal reads; they are unchanged. A test pins the exact key set of the `hold` object.
3. **Delete `oob_triage`, script and tests, in the same change.** Splitting them would leave the
   script silently degraded to `undetermined`. Its remaining content (read the hold, print
   `meiva start`) is what `meiva status` already shows.
4. **Document the replacement.** The operations guide now says how to tell whether a stop is
   still running: `lifecycle-op.json`, or `meiva start`, which names the lock holder when it cannot
   take the lock.
5. **Old journals.** The journal decoder reads only the keys it knows, so a journal written with a
   `driver` key reads whole (the key is ignored, never parsed, so a malformed value cannot make a
   hold unreadable) and the next write omits it. A fleet update stops on the old code and starts
   on the new one, so this is needed for one update window; a test covers the round trip. An old
   binary reading a new journal sees no `driver`, which it already treated as `missing`.

## Alternatives rejected

- **Keep the probe as read-only diagnostics.** It cost several hundred lines of production code and
  tests, a second copy of process-identity logic that nothing killed, released or took over on,
  and it answered a question a lock answers better. A diagnostic that no decision reads is the
  part of a recovery stack that stays after the recovery is gone.
- **Strip only `driver` from `oob_triage`.** What is left is a primary and a fallback read that
  now carry the same information and a classification with one live branch.
- **Add an alert for an orphaned hold.** A hard-killed CLI can leave a hold that blocks new
  admission with nothing to flag it; that gap predates this change and the probe only gave a
  human something to notice. An alert is a new mechanism and belongs to its own decision.

## Consequences

- `meiva status` no longer says whether the process that took the hold is alive. An operator reads
  the hold's age from `acquired_at` and the process state from `lifecycle-op.json`.
- `meiva status --json` loses its `hold.driver` object. The repository has no other reader.
  Anything outside it that parsed `driver` must stop.
- No hold exit changes: `meiva start`, a rerun of `meiva stop`, and a failed restart releasing the hold
  its own process took never read the driver, and the compare-and-set on `(holder, acquired_at,
  maintenance)` never included it.
- The process-identity primitives that kill or adopt processes (`stable_create_time`,
  `pid_starttime_ticks`, `OwnedProcess`) are unrelated and stay.
