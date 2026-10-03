# The manual maintenance ladder is deleted; `ava maintenance` keeps only the hold's reader and exits

## Context

`ava maintenance` grew eight verbs in 2026-09 (`prepare`, `drain`, `stop`, `start`, `resume`,
`repair`, `stop-data-plane`, `status`) so an operator could walk one hold through its phases by
hand. Every step is a slice of a kernel that `ava stop` and `ava start` already run end to end:
`pause_agents` is `prepare` plus `drain`, `_temporary_stop` is the quiesce, service, terminal and
data-plane legs, and `ava start` releases the hold after readiness. The verbs had no consumer. The
fleet update stops with `ava stop -y` and starts with `ava start`; no cutover, rollout, watchdog,
self-heal or `ava.self.*` path spelled them, and no shell history, CLI log or journal on any of
the five hosts shows a successful production use since they landed. `stop-data-plane` reached the
same `stop_data_plane` that `ava stop -y` calls, with a weaker precondition set and no failure
compensation, and `--gateway-last` was an assertion the command never checked.

Three verbs are not slices of that ladder but the hold's exits. `status` is read by the fleet
update's start-of-work refusal and by the out-of-band triage. `repair` is the only release for a
hold latched on failed receipts: `ava start` and `ava stop` both refuse while `failures` is
non-empty, and the 2026-09-20 incident needed it. `resume --cancel` is the generation-checked
release of a hold that has not started stopping, without launching anything.

## Decision

1. Delete `prepare`, `drain`, `stop`, `start`, `resume` (the path without `--cancel`) and
   `stop-data-plane`, with `--keep-terminals` and `--gateway-last` on them, and what only they
   used: `service_stop.stop_services`, the `selected` parameter of `stop_root_service_tree`, the
   `cli-maintenance-start` log name, the settings-lite branch for `stop-data-plane`, and the
   operator-side driver stamp `run` wrote before each verb. No alias, no deprecation window; the
   removed verbs are invalid choices.
2. `resume --cancel` becomes `cancel`. With `resume` gone the flag was the only form left, so the
   verb says what it does. `oob_triage` renders the new spelling and every error message that
   named the old one names this one.
3. Error messages that pointed at a deleted verb point at `ava stop` (re-run, which resumes the
   standing hold) or `ava start` (brings services back and releases the hold). `ava start`'s
   refusal to release failed receipts now names `repair`, which it did not before.

## Alternatives rejected

- **Keep the verbs as a debug entry point, or hide them.** A hidden verb is still a second way to
  drive the same hold, and it is only ever exercised in an incident, when the hold is already in
  an unusual state. Its tests would be the sole thing keeping it correct. The kernel is reachable
  through `ava stop` and `ava start`, which every update exercises.
- **Fold `cancel` into `ava start`.** It would remove a verb, but `start` releases without the
  generation check and launches services; `cancel` is the release that must not launch anything
  (a drain that never stopped services). It also drops the CAS that `oob_triage` relies on.
  Revisit if `cancel` stays unused.
- **Keep `stop-data-plane` for "services down, data plane up, then close it later".** No runbook
  or cutover used it. If that need is real it is an option on `ava stop`, not a parallel verb
  resting on a verbal assertion.

## Consequences

- The `starting` and `ready` hold phases are no longer entered by any command; they stay in the
  journal vocabulary so a journal written before this change still decodes. Removing them is a
  separate journal-schema change.
- `ava maintenance stop --keep-terminals` was the explicit-steps way to stop services and leave
  persistent shells alone, as [delete-ava-pause](2026-10-03-delete-ava-pause.md) recorded. It is
  gone, and this change adds no replacement: what happens to shells at stop is decided by the
  persistent-terminal service work, not by a flag here.
- Multi-host ordering (runners before the gateway) was never enforced by `--gateway-last`; it is
  enforced only by the fleet update.
- A script or runbook step that ran a deleted verb fails at argument parsing. None exists in the
  repository.
