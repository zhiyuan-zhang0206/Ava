---
type: doc
title: Managed writer closure evidence
description: Operation-bound evidence and transaction fencing for managed-writer protocol admission.
tags:
  - shared
  - deployment
---

# Managed writer closure evidence

`publication.py` defines the version-2 envelope in this SAME field:
`current` is a committed exact all-unit release tuple; `pending` preserves that
predecessor while freezing ordinary births. Each unit binds machine, canonical
home, artifact/manifest digests, the observer's `ExpectedUnitWriters` digest, and
the separate complete prepared receipt digest (including service-only
declarations). The receipt filename digest from the verified inventory producer
is the latter; its narrower `expected.unit().inventory_digest` cannot substitute
for it. Collection adoption validates the full receipt digest after the trusted
producer has checked the observer tuple; it never accepts the narrower observer
digest in that slot. Beginning pending requires the current live
rollout, predecessor match and every registered unit, including stopped machines.
Expiry or crash never silently clears pending/current. Ordinary admission checks
deployment -> registry before any agent row lock, refuses unsettled deployment,
pending/new units/image-selector mismatch, and retains the row locks through birth.
Committed state outlives its originating lease; it is not liveness evidence.

`adopt_pending_collection` obtains expected challenge and full receipt hashes
from locked pending state, validates live operation/fresh observations/all units,
and preserves current without allowing admission. Cached or mismatched
acknowledgements cannot replace pending or authorize rollback. No command clears
a recorded pending publication; the one-time cutover database-records repair
removes exactly the recorded value. These transaction fences remain consumed by
runtime admission until the database-authority replacement; generic recovery
must not erase a pending operation.

No production code writes this envelope, so no activation path exists and every
admission stays protocol zero. `begin_pending_publication`,
`adopt_pending_collection`, `require_current_publication` and the barrier's
`record_collection` are tested storage contracts without a production caller.
The retired updater's migration, selector, service-readback and commit
producers are absent: nothing writes a `current` carrying the verified
activation (`activation_digest`/`activation_challenge`) that `RuntimeAdmission`
requires for protocol one. The release path's `current-release` pointer is the
two-field legacy selector, which yields no loaded publication input. Historical
version-1 collections are not converted to current permits. Protocol one must be
rebuilt on the release/fleet path
([plan](../../../future/infra/unified-cluster-lifecycle.md)).

`deployment_state.managed_writer_evidence` is nullable typed version-1 evidence
for the existing operation, never another registry or command journal. Absence
means unknown. `machine_units` is the authoritative machine/home inventory;
every row participates, including stopped units and paused/staging machines.
An orphan composed machine row refuses rather than disappearing from the count.

Prepared inventory digests cover a rollout's exact old PID/birth/session and OS
relauncher definitions; only observed old-writer absence and disabled/rebound
relaunchers can produce positive closure. A pause RPC's success is not evidence.
No credentials or full command environments belong in the receipt. This boundary
covers registered **managed** writers, not arbitrary external processes holding
shared database or cluster credentials.

The transaction order is deployment row lock, inventory table locks, agent
ownership rows. Fresh database-clock checks occur after lock acquisition,
including after waiting for inventory writes. Registration changes, missing
units, mismatched prepared inventory, replayed challenges, another lease or an
expired observation all refuse. No network or filesystem operations occur under
these locks. A cached receipt cannot establish a fresh observation.

Replacing different existing evidence is refused. The column's down migration
refuses while any evidence remains, avoiding silent removal of permission on
rollback.

## Integration boundary

Publication admission has identical synchronous/asynchronous SQL and a shared
decision function. SQL NULL evidence is the explicit never-enabled legacy state:
stable permits only existing protocol-zero behavior. A nonstable phase under a
live deploy lease, or a valid pending publication, defers new runtime admission,
preserving agent state and all inbounds; a pending publication keeps deferring
after the operation lease expires. Valid stable current evidence requires the
exact loaded image, selector, full receipt and complete registry.
Unknown versions, malformed evidence and an empty version-two record are errors,
never legacy fallbacks. These results do not authorize caller-supplied DTOs or
terminate an agent; hosted admission (`agent/ownership/hosted.py`) consumes them.

The models, transaction fence and nullable schema are not activation. Actual
rollout collection and image-bound runtime admission must consume the same
operation, and ordinary same-owner idle must retain only previously granted
capability. Until those paths are connected and reviewed, production protocol
remains zero. No installation SHA, environment flag, bare receipt or successful
RPC grants protocol one.
