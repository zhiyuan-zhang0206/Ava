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
returns without a receipt and the hold is retained 300s later (#2159). Exact
commit replay returns the original UUID; different evidence does not replace
it. Database clock checks occur after locks.

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
to refuse — recovery stays explicit. The rollout's begin position (task #4128
E2-b) now reaches this seat: under an `active` decision the dispatch chain
(task #4129 channels B/C) opens the
journal through `open_pending_publication`, assembles each unit's hop
projections against the journaled single challenge, and fans the sealed plan
out -- every other decision skips the position untouched, and the seat itself
performs no filesystem or network work.
`cli/commands/_update_normal_release.py` carries the
P3/P4 call positions (migration receipt, selector CAS, normal start/observe,
`record_pending_unit_readback`): the checked activation entry drives them, and
the coordinator's per-unit continuation dispatch reaches them (task #4129 I6).

A restart-only bounce never enters the begin or collection positions: the chain is a
code-release protocol, so a plan-less bounce has nothing to journal and
publishes nothing -- the terminal design (restart-only semantics ruled
2026-09-20, task #4128); existing skip tests are its pins.

The P5 completion seat, `commit_pending_publication`, lives in the same module:
once the units recorded their normal-service readbacks, the coordinator reads
the journaled set and publishes exactly the complete readbacks through
`commit_current` — a partial set refuses, because an incomplete activation is
checked recovery's to clear. The seat writes no deployment phase, holder or
lease: ordinary admission stays deferred until the existing finalizer's release
settles the phase, and that release's pending guard is exactly what the commit
clears. The rollout wiring connects the post-Phase-B step (task #4128 E2-a):
`cli/commands/_managed_writer_wiring.py` consumes the enable point's recorded
decision and, only under `active`, runs this seat inside one short transaction
and one rollout stage -- every other decision skips it untouched, and a seat
refusal fails the rollout with the pending journal left for checked recovery.

The P2 collection position (task #4128 E2-c) sits in the same window: the
wiring module's `_collect_managed_writer_publication` consumes the recorded
decision and, only under `active`, adopts the completed units' post-stop writer
closure -- the observed facts gathered across the fleet, with the platform
final re-read after the candidate is ready -- into the pending journal before
P5 publishes. Its gathering channel is connected by task #4129 I5: the closing
section's hop verdict waits for every unit's candidate-ready journal, then hands
the phase input to the collector (`cli/commands/_managed_writer_collector.py`),
which re-derives each unit's closure from its served facts and calls
`collect_and_adopt`; the adoption seat (`adopt_pending_collection`) revalidates
the whole collection under the locked rollout before storing it. An `active`
decision with no phase input still refuses explicitly (a rollout assembled
outside its orchestration must not publish uncollected). The continuation
channel (task #4129 I6) then gates the publish on the drive's full readback
roster and closes it with each unit's commit tail; a drive refusal retains the
journal, a tail failure keeps only the commit paid.

No production migration, normal service activation or protocol advertisement
is performed by importing or testing these helpers.

Parent: [[shared/shared.ava.okf.md|shared libraries]].
