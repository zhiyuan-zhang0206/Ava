---
type: doc
title: Pending service activation
description: Existing rollout authority fences migrations, selector CAS, normal service startup and all-unit publication.
---

# Pending service activation

`managed_writer_activation.py` is the existing updater's short-transaction
authority API, not an RPC, controller, liveness scanner or agent-birth permit.
`managed_writer_publication.py` owns the same version-2 current/pending JSON.

The verified prepared plan fixes every registered unit, full inventory receipt,
schema migration SET, exact service session/command/executable/entrypoint and
selector-v2 predecessor/new bytes digests. Native and Python services share the
roster; Python also requires retained loaded-module evidence. Unsupported native
adapters must be rejected by the producer before quiesce. This interface rejects
Windows plans until a matching normal-start adapter is available.

`require_pending_migration` requires adopted, fresh all-unit writer closure.
`record_pending_migration` reads the actual locked `schema_migrations` SET and
records database time; an ACK or candidate validation before migration is not a
migration receipt. The existing migration runner still verifies schema content.
`require_pending_selector_change` precedes the existing local selector CAS.
`require_pending_candidate_start` authorizes only an exact listed normal service
after selector readback. Every effect rechecks the fixed operation's live lease;
there is no reusable bearer permission or renewed deadline. `pending_stage` is a
bounded-wait hint, never effect authorization. The existing updater retains its
unit flock and absolute deadline; no database transaction spans OS work.

`commit_current` revalidates all registered units and complete normal-service
readbacks, selector predecessor/new bytes, challenge, image paths and observation
windows. It writes current and clears pending atomically, retaining the existing
deployment phase/lease for the existing finalizer. Ordinary admission therefore
still defers until phase stable; the one
continuation exempt from that freeze is a held maintenance command the
same boot already owns (`admit_hosted_runtime`), because a rollout's own
pause must not defer the drain it requires — otherwise every held wake
returns without a receipt and the hold is retained 300s later (#2159). Exact commit replay returns the original UUID;
different evidence does not replace it. Database clock checks occur after locks.

Detached unit updaters use `record_pending_unit_readback` to persist their exact
native/health/selector observation in that same pending field. Equal retries do
not update timestamps; conflicting results refuse. The original coordinator
uses `read_pending_unit_readbacks`, which revalidates freshness but may return a
partial tuple, then publishes only the complete exact set. No callback server,
new registry or second completion controller is introduced. The infrastructure
session `ava-agent-host` is allowed in the prepared roster; other `ava-agent-*`
names remain refused, including numeric agent and attempt sessions.

The trusted updater obtains process birth, supervisor/child relationship, exact
argv/environment projection, native health and authenticated runtime identity
outside the transaction. These typed DTOs validate bindings; constructing them
is not authentication or proof that a service is alive. No public caller may
submit them as a ready claim. HTTP/filesystem production adapters belong to the
updater integration, not this storage contract.

Rollback is another explicitly prepared operation with fresh closure and a
compatible migration SET. Old evidence without the new plan/receipt is not
upgraded into authority. Readers which cannot parse this additive v2 shape must
not be selected as rollback runtimes; resource-state/schema compatibility is a
preparation gate, never forced deletion or a hard-coded historical release.

`cli/commands/_update_publication.py` is the P1 journal seat for the existing
updater's Phase-0 window: `build_pending_publication` assembles the complete
registered-roster `PendingPublication` from per-unit prepared facts (the sealed
receipt plus its byte digest, the candidate image digests, and the unit's
normal-service plan when the release includes one), and
`open_pending_publication` opens the journal inside the caller's transaction.
The single observation challenge is minted once per operation: a same-operation
retry adopts the journaled challenge, because the begin retry check compares the
whole entry, while a pending entry from another operation is left for that check
to refuse — recovery stays explicit. The seat is inert until a rollout wiring
change connects it: no production module imports it, and it performs no
filesystem or network work. `cli/commands/_update_normal_release.py` carries the
P3/P4 call positions (migration receipt, selector CAS, normal start/observe,
`record_pending_unit_readback`) immediately behind the checked-activation gate;
the gate remains the only blocker, and no production path reaches them yet.

The P5 completion seat, `commit_pending_publication`, lives in the same module:
once the units recorded their normal-service readbacks, the coordinator reads
the journaled set and publishes exactly the complete readbacks through
`commit_current` — a partial set refuses, because an incomplete activation is
checked recovery's to clear. The seat writes no deployment phase, holder or
lease: ordinary admission stays deferred until the existing finalizer's release
settles the phase, and that release's pending guard is exactly what the commit
clears. It is inert on the same terms: no production module imports it until the
rollout wiring slice connects the post-Phase-B step.

No production migration, normal service activation or protocol advertisement
is performed by importing or testing these helpers.

Parent: [[shared/shared.ava.okf.md|shared libraries]].
