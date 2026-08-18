---
type: doc
title: Project-local skills
description: Repo-local skills mount at scan time from the working repo's .claude/skills / .agents/skills / .ava/skills — the open Agent Skills standard directory plus compatibility links.
tags:
- extensions
- compatibility
- agent-instruction
---

# Project-local skills

Beyond the `~/.ava/skills/` load dir, **project-local skills** mount at scan time via provider roots (`ava/skills.py:register_skill_source`, registered by the `ava_code` plugin): the working repo's `.claude/skills` (Claude Code compatibility), `.agents/skills` (the open Agent Skills standard directory) and `.ava/skills` (Ava-native, scanned last so it wins), resolved from `ava.cwd` — see `ava_builtins/plugins/ava_code/_walk.py:project_skill_roots`.

The standard directory is the open surface: a repo keeps its skills under `.agents/skills/` and links `.ava/skills` / `.claude/skills` back to it, so Ava, Claude Code and any Agent-Skills-standard client load the same set. The same content reached through several paths is deduped by content hash (`ava/skills.py:_scan_tree`).

## Key Dependencies
- [[okf/skills/skills.ava.okf.md|Skill System]] — the mount/scan machinery this node extends
- `ava_builtins/plugins/ava_code/_walk.py` — `project_skill_roots`, the three candidate paths
