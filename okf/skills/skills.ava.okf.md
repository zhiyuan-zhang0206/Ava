---
type: doc
title: Skill System
description: Skills — Ava's lightest-weight extension — pure markdown instruction packages, lazy-loaded on demand; no runtime state, no hooks. Flat skill directories, catalogued by what each does, with a core vs instance origin axis.
tags:
- extensions
- tool
- agent-instruction
---

# Skill System

## What It Is
Skills are Ava's lightest-weight extension mechanism — pure markdown instruction packages, lazy-loaded by agents on demand. Each skill is a directory containing a `SKILL.md`: YAML frontmatter declares metadata, the body is an operation guide. Unlike plugins, skills have no runtime state, cannot modify agent behavior, and inject no hooks — simply "manuals consulted when needed".

## Core Responsibilities
- **Instruction dispatch**: `ava.help(ava.skills.<path>)` loads a skill body by path; nested paths like `ava.skills.web_ai.deep_research`. Directories and every displayed name are dash-separated (ecosystem-standard), and the display identifier joins namespace segments with `:` (`ava.skills.web-ai:deep-research`); underscore is only the Python projection, folded by `base/packages/skills/names.py` — see [[ava/skills/docs/skills.ava.okf.md|Skill System]] "Naming".
- **Discovery (index)**: `settings.agent.skills_to_inject_into_system_prompt` defaults to `'*'` — **every** loaded skill (nested paths included) gets **name + one-line description** in the system prompt's `# Capabilities` section, which is the prompt's ONE skill index (`AVA_SDK_EXPAND`'s `*` deliberately skips `ava.skills` / `ava.mcps` so the expanded SDK reference does not render a second one). An explicit list NARROWS a single agent's index below the catalog — it hides entries from the index only; `ava.help(ava.skills)` still lists the whole catalog and any skill stays reachable by name. Consulting the index is the first step of the delegation check — the prompt's only mandatory-flagged process — because an agent that has not read it cannot know it is rebuilding a skill it already has. **Full body is never injected** — pulled on demand via `ava.help`. The index is rendered into the SystemMessage, so it is a **snapshot** built once per context window, while the catalog under it is an uncached filesystem scan: `state.capabilities.indexed` records what the snapshot covered and a framework `before_llm` hook names anything installed since, so a mid-session install does not stay invisible until the next compact — see [[agent/graph/docs/system-prompt.ava.okf.md|System Prompt]].
- **Preload (full text)**: `settings.agent.skills_to_expand_at_start` (#776, default empty) preloads entire SKILL.md text as a system note (same carrier as the memory index) at session start and after every compact — for short discipline skills that must take effect from spawn and survive compact (for example, a role-specific instruction pack); large reference skills stay index-only. Both lists are `per_agent` overridable via spawn/restart config_overlay, and both are `lifecycle: "frozen"` — an agent's skill set is resolved once at its spawn and replayed for its life, so changing the cluster default arms agents born after it and never re-arms one already running (which compact would otherwise do mid-task). See the `base/config` module docstring.

## Origin Axis: Core Built-in vs Instance Side
Each skill in the install registry (`base/packages/extensions/install_registry.py`) carries an `origin` (`PackageOrigin`) and, alongside it, a **trust tier** (`builtin` / `reviewed` / `unreviewed`, raised via `ava skill trust`) plus a `content_hash` for drift detection (a user-edited converge copy is neither overwritten nor auto-deleted):
- **core built-in** (`origin=repo` / `plugin`): distributed with Ava code — `<repo>/ava_builtins/skills/` or plugin-carried — synced into the load directory by converge. The skills catalog below is all this type (exception: `ava_memory`, plugin-carried); plugin-carried skill nodes hang under their plugin subtrees — [[ava_builtins/plugins/ava_fleet/docs/skills/skills.ava.okf.md|ava_fleet]] / [[ava_builtins/plugins/ava_memory/skills/docs/skills.ava.okf.md|ava_memory]].
- **instance / marketplace side** (`origin=user`): installed from marketplace / git URL (`source` records origin), or carried by an operator's own private repo; converge does not touch; **not enumerated in this core tree** — an instance-only skill has no node here by construction.

## Core Built-in Skills
Every core built-in skill is a self-contained directory under `ava_builtins/skills/<group>/<name>/` — code and docs live together, and the four source-tree groups (`practice`, `coordination`, `platform`, `integrations`) only keep the parent directory under its entry budget: converge lands each skill at `skills/<name>/`, so a group never reaches a skill's identity. The full catalog, grouped by what each does (communication, ops/scheduling/lifecycle, orchestration, self-improvement, web/media) with one line + a link per skill: [[ava_builtins/skills/docs/skills.ava.okf.md|Skills index]] (telegram skill removed 2026-08-03; `ava_memory` is plugin-carried, listed under its own plugin subtree instead).

Ava Guide is the shared use/deploy/operate/extend package for humans and
internal/external agents. Its sub-skills own modification layers, package
installation, native plugin development, schedules, and external-agent execution.
Capability/timescale calibration is a sub-skill of `ava-workflow`.

## Skill Sources (Load Directory Sync)
One load directory: `~/.ava/skills/`; converge syncs repo built-ins (`ava_builtins/skills/`) and plugin-carried skills into it; user installs land directly: [[okf/skills/load-directory-sync.ava.okf.md]]. The repo's `.agents/skills/` project skills are NOT converged — project-local mount only: [[okf/skills/project-local.ava.okf.md]].

## Skill Structure
`SKILL.md` = frontmatter (`name` + `description`) + a markdown body; `description` is the capabilities line in the system prompt. This format **is** the [Agent Skills](https://agentskills.io) open standard — a Claude Code skill folder installs unmodified: [[okf/skills/agent-skills-standard.ava.okf.md]].

## Key Dependencies
- [[system-prompt.ava.okf.md]] — the capabilities section that indexes them (whole catalog by default)
- Discovery/merging in `ava/skills/__init__.py` (`_scan_tree` / `_mount` / `_flatten`); `ava/sdk_surface/skill_sources.py` is just a registry for plugin skill-root providers (register/clear/roots), **unrelated to agents-contract**

## Entry Points
- `ava/skills/__init__.py` — scans `~/.ava/skills/` + provider roots, merges the skill tree; `help()` loads SKILL.md by path
- `base/packages/extensions/install_registry.py` — per-machine installation/origin/enabled registry
- `cli/commands/extensions/skills_sync.py` — syncs repo/plugin skills into the load directory
- `ava/__init__.py` — integrates skill docs in `help()`

## Notes
- Skills are the most suitable extension point for user customization — just create a markdown directory; no version management, no dependency resolution — pure filesystem convention
- Unlike plugins, skills do not participate in agent process startup; loaded only when the agent calls `help()`
- `ava-guide`, `ava-workflow`, `web-ai`, and `web-sources` contain nested sub-skills. Their root OKF nodes index the hierarchy. Larger sub-skills with bundled execution resources may keep a local detail node; a node per sub-skill is not required. Plugin-carried skills keep their own nodes.
