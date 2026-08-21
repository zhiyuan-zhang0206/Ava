---
type: doc
title: Skill sources — load-directory sync
description: One load directory ~/.ava/skills/; converge syncs repo built-ins (ava_builtins/skills/) and plugin-carried skills into it, and user installs land directly (untouched). The repo's .agents/skills project skills are NOT converged — they reach agents through the project-local mount.
tags:
- extensions
- agent-instruction
---

# Skill sources (load-directory sync)

One load directory: `~/.ava/skills/` (gated by the install registry's enabled
flag). Converge (`cli/commands/_converge_skills.py`, on `ava start` /
`ava cluster update` / `ava converge`) syncs two source types into it:

1. **Repo built-in** (origin=repo): `<repo>/ava_builtins/skills/` →
   `~/.ava/skills/<name>/`. Repo-native sources are bootstrap-only:
   converge lands a missing copy and never updates one; the explicit update is
   `ava skill update`.
2. **Plugin-carried** (origin=plugin): `<repo>/ava_builtins/plugins/<p>/skills/`
   and `~/.ava/plugins/<p>/skills/` → `~/.ava/skills/<p>/`.

User-installed packages (origin=user): `ava skill install` drops directly into
`~/.ava/skills/` (untouched by converge); a hand-placed dir needs
`ava skill register`.

**Not converged — the repo's `.agents/skills/` project skills.** The
kernel-contributor family (ship-a-change, write-a-pr-description,
ava-self-development, …) stopped being fleet-distributed (issue #146;
`decisions/2026-08-20-stop-fleet-distributing-kernel-contributor-skills.md`,
resolving the open point in
`decisions/2026-08-19-four-layer-modification-model.md`). A converge pass
treats them as gone sources: untouched copies they used to land are removed
and deregistered, so runtime agents' indexes lose the L4 noise. They reach
agents only through the project-local mount — see
[[okf/skills/project-local.ava.okf.md]].
