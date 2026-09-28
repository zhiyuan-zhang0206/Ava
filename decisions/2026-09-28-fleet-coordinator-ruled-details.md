# Fleet coordinator: remaining ruled details

## Context

[`cli/release_fleet/coordinator.ava.okf.md`](../cli/release_fleet/coordinator.ava.okf.md)
carried three items still awaiting a ruling after the 2026-09-27 fleet-core
release choices: whether the coordinator's phase names should track the
plan's older names or the code's, whether a unit's barrier deadline is
recomputed on a continuation, and whether an abort decided at `prepared`
refuses the operation outright or completes it.

## Decision

User ruling, 2026-09-28:

1. **Code names win.** `stopping` is the plan's `closing`; `starting` +
   `observing` together are the plan's `starting_gateway`. The coordinator,
   its journal and every in-repo description use the code's names; the plan
   (a working document kept outside the repository) is the one to change.
2. **A unit's barrier deadline is journaled in its instruction and reused by
   a continuation.** The gateway computes the deadline once when it issues
   the instruction and journals it there; resuming a stalled operation reads
   that same journaled deadline rather than computing a new one from the
   continuation's own start time. A continuation only adds a re-answer
   window: after it binds its listener, each unit has at least
   `_REANSWER_S` (30 s, `cli/release_fleet/units.py`) to answer again,
   because answers sent while the coordinator was away were never received.
3. **An abort decided at `prepared` completes as `aborted`.** Nothing has
   been dispatched yet at that phase, so there is no effect to undo; the
   operation records outcome `aborted` and exits rather than refusing to run.

## Alternatives rejected

- **Keep the plan's phase names (`closing`, `starting_gateway`) and rename the
  code to match.** The code, the journal's phase enum and the tests already
  use the code's names; renaming all of them to match a planning document
  changes no behavior. (The fleet coordinator has not run in production;
  production still runs the legacy updater.)
- **Recompute a fresh barrier deadline when a continuation resumes.** This
  would let a coordinator that was down for a long stretch keep extending a
  unit's wait indefinitely every time it comes back, defeating the barrier's
  purpose as a bound.
- **Refuse to execute an abort decided at `prepared`.** Nothing has been
  touched yet at that phase (no lease, no dispatch), so refusing buys no
  safety and only forces the operator to resubmit the same request.

## Consequences

- In-repo descriptions use `stopping` / `starting` + `observing`; the plan's
  `closing` / `starting_gateway` appear only where a docstring maps them
  (`cli/release_fleet/progress.py`).
- Because a continuation reuses the original journaled deadline rather than
  restarting the clock, a coordinator that resumes after being down past a
  unit's barrier window gives that unit only the 30 s re-answer window. A
  unit still silent after it is late, and lateness never holds the
  operation: before the fence (the candidate direction's `quiescing` and
  `stopping` barriers) the barrier raises `UnitBarrierError` and the release
  aborts; otherwise the unit is marked `unknown`, leaves the operation, and
  the workload policy judges its agents as affected. Deadlines therefore stay
  bounded, at the cost of no longer grace after a long coordinator outage.
