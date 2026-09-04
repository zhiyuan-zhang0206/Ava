# Bind external skill ownership outside the target

## Context

The initial external-agent projection proved ownership with a marker stored
inside the copied target. That marker traveled with the directory and could be
copied or spoofed. The update check also happened before the current target was
claimed, leaving a race in which a late user edit could be overwritten. Process
interruption and concurrent converge runs needed one durable transaction owner.

## Decision

Bind each client installation to a private ledger under the prod cluster home,
outside the user-owned client directory. The ledger carries a random
installation identity, the installed generation and digest, the active
transaction, and exact cleanup records. The in-target marker is supporting
evidence only; both records must agree before Ava changes or removes a target.

Serialize each client's target with a cross-process lock beside its ledger.
Updates first stage a no-follow copy, then atomically move the current target to
a generation-specific prior path and verify that claimed directory against the
ledger. A mismatch is restored without replacing any path that appeared late.
Activation is committed to the ledger before cleanup, so cleanup failure leaves
the new target authoritative and retryable. Recovery interprets the ledger and
the exact generation paths rather than treating a temporarily absent target as
a fresh install.

Digests cover relative names, entry kinds, bytes, and file and directory modes.
Source and client trees reject symbolic links, junctions, reparse points, and
non-regular entries. Copying uses regular files and directories and therefore
does not depend on symlink support on Windows.

External client integration remains optional. A client path conflict, lock
timeout, permission failure, or rename failure produces a client-labelled
warning and does not abort core converge. A repository source-integrity failure
remains fatal because it indicates Ava cannot prove what it is publishing.

## Consequences

- A copied or fabricated marker cannot grant Ava authority without the private
  ledger and its matching installation identity.
- Permission-only changes are user modifications and are preserved.
- Transaction residue is removed only when its generation marker and complete
  digest match the ledger; otherwise it is preserved for inspection.
- Routine output names the client and relative skill target, not the user's
  absolute home path.
- The scope remains exactly Codex, Claude Code, and
  `operating-ava-cluster` in prod host-global converge.

Supersedes the ownership and replacement protocol in
[`2026-09-04-project-one-operator-skill-to-external-agents.md`](2026-09-04-project-one-operator-skill-to-external-agents.md),
not that decision's distribution boundary.

Update 2026-09-04: cleanup records now carry the complete expected path
manifest, not only the generation's whole-tree digest. The ledger moves all
remaining stage and prior-copy pointers into cleanup state before clearing a
transaction. Cleanup validates that the current residue is an unmodified subset
of that manifest, allowing a later converge to resume after some expected paths
were already removed while still preserving any unexpected or modified path.
