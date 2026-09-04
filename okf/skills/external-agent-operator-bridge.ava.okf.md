---
type: doc
title: External-agent operator skill bridge
description: Prod host convergence copies only operating-ava-cluster into already-present Codex and Claude Code global skill roots, with marker-and-digest ownership that preserves conflicts.
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
beside that target and records `.ava-managed.json` with the skill identity and a
digest over relative paths, entry kinds, and file bytes. A matching unmodified
copy is idempotent; a source change replaces it through a staged rename with
rollback to the prior verified copy if activation fails. Temporary and prior
directories are scoped to this exact target.

An existing target without the valid marker is unmanaged and is preserved. If
the target's current digest differs from the marker's recorded digest, it is a
user-modified managed copy and is also preserved. Both states print a conflict
warning. The copy model uses ordinary directories and files rather than
symlinks, so the ownership contract is the same on POSIX and Windows.

This projection is not an Ava skill source: it writes no install-registry row,
does not enter `~/.ava/skills/`, and does not make `.agents/skills/` a
fleet-wide runtime source again.

## Key dependencies

- [[okf/skills/project-local.ava.okf.md]] — the normal visibility path for the repo skill family
- [[okf/skills/load-directory-sync.ava.okf.md]] — Ava's distinct runtime load-directory contract
- `cli/commands/_converge.py` — prod/default-home and worktree gate
