---
type: doc
title: Skill System
description: Skills — Ava's lightest-weight extension — pure markdown instruction packages, lazy-loaded on demand; no runtime state, no hooks. Tree divided into 5 functional groups with a core vs instance origin axis.
tags:
- extensions
- tool
- agent-instruction
---

# Skill System

## What It Is
Skills are Ava's lightest-weight extension mechanism — pure markdown instruction packages, lazy-loaded by agents on demand. Each skill is a directory containing a `SKILL.md`: YAML frontmatter declares metadata, the body is an operation guide. Unlike plugins, skills have no runtime state, cannot modify agent behavior, and inject no hooks — simply "manuals consulted when needed".

## Core Responsibilities
- **Instruction dispatch**: `ava.help(ava.skills.<path>)` loads a skill body by path; nested paths like `ava.skills.web_ai.deep_research`. Directories and every displayed name are dash-separated (ecosystem-standard), and the display identifier joins namespace segments with `:` (`ava.skills.web-ai:deep-research`); underscore is only the Python projection, folded by `shared/skill_names.py` — see [[ava/skills.ava.okf.md|Skill System]] "Naming".
- **Discovery (index)**: `settings.agent.skills_to_inject_into_system_prompt` defaults to `'*'` — **every** loaded skill (nested paths included) gets **name + one-line description** in the system prompt's `# Capabilities` section, which is the prompt's ONE skill index (`AVA_SDK_EXPAND`'s `*` deliberately skips `ava.skills` / `ava.mcps` so the expanded SDK reference does not render a second one). An explicit list NARROWS a single agent's index below the catalog — it hides entries from the index only; `ava.help(ava.skills)` still lists the whole catalog and any skill stays reachable by name. Consulting the index is the first step of the delegation check — the prompt's only mandatory-flagged process — because an agent that has not read it cannot know it is rebuilding a skill it already has. **Full body is never injected** — pulled on demand via `ava.help`. The index is rendered into the SystemMessage, so it is a **snapshot** built once per context window, while the catalog under it is an uncached filesystem scan: `state.capabilities.indexed` records what the snapshot covered and a framework `before_llm` hook names anything installed since, so a mid-session install does not stay invisible until the next compact — see [[agent/graph/system-prompt.ava.okf.md|System Prompt]].
- **Preload (full text)**: `settings.agent.skills_to_expand_at_start` (#776, default empty) preloads entire SKILL.md text as a system note (same carrier as the memory index) at session start and after every compact — for short discipline skills that must take effect from spawn and survive compact (e.g., [[ava_builtins/skills/ops_lifecycle/ava-ultra-speed.ava.okf.md|ava-ultra-speed]]); large reference skills stay index-only. Both lists are `per_agent` overridable via spawn/restart config_overlay, and both are `lifecycle: "frozen"` — an agent's skill set is resolved once at its spawn and replayed for its life, so changing the cluster default arms agents born after it and never re-arms one already running (which compact would otherwise do mid-task). See the `shared/config` module docstring.

## Origin Axis: Core Built-in vs Instance Side
Each skill in the install registry (`shared/install_registry.py`) carries an `origin` (`PackageOrigin`) and, alongside it, a **trust tier** (`builtin` / `reviewed` / `unreviewed`, raised via `ava skill trust`) plus a `content_hash` for drift detection (a user-edited converge copy is neither overwritten nor auto-deleted):
- **core built-in** (`origin=repo` / `plugin`): distributed with Ava code — `<repo>/ava_builtins/skills/` or plugin-carried — synced into the load directory by converge. The 5 functional groups below are all this type (exception: `ava_memory` in ops_lifecycle); plugin-carried skill nodes hang under their plugin subtrees — [[ava_builtins/plugins/ava_code/skills/skills.ava.okf.md|ava_code]] / [[ava_builtins/plugins/ava_fleet/skills/skills.ava.okf.md|ava_fleet]] / [[ava_builtins/plugins/ava_memory/skills/skills.ava.okf.md|ava_memory]].
- **instance / marketplace side** (`origin=user`): installed from marketplace / git URL (`source` records origin), or carried by an operator's own private repo; converge does not touch; **not enumerated in this core tree** — an instance-only skill has no node here by construction.

## Functional Groups (core built-in skills)
- [[self_improvement.ava.okf.md|Self-development and Evolution]] — ava-self-evolution / skill-creator / sweeper / auto-review
- [[ops_lifecycle.ava.okf.md|Operations, Scheduling, and Lifecycle]] — ava-guide / ava-schedule-writer / ava-watcher / ava-being-a-long-running-agent / ava-ultra-speed / ava_memory
- [[comms.ava.okf.md|Communications and User Interaction]] — ava-ui / sms / gmail (telegram skill removed 2026-08-03)
- [[orchestration.ava.okf.md|Workflow Orchestration]] — ava-workflow / ava-dynamic-workflow / ava-goal / ava-use-claude-code-and-codex
- [[web_media.ava.okf.md|Web and Multimodal]] — web-ai / web-sources / audio-transcribe

Four further core built-in skills live at the top level without a group: **ava-package-installer** (a verified skill / plugin / MCP installer), **ava-qa-inspection** (sweep the rendered frontend over the chrome MCP for visual/structural defects), **ava-modification-layers** (pick the right layer L1–L4 before changing a deployment; `decisions/2026-08-19-four-layer-modification-model.md`) and **develop-a-plugin** (the L3 plugin ladder, applied at `self.restart`, decoupled from `ava cluster update`). All are origin=repo skills in every agent's capabilities index; none has an OKF node yet.

## Skill Sources (Load Directory Sync)
One load directory: `~/.ava/skills/`; converge syncs three source types into it — repo built-ins (`ava_builtins/skills/` plus the repo's `.agents/skills/` project skills), plugin-carried, user-installed: [[okf/skills/load-directory-sync.ava.okf.md]]. Project-local mounts: [[okf/skills/project-local.ava.okf.md]].

## Skill Structure
`SKILL.md` = frontmatter (`name` + `description`) + a markdown body; `description` is the capabilities line in the system prompt. This format **is** the [Agent Skills](https://agentskills.io) open standard — a Claude Code skill folder installs unmodified: [[okf/skills/agent-skills-standard.ava.okf.md]].

## Key Dependencies
- [[system-prompt.ava.okf.md]] — the capabilities section that indexes them (whole catalog by default)
- Discovery/merging in `ava/skills.py` (`_scan_tree` / `_mount` / `_flatten`); `ava/_skill_sources.py` is just a registry for plugin skill-root providers (register/clear/roots), **unrelated to agents-contract**

## Entry Points
- `ava/skills.py` — scans `~/.ava/skills/` + provider roots, merges the skill tree; `help()` loads SKILL.md by path
- `shared/install_registry.py` — per-machine installation/origin/enabled registry
- `cli/commands/_converge_skills.py` — syncs repo/plugin skills into the load directory
- `ava/__init__.py` — integrates skill docs in `help()`

## Notes
- Skills are the most suitable extension point for user customization — just create a markdown directory; no version management, no dependency resolution — pure filesystem convention
- Unlike plugins, skills do not participate in agent process startup; loaded only when the agent calls `help()`
- `ava-guide` / `web-ai` / `web-sources` / `ava-workflow` have nested sub-skills (agents/mcp/models/onboarding/ops/packages/presets; console/deep-research/media; per-platform adapters; align/calibrate/plan/work-eval); plugin-carried `ava_memory` has a consolidation sub-skill. **Nested sub-skills of top-level skills do not each get an OKF node** (listed under their top-level node) — only plugin-carried skills each get a node.
