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
still defers until phase stable. Exact commit replay returns the original UUID;
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
No production migration, normal service activation or protocol advertisement
is performed by importing or testing these helpers.

Parent: [[shared/shared.ava.okf.md|shared libraries]].
