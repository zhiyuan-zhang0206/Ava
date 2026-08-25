---
name: worktree
description: Creates correctly named git worktrees for isolated coding changes and explains the narrow exceptions. Use before editing any repository, even for a one-file fix, unless the work is genuinely read-only.
---

# Ava Code — worktrees

The rule to work in a worktree — never edit, switch the branch of, or push the
shared checkout; every change goes through a PR — is in your coding-tools
section, which points here for the naming and how to create one. The rationale:

**Why:** parallel tasks sharing one source tree overwrite each other's changes into a messy diff; a worktree gives each task an independent workspace. Naming it with your agent id means two agents handed the same task land in *different* worktrees instead of silently clobbering each other, and an observer can map a worktree back to its owner.

**When not to use:** read-only code analysis, single-agent sequential changes.

**Creating a worktree:**

```bash
git worktree add -b ava-<id>-<task> .worktrees/ava-<id>-<task> main
```

This creates a branch `ava-<id>-<task>` from `main` and checks it out as a
worktree in one step. `git worktree add <path> main` fails when `main` is
already the primary checkout — use `-b` or the two-step alternative:
`git branch ava-<id>-<task> main` then `git worktree add .worktrees/ava-<id>-<task> ava-<id>-<task>`.

Run it from the repo root: the path resolves against your current logical directory,
so running it from inside another worktree silently nests the new worktree
there instead of placing it under the repo root's `.worktrees/`.

**After the PR merges:** `git worktree remove <path>` — a stranded worktree
keeps a full checkout on disk for nothing (the branch is deleted by the merge).
