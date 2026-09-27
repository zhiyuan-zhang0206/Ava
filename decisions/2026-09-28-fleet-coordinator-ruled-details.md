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
   `observing` together are the plan's `starting_gateway`. The coordinator
   and its docs use the code's names; the plan document is updated to match
   instead of the reverse.
2. **A unit's barrier deadline is journaled in its instruction and reused by
   a continuation.** The gateway computes the deadline once when it issues
   the instruction and journals it there; resuming a stalled operation reads
   that same journaled deadline rather than computing a new one from the
   continuation's own start time.
3. **An abort decided at `prepared` completes as `aborted`.** Nothing has
   been dispatched yet at that phase, so there is no effect to undo; the
   operation records outcome `aborted` and exits rather than refusing to run.

## Alternatives rejected

- **Keep the plan's phase names (`closing`, `starting_gateway`) and rename the
  code to match.** The code and its journaled phase values are already
  running in production; renaming call sites and journal enum values instead
  of updating a plan document risks a migration for no behavior change.
- **Recompute a fresh barrier deadline when a continuation resumes.** This
  would let a coordinator that was down for a long stretch keep extending a
  unit's wait indefinitely every time it comes back, defeating the barrier's
  purpose as a bound.
- **Refuse to execute an abort decided at `prepared`.** Nothing has been
  touched yet at that phase (no lease, no dispatch), so refusing buys no
  safety and only forces the operator to resubmit the same request.

## Consequences

- The plan document (`fleet-and-cutover-plan.md`) is corrected to use
  `stopping` / `starting` + `observing` wherever it previously said `closing`
  / `starting_gateway`, so future readers are not misled by stale names.
- Because a continuation reuses the original journaled deadline rather than
  restarting the clock, a coordinator that resumes an operation after it has
  been down for close to (or longer than) a unit's original barrier window
  will find that unit already past deadline and immediately time out into
  the held-for-operator state (per FC-7's ruling that a stalled operation
  stops and waits rather than retrying automatically). Deadlines therefore
  stay bounded, at the cost of no automatic grace extension after a long
  coordinator outage.
