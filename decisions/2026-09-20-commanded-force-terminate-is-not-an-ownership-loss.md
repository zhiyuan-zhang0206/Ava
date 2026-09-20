# A commanded force terminate is not an ownership loss

Date: 2026-09-20
Task: #4180 (from the #4178 triage)
Status: implemented (this PR)

## Decision

When an externally commanded force terminate is applied to a hosted turn's own
incarnation — the delivery watchdog's hosted-turn wedge recovery, a CLI/operator
force, a machine pause — the turn's next fail-closed guard refusal
(`shared.impersonation.native_status`: "Native runtime no longer owns this
agent") is classified as a **deliberate termination**, not an ownership loss.
The host ends the turn as `TurnOutcome(truncated=True)`: no crash record, no
error event, no failure receipt. Every other ownership loss still raises
(fail-closed, unchanged).

## Predicate (bound to the command, not the row)

An exception classifies iff it is `ImpersonationError` and the database still
holds *this incarnation's own applied force command*:

- `inbound_messages`: `kind='terminate'`, `status='claimed'`,
  `applied_at IS NOT NULL`, `observed_at IS NULL`,
- `target_generation`/`target_owner` equal the turn's own incarnation,
- `agents_meta.lifecycle_command_id` still points at that command.

`services.agent_host.force_termination` owns the predicate; the single turn
exit boundary (`host._invoke_until_done`) and the held-controls wake
(`host._run_held_controls`) are the two classification sites, mirroring the
straggler-reap truncation (tasks #4164/#4156).

## Why the command's target, not the row

The wedge recovery's window has two states: (a) the row is `terminated` with
the force unobserved; (b) the resurrection already ran — the final CAS has
NULLed the row's `runtime_generation`/`runtime_owner` — while the command and
the live pointer survive. Binding to the command's stored `target_*` covers
both; a row-based predicate would classify only the first.

## Why no source whitelist

The durable shape is identical for `source='system'` (delivery watchdog),
`'user'` (CLI/operator force) and `'machine-pause'`, and in every case the
refusal is the commanded end of *this* incarnation, not a surprise. One rule,
no special cases; the narrower system-only variant was considered and declined
(it would keep CLI forces noisy and add a list to maintain).

## What deliberately stays

- The guard is not relaxed: `native_status` / `require_native` refuse exactly
  as before; the classification happens at the host's exit boundary, where the
  *reason* is known.
- The command is not consumed here. Its observation stays with the pump's own
  boundary (`shared.hosted_force.original_host_force(quiescent=True)`) and the
  boot recovery — verified live: in both 6240 incidents the observation landed
  within 0.3 s of the crash record it replaces.
- The wedge-detection event (`host_turn_stall_detected`, error level) stays
  loud. Only the unclassified-crash noise is removed.

## Evidence

company-air 6240, two recurrences (2026-09-19 16:49:58Z, 2026-09-20
07:33:49Z): wedge detected (`host_turn_stall_detected`, age_s 29701 / 5907),
force terminate applied (inbound 230366 / 236109), the same pump's next guard
read refused, and the finished turn was recorded as `host_turn_crashed`
(ImpersonationError) although the pump then observed the very command that
ended it. See also the #4178 triage in the task log.
