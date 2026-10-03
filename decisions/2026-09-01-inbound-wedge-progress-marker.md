# Inbound wedge progress marker

The process-mode idle claim loop now records `agents_meta.last_claim_loop_at`
before every Redis wait / fallback-SELECT round. The wedged controller can act
on an idling row whose marker has become stale even when no inbound has arrived;
a renewing lease alone is not evidence that the claim loop still advances.

A Postgres column was chosen over a Redis key because the controller already
claims candidates from Postgres under its row-lock protocol. Keeping the marker
on that row preserves one durable, machine-scoped recovery decision without a
second availability or expiry contract.

`NULL` is deliberately unknown, not stale. During a rolling upgrade an old
process has no marker, and treating it as dead would restart every otherwise
healthy idling agent before it reaches the new loop. A newly booted process
writes the marker on its first idling round.
