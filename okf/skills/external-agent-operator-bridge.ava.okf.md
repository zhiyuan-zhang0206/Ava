---
type: doc
title: External-agent operator skill bridge
description: Prod host convergence copies only operating-ava-cluster into already-present Codex and Claude Code global skill roots, with externally bound ownership and serialized recovery that preserve conflicts.
tags:
- extensions
- agent-instruction
- lifecycle
---

# External-agent operator skill bridge

`cli/commands/_converge_external_agent_skills.py` is a dedicated host-global
converge step. It reads
`<repo>/.agents/skills/operating-ava-cluster` and considers exactly two client
homes: `~/.codex` and `~/.claude`. A missing client home is a no-op; the step
does not create either top-level directory. Because the step has
`host_global=True`, `converge_host` runs it only from the default-home prod
checkout and skips both `.worktrees/` and `.claude/worktrees/` checkouts.

For each present client, the target is
`<client-home>/skills/operating-ava-cluster`. Ava creates a complete staged copy
beside that target. One validated in-memory source snapshot supplies the
manifest, digest, and staged bytes for every present client, so publication
never re-reads a moving Git checkout or mixes source generations. Pathname and
opened-handle metadata are compared by stable file identity across those API
families, while full before/after signatures remain local to each family; this
avoids Windows metadata-representation mismatches without weakening mutation
detection. A private per-client ledger under
`$AVA_HOME/configs/external-agent-skills/` records an installation identity,
installed generation and digest, active transaction, exact cleanup records,
and terminal private-retention records;
the target's `.ava-managed.json` marker must match that external authority. The
digest covers relative paths, entry kinds, file bytes, and file and directory
modes. Each transaction, installed generation, and cleanup record also carries
the expected path manifest. Moving a residue from transaction state to cleanup
state is one atomic ledger write, so no live stage or prior copy loses its
durable pointer. Write-ahead stage-publication and target-claim phases precede
their no-replace renames. Recovery reconciles source and destination presence
against the transaction marker, digest, and manifest; a merely colliding or
otherwise ambiguous generation-shaped path never becomes Ava-owned.

A per-target cross-process lock serializes convergence. A source change stages
a complete no-follow copy, atomically claims the current target, and verifies
the claimed directory before activation. A mismatch is restored without
overwriting a path that appeared late: claim, activation, and restoration all
use atomic no-replace renames. Activation is recorded before cleanup, so a
cleanup failure leaves the new copy authoritative. Cleanup first records and
atomically moves each residue tree to a quarantine under the private ledger
root. For every file, the ledger stores the source and quarantine relative
paths and durably transitions through `source`, `claiming`, `quarantine`, and
`retained` around its no-replace rename. An interrupted claim never adopts a
destination from its deterministic name or matching content. No supported
portable primitive can unlink the verified inode rather than a mutable
pathname, so cleanup terminally retains the small private tree without unlink,
chmod, or retry. The ledger recovers interrupted staged, claimed,
cleanup-claimed, and activated generations;
temporary and prior directories remain scoped to this exact target.

An existing target without the matching external ledger is unmanaged and is
preserved, even if it contains a copied or fabricated marker. Content or
permission changes are user modifications and are also preserved. Source and
external trees reject symbolic links, junctions, reparse points, and
non-regular or multi-link entries. Cleanup changes no file or directory mode.
The copy model uses ordinary directories and files, so the
ownership contract does not require symlink support on Windows. Per-client
path, lock, and rename failures are labelled warnings and do not abort core
converge; source-integrity failures remain fatal.

This projection is not an Ava skill source: it writes no install-registry row,
does not enter `~/.ava/skills/`, and does not make `.agents/skills/` a
fleet-wide runtime source again.

## Key dependencies

- [[okf/skills/project-local.ava.okf.md]] — the normal visibility path for the repo skill family
- [[okf/skills/load-directory-sync.ava.okf.md]] — Ava's distinct runtime load-directory contract
- `cli/commands/_converge.py` — prod/default-home and worktree gate
