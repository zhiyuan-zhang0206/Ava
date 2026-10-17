---
type: doc
title: Project-local skills
description: Repo-local skills mount at scan time from the working repo's .claude/skills / .agents/skills / .meiva/skills — the open Agent Skills standard directory, built-in mirrors, and compatibility links.
tags:
- extensions
- compatibility
- agent-instruction
---

# Project-local skills

Beyond the `~/.meiva/skills/` load dir, **project-local skills** mount at scan time via provider roots (`skill_sources` declared by the `meiva_code` plugin): the working repo's `.claude/skills` (Claude Code compatibility), `.agents/skills` (the open Agent Skills standard directory) and `.meiva/skills` (Meiva-native, scanned last so it wins), resolved from `meiva.cwd` — see `base/packages/skills/project_roots.py:project_skill_roots`.

The standard directory is the open surface: a repo keeps its skills under `.agents/skills/` and links `.meiva/skills` / `.claude/skills` back to it, so Meiva, Claude Code and any Agent-Skills-standard client load the same set. The same content reached through several paths is deduped by content hash (`meiva/skills/__init__.py:_scan_tree`).

For Meiva's own repo, this mount is the **only** distribution path for the
real repo-development workflow and Meiva-cluster-operations skills since
2026-08-21 (issue #146): converge stopped syncing `.agents/skills/`
fleet-wide, so those skills load exactly when the agent works inside the
checkout (`meiva.cwd` under the repo) and never enter Meiva's fleet-wide runtime
skill load directory. One deliberately narrower host integration copies the complete
`meiva-guide` package into already-present Codex and Claude Code global skill
roots; it is an external-client projection, not an Meiva runtime skill source —
see [[docs/skills/external-agent-operator-bridge.meiva.okf.md]].
The five general skills mirrored alongside them — `meiva-serious-engineering`,
`meiva-serious-research`, `meiva-deep-research`, `meiva-corp`, and
`telegram-send-file` — are instead built-ins converged into `~/.meiva/skills/`;
the mirror preserves the open-standard path while content-hash dedup keeps the
same skill from loading twice in a checkout.

Contributor entry: [docs/contributing.md](../../docs/contributing.md).
The repo-specific `review-contribution` skill routes a contributor self-check to
current convention and component owners; it does not require a reviewer agent.

## Key Dependencies
- [[docs/skills/skills.meiva.okf.md|Skill System]] — the mount/scan machinery this node extends
- `meiva_builtins/plugins/meiva_code/_walk.py` — `project_skill_roots`, the three candidate paths
