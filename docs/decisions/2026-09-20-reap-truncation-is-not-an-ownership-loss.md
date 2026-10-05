# The reap truncation ends the turn; it is not an ownership loss

## Context

The update wave's straggler reap
([2026-09-19-straggler-reap-in-update-waves.md](2026-09-19-straggler-reap-in-update-waves.md))
CAS-marks an un-landed cohort member `'restarting'` mid-turn; that mark is the
durable truncation signal `agent.db.has_pending_interrupt` turns into an abort
for the member's in-flight exec/LLM work. What the mark is *not* is an
ownership failure: the member's generation, owner and lease are untouched, and
the drain has already released it with the honest `reaped` receipt.

The turn's fail-closed guards read it as one anyway. The native probes
(`native_status` / `require_native`) require `status IN ('running','idling')`;
the member's next guard read — the graph's hook/claim fences, the turn-boundary
settle probes, the held-controls probe — therefore raises `ImpersonationError`
("Native runtime no longer owns this agent") while the turn is still
finishing. Live on macmini 2026-09-20: the 04:55:37Z reap marked seven members
and five crashed one second later, with ~9 minutes of lease left — an
unclassified crash (`host_turn_crashed` at ERROR), an Error event on the
agent's SSE channel, a runtime drop, and a `record_failure` receipt the drain
machinery then had to fence out (task #4156). Non-takeover members get the
quiet interrupt the reap designed; their guard read still refused.

## Decision

The reap mark is classified at the host's turn boundary and ends the turn as
**truncated**, never as a crash:

- `services/agent_host/truncation.py` recognises the exact shape
  `has_pending_interrupt` fires on — the row CAS-marked `'restarting'` with its
  un-applied, non-self maintenance restart still pending/claimed — **bound to
  the turn's own generation/owner**.
- `_invoke_until_done`'s exit boundary turns that refusal into
  `TurnOutcome(truncated=True)`: no corpse marker, no Error event, no failure
  receipt; the cached runtime is dropped so the successor's admission is cold
  and re-runs the startup reconcile (the reap's re-delivery promise).
- The held-controls probe is guarded by `reap_truncation_stop`, which stops the
  held wake quietly: the successor boundary that settles the mark owns the row
  and its un-applied command.
- Every other ownership refusal still raises, and the mark's own write fences
  are untouched: old-incarnation writes still die on the status predicates.

## Alternatives rejected

- **Relax `native_status`/`require_native` to admit `'restarting'`.** The
  shared guard is the impersonation coordination surface: admitting a row that
  is being torn down would let a dying incarnation keep presenting consent and
  serving lease mutations. The fence is the point.
- **Treat any `ImpersonationError` during a turn as a truncation.** A replaced
  row (foreign generation/owner) and a genuinely lapsed lease are real
  ownership losses; swallowing them would hide the exact failures the guard
  exists to catch. The classification binds to the incarnation for this reason.
- **Make the drain-side reap settle the turn.** The reap must stay bounded by
  its window W and outside the member's process; the turn boundary is the only
  place both the refusal and its cause are knowable, and the successor settle
  already owns the row.
- **Leave it as a crash.** A designed truncation surfaced as an unclassified
  failure: an operator-visible error event, a receipt the drain has to fence,
  and a corpse-adjacent settle path for a row that is perfectly healthy.

## Consequences

- Truncation is observable as `host_turn_truncated` (turn) /
  `host_held_wake_truncated` (held wake) at INFO; the member's `reaped`
  receipt stays the authoritative drain record.
- The classification costs one extra read per *refused* turn — the reap path
  only; the hot path is unchanged.
- The shape read is a second consumer of the mark: a new writer of
  `'restarting'` or a widened reap CAS must update it together with
  `has_pending_interrupt` (the write-side guard in tests enumerates them).
- The company-air family — an external stall-terminate invalidating an
  in-flight turn — is deliberately NOT covered; it still crashes as before and
  would be its own decision.
