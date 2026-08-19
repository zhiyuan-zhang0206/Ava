---
type: doc
title: Skill sources — load-directory sync
description: One load directory ~/.ava/skills/; converge syncs three source types into it — repo built-ins (ava_builtins/skills/ plus the repo's .agents/skills project skills), plugin-carried skills, and user installs (untouched by converge).
tags:
- extensions
- agent-instruction
---

# Skill sources (load-directory sync)

One load directory: `~/.ava/skills/` (gated by the install registry's enabled
flag). Converge (`cli/commands/_converge_skills.py`, on `ava start` /
`ava cluster update` / `ava converge`) syncs three source types into it:

1. **Repo built-in** (origin=repo): `<repo>/ava_builtins/skills/` →
   `~/.ava/skills/<name>/`, plus the **non-symlink entries of
   `<repo>/.agents/skills/`** (the R5 design, task #1013). The latter means
   kernel-contributor project skills (ship-a-change, ava-self-development, …)
   also land fleet-wide, not only when working inside the repo — whether they
   should is an open point recorded in
   `decisions/2026-08-19-four-layer-modification-model.md` (family placement
   is taxonomy, not distribution). Repo-native sources are bootstrap-only:
   converge lands a missing copy and never updates one; the explicit update is
   `ava skill update`.
2. **Plugin-carried** (origin=plugin): `<repo>/ava_builtins/plugins/<p>/skills/`
   and `~/.ava/plugins/<p>/skills/` → `~/.ava/skills/<p>/`.
3. **User-installed** (origin=user): `ava skill install` drops directly into
   `~/.ava/skills/` (untouched by converge); a hand-placed dir needs
   `ava skill register`.

Project-local mounts (the working repo's `.agents/skills` etc., resolved from
`ava.cwd` at scan time) are a separate mechanism:
[[okf/skills/project-local.ava.okf.md]].

## Key Dependencies
- [[okf/skills/skills.ava.okf.md|Skill System]] — the overview this node details
- `shared/install_registry.py` — per-machine installation/origin/enabled registry
