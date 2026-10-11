# The maintenance journal has no `starting` or `ready` phase; one window predicate

## Context

`decisions/2026-09-12-stop-window-contract.md` defined the quiesced window as `drained` ..
`ready` and gave the host's pending-turn scan a narrower predicate (`in_stop_leg()`,
`drained` .. `stopped`) so a booting host could scan while the unit was still held. The
start leg that justified it never materialised: `ava start` authorizes bring-up against a
`stopped` (or earlier) hold and releases the hold after readiness, and no command ever
advanced the journal to `starting` or `ready`. Only tests entered those phases, which kept
two predicates with identical membership alive.

## Decision

1. **`MaintenancePhase` ends at `stopped`.** `starting` and `ready` are deleted, with every
   branch that listed them (the `stopped` -> `starting` -> `ready` transitions, the business
   gate, the ops admission refusal, the partial-stop heal, the receipt-grading cases). The
   journal decoder rejects them like any unknown phase.
2. **One predicate: `quiesced()`.** It covers `drained` .. `stopped` and is the only gate.
   `in_stop_leg()` is deleted; the host's turn scan and the quiesced-loop lint use
   `quiesced()`.
3. **No data migration.** No code path wrote the two values, so a journal can only carry
   one if a build older than this change wrote it by hand; such a journal fails to decode
   and is removed by hand, the same as any unreadable journal.

## Consequences

- The "recovery may not wait for the hold to release" constraint of the 09-12 record is
  moot until a start phase exists again; a future start phase must reintroduce a separate
  scan predicate deliberately, with its own test.
- `oob_triage` and the operations docs list five phases.

Forward link: replaces the "`drained` .. `ready`" window and the stop-leg scan predicate of
[the stop window contract](2026-09-12-stop-window-contract.md).
