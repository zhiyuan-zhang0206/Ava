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
beside that target. A private per-client ledger under
`$AVA_HOME/configs/external-agent-skills/` records an installation identity,
installed generation and digest, active transaction, and exact cleanup records;
the target's `.ava-managed.json` marker must match that external authority. The
digest covers relative paths, entry kinds, file bytes, and file and directory
modes. Each transaction, installed generation, and cleanup record also carries
the expected path manifest. Moving a residue from transaction state to cleanup
state is one atomic ledger write, so no live stage or prior copy loses its
durable pointer.

A per-target cross-process lock serializes convergence. A source change stages
a complete no-follow copy, atomically claims the current target, and verifies
the claimed directory before activation. A mismatch is restored without
overwriting a path that appeared late. Activation is recorded before cleanup,
so a cleanup failure leaves the new copy authoritative and is retried later.
Cleanup validates the remaining tree as an unmodified subset of the manifest;
paths removed by an earlier attempt stay safely reclaimable, while unexpected
or modified paths are preserved. The ledger recovers interrupted staged,
claimed, and activated generations;
temporary and prior directories remain scoped to this exact target.

An existing target without the matching external ledger is unmanaged and is
preserved, even if it contains a copied or fabricated marker. Content or
permission changes are user modifications and are also preserved. Source and
external trees reject symbolic links, junctions, reparse points, and
non-regular entries. The copy model uses ordinary directories and files, so the
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
