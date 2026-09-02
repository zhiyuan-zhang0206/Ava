---
type: doc
title: Managed writer closure evidence
description: Operation-bound evidence and transaction fencing for managed-writer protocol admission.
tags:
  - shared
  - deployment
---

# Managed writer closure evidence

`deployment_state.managed_writer_evidence` is nullable typed version-1 evidence
for the existing operation, never another registry or command journal. Absence
means unknown. `machine_units` is the authoritative machine/home inventory;
every row participates, including stopped units and paused/staging machines.
An orphan composed machine row refuses rather than disappearing from the count.

The rollout owns collection. Prepared inventory digests cover its exact old
PID/birth/session and OS relauncher definitions; only observed old-writer absence
and disabled/rebound relaunchers can produce positive closure. A pause RPC's
success is not evidence. No credentials or full command environments belong in
the receipt. This boundary covers registered **managed** writers, not arbitrary
external processes holding shared database or cluster credentials.

The transaction order is deployment row lock, inventory table locks, agent
ownership rows. Fresh database-clock checks occur after lock acquisition,
including after waiting for inventory writes. Registration changes, missing
units, mismatched prepared inventory, replayed challenges, another lease or an
expired observation all refuse. No network or filesystem operations occur under
these locks. A cached receipt cannot establish a fresh observation.

Bootstrap collection does not require the new column: the candidate's existing
operation-local receipt can carry `ManagedWriterCollection` as **journal input**.
After positive closure, an additive schema migration runs under the old lease;
the collection is freshly revalidated before adoption. Replacing different
existing evidence is refused. The down migration refuses while any evidence
remains, avoiding silent removal of permission on rollback.

## Integration boundary

The models, transaction fence and nullable schema are not activation. Actual
rollout collection and image-bound runtime admission must consume the same
operation, and ordinary same-owner idle must retain only previously granted
capability. Until those paths are connected and reviewed, production protocol
remains zero. No installation SHA, environment flag, bare receipt or successful
RPC grants protocol one.
