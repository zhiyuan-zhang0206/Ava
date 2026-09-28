---
type: doc
title: Release write generations
description: Each release direction fences the active database write generation and admits a fresh one, journaling intent and receipt per ledger transition.
tags: [cluster-lifecycle, release]
---

# Release write generations

Every direction runs on a fresh database write generation
([[shared/cluster/authority/authority.ava.okf.md|write-generation authority]]);
`authority.py` orchestrates it and `cli/commands/data_plane/write_generation.py`
performs the data-plane effects. The home ledger is the authority; the journal
(`authority_evidence.py`) carries one `Fence` and one `Issue` per direction:
intent before each ledger transition, a non-secret receipt after it.

- **prepared** also checks, read-only, that exactly one admitted generation
  exists: ledger active with nothing pending or unclosed, the catalog
  invariant for it, no prepared transaction, and a pooler userlist serving
  exactly it.
- **fencing** (root absent): `Fence(revoking)` names the ledger's active
  generation (for a recovery, exactly the candidate's issue), then revoke and
  the NOLOGIN sweep, the owned pooler stopped (escalated to a kill when the
  safe shutdown cannot finish; no listener may remain), termination and census
  until no stale session or prepared transaction survives (ledger `closed`,
  secret deleted), and a prune without CASCADE; `Fence(closed)` records the
  census, the pooler outcome and the ledger's drop outcome. A census failure
  holds, never closes.
- **authorizing** (target selected, root absent): `Issue(minting)` records the
  number the ledger will allocate, then mint (secret, `pending`, the two
  LOGIN roles), a fresh pooler serving exactly the pair, a pooled `SELECT 1` as
  each login, ledger `active`, and `Issue(authorized)` with its credential
  digest. A retry reconciles the recorded number exactly or holds; a foreign
  pending generation or another allocation refuses before any effect.
- **starting**: the stage refuses unless the ledger's active generation is
  this direction's authorized issue, so the launch delivers and binds only it.
- **restoring** (an abort): nothing was fenced or minted; the stage refuses
  unless the active generation is still the one `prepared` recorded.
- **observing / resuming**: after readiness the stage proves the issued
  generation is active, the invariant holds, no fenced session survives, the
  pooler serves only that pair, and both logins answer.

The finite executor itself runs the candidate image, which the boot pass never
admits to a generation. It adopts, in-process, the OS-user administrator over
the owner-only socket acting as `ava_gateway` (`peer`, startup
`-c role=ava_gateway`, which `RESET ALL` keeps): no fence census includes its
session, so the same authority serves every phase, before and after the
selector moves and across the generation it mints. Its launch environment is
fixed (home, registry, `HOME`, `PATH`) and carries no login. The submission
that launches it, the same candidate image before any operation exists, reads
the registered units with the gateway login the previous image's handoff
passed in its exec environment
([[cli/release_handoff/release_handoff.ava.okf.md]]). PITR operations and
aborts carry neither record and reuse the active generation; a remote unit
receives its generation over the coordinator channel (slice dbgen-8).
Networked fleets refuse before any effect (`cli/release_fleet/inventory.py`).
