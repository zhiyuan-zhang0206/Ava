---
type: doc
title: Deploy pause owner
description: Host-local exact capability journal for DB-independent pause compensation.
status: current
---

# Deploy pause owner

`$AVA_HOME/run/deploy-pause-owner.json` records the exact central deploy-lease
identity `(holder, acquired_at)` that most recently paused this host. A stop
request carries that identity; under the lifecycle mutex the runner verifies it
against the live executing lease, atomically journals `paused`, verifies the
lease again, then mutates posture. A mismatch or replacement writes no posture.

Resume never mints or rereads a current DB capability. It matches only the
local journal, so it still works while the gateway or Postgres is unavailable
and a delayed generation A resume cannot unpause generation B. Matching
`paused` unpauses then records `resumed`; matching `resumed` is an idempotent
no-op. Before unpausing, a fresh pending or live running local updater handoff
blocks a stale deploy resume. Recovery clears only its captured exact journal
after the normal no-live-owner proof and successful unpause.

A rollout also ends a pause without a resume op: the Phase-B `ava start`
returns the host to `idle` directly, and the gateway-local `finally` unpauses
the co-located host itself. Both paths must record the journaled generation as
`resumed` (the generation-scoped successful-finalize
`pause_owner.finalize_natural_resume`), or the journal stays `paused` forever
while the host serves — the 2026-08-26 residue. The finalize is generation-
scoped by construction and never a force-clear: only a `paused` journal is
transitioned, and only to its own generation.

During the rollout that first adopts this protocol, the old receiver handled
stop and therefore wrote no exact journal, while the still-old orchestrator may
resume the newly updated receiver with an empty payload. Only an absent journal
may take this compatibility path; it writes a `legacy-resumed` tombstone for
idempotency. Exact or malformed owners and live local updater handoffs refuse it,
and any later exact stop overwrites the tombstone.

The control-plane stop/resume payload requires both fields and a timezone-aware
RFC3339 timestamp. Missing, naive, or mismatched capabilities fail closed. Old
receivers ignore the new payload; a new receiver accepts an old tokenless
resume only through the one-rollout inactive-journal bridge above. Full
delayed-request protection begins after stop-side protocol adoption on every
node and degrades when rolling back to an older target.


An explicit [maintenance hold](maintenance.ava.okf.md) uses the same journal
with a typed cohort/progress payload. It has no automatic expiry. Ordinary
compensation, natural startup finalization, force-clear and a newer rollout
cannot release or overwrite it; only the exact maintenance start/resume path
can do so. This is distinct from a recoverable stranded rollout pause.
