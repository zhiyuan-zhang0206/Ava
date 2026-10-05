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

Update 2026-09-04: the active transaction now records successful exclusive
stage creation and successful prior-target claim as separate durable facts.
Generation-shaped paths that merely collide with those planned names remain
unmanaged. Claim and every restoration use atomic no-replace renames, so a late
destination is preserved together with the still-tracked claimed copy. Cleanup
rejects multi-link regular files and never changes regular-file modes; only
directory permissions needed for unlink are widened.

Update 2026-09-04: stage publication and prior-target claim now have durable
write-ahead phases. Stage content is prepared under Ava's private ledger root;
publication is one no-replace rename, and recovery requires the prepared/stage
outcome plus the transaction marker and manifest. Claim recovery similarly
requires the canonical/prior outcome plus the installed marker and digest.
Ambiguous outcomes retain the transaction and all paths fail-closed.

Cleanup now records a write-ahead claim before moving each residue tree to a
transaction-specific no-replace quarantine. It verifies the post-rename tree
before deletion and restores a mismatch when the source name remains free.
Each file receives a second deterministic quarantine rename and verification
before unlink. Directory permission changes are bound to a verified descriptor
on POSIX and are skipped, allowing deletion to fail closed, on Windows.

Update 2026-09-04: pathname deletion was removed from residue cleanup. Even
after post-rename verification, a pathname can be replaced before `unlink`, and
POSIX does not provide a portable unlink-by-open-handle primitive. Cleanup now
moves the residue under the private ledger root, records every file's source
and quarantine paths plus write-ahead `claiming` state, and terminally retains
the isolated tree. Restart reconciliation never treats a deterministic
quarantine name or matching bytes as proof that Ava completed the file claim.
An ambiguous or verified file is preserved without chmod, pathname unlink, or
retry, while the newly activated external skill target remains unblocked.
