# External-agent operator skill bridge

On 2026-09-04, Ava added a prod host-global converge step that projected the
repository's `operating-ava-cluster` skill into already-present Codex and Claude
Code global skill roots. The change addressed external agents that needed Ava
workspace and memory context while operating outside the repository checkout.

The implementation deliberately did not restore `.agents/skills/` as an Ava
fleet runtime source. It considered only `~/.codex` and `~/.claude`, skipped
missing homes, and copied only the operator skill. Copied targets carried an
Ava marker plus the digest of the last written content. That made unchanged
passes idempotent and updates safe while forcing both unmanaged collisions and
user edits into visible, non-destructive conflicts.

Updates used a complete sibling staging directory. An existing verified copy
was moved aside, the stage was activated, and an activation failure restored
the prior copy. Cleanup remained limited to the exact bridge target's unique
stage and prior-copy paths. Ordinary copied directories kept the contract
portable to Windows without symlink privileges.

The operator skill also gained a compact cross-agent lookup sequence: map an
agent through `ava agents ls`, resolve a same-host workspace from the owning
cluster home, never infer a remote workspace from the gateway home, and read
`memory/MEMORY.md` plus its linked entries before searching shared memory.

Decision:
[`decisions/2026-09-04-project-one-operator-skill-to-external-agents.md`](../../../decisions/2026-09-04-project-one-operator-skill-to-external-agents.md).

## Security and concurrency update

Adversarial review replaced marker-only authority with a private per-client
ledger under the prod cluster home. The ledger binds an installation identity,
installed generation, transaction, and exact cleanup records to the external
copy. A per-target cross-process lock serializes converge, and update now claims
the current target before verifying it so a late user edit is restored rather
than overwritten.

The copier rejects linked, reparse, and non-regular source or destination
entries and includes modes in its digest. Crash recovery distinguishes staged,
claimed, activated, and cleanup-pending generations. Activation is committed
before cleanup, making cleanup failure retryable without rolling back the
usable target. External client failures became labelled warnings while source
integrity failures remained fatal.

Follow-up decision:
[`decisions/2026-09-04-bind-external-skill-ownership-outside-target.md`](../../../decisions/2026-09-04-bind-external-skill-ownership-outside-target.md).

The recovery protocol was subsequently tightened so partial staging, a prior
copy displaced by a late user target, and partially deleted cleanup trees all
retain durable ledger pointers. Cleanup records carry an expected manifest and
accept missing expected entries on retry; unknown or modified entries still
stop deletion and remain visible as conflicts.

A later hardening pass separated planned transaction paths from paths Ava had
successfully created or claimed. Exclusive-stage and prior-path collisions no
longer acquire cleanup authority from their names. Claim and both restoration
paths use atomic no-replace renames, leaving a late user destination untouched
and retaining the prior installed authority. Residue cleanup also rejects
multi-link files and avoids chmod on regular files, preventing a hard-linked
outside inode from receiving a metadata change.
