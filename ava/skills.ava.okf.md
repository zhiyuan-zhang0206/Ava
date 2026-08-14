---
type: doc
title: Skill System
description: Ava's skill system — how skills are organized, loaded, and used by agents. All built into the core repo (origin=repo).
tags:
- core
- agent-instruction
---

# Skill System

Skills are reusable instruction packs that agents load at runtime. They are
organized into functional groups under the `ava_builtins/skills/` directory:

- [[ava_builtins/skills/comms/comms.ava.okf.md|Communication & User Interaction]] — UI pages, IM Bridge, SMS, Gmail
- [[ava_builtins/skills/ops_lifecycle/ops_lifecycle.ava.okf.md|Ops, Scheduling & Lifecycle]] — guide, watcher, schedule writer, long-running agent
- [[ava_builtins/skills/orchestration/orchestration.ava.okf.md|Orchestration & Workflow]] — fleet, goal, workflow phases
- [[ava_builtins/skills/self_improvement/self_improvement.ava.okf.md|Self-Improvement]] — self-development, self-evolution, skill creator, sweeper
- [[ava_builtins/skills/web_media/web_media.ava.okf.md|Web & Media]] — web AI, web sources, audio transcription

## How Skills Work
Skills are loaded on-demand by agents via `ava.help(ava.skills.<name>)`.
Each skill is a Markdown file with frontmatter describing when to use it.
Skills can be nested — displayed as `ava.skills.ava-code:pr` (loadable as `ava.skills.ava_code.pr`).


## See Also
- [[okf/plugins.ava.okf.md|Plugin System]] — skills vs plugins distinction

# ava.skills — Skill Registry

## What it is

`ava.skills` is a registry of "skill packages" that agents can load, organized as a **namespace tree mirroring the folder structure**. Each SKILL.md is a leaf node, with its parent folders forming its namespace path. Skills are not code; they are instruction text that agents actively read and follow.

## Namespace Tree

Node three states (like Python packages):
- **Leaf skill**: Only SKILL.md, no children with skills → pure leaf. `ava.skills.ava_goal`.
- **root skill**: Has both SKILL.md and children with skills → both callable and a namespace. `ava.help` renders its own SKILL.md then lists children.
- **Pure namespace**: Only INDEX.md (or neither) → just a labeled layer with a description. `ava.skills.coding.tdd`, `ava.skills.superpowers.brainstorming`.

## Naming: dash outside, underscore inside

**Dash is canonical** at every human-facing surface — the on-disk directory
name, SKILL.md frontmatter `name:`, CLI arguments, gateway API fields, the
`skills_to_inject_into_system_prompt` / `skills_to_expand_at_start` config
lists, the frontend, `identifier()`. That is the Agent Skills / Claude Code
ecosystem spelling: a skill directory authored here is a skill directory
anywhere else, and one published elsewhere drops in unmodified.

**Underscore is the Python projection and nothing else.** `-` is not an
identifier character, so the CodeAct namespace reaches
`write-a-pr-description/` as `ava.skills.write_a_pr_description`. `target()`
renders that projection; `.` separates namespace segments there, while the
display `identifier()` joins the same segments with `:`
(`ava.skills.web-ai:deep-research`).

`shared/skill_names.py` is the single fold between them — `match_key` inbound
(dash→underscore, and `:`→`.` so an ecosystem-style `ava-code:pr` resolves),
`display_name` outbound, `find` where a name has to become a real directory or
registry key. Every dash/underscore comparison in the system routes through it,
which is why a legacy underscore directory (a hand-installed
`~/.ava/skills/wechat_ocr/`) still loads, still answers to
`ava.skills.wechat_ocr`, and still displays as `wechat-ocr`. Two directories
that fold together are refused outright (`SkillNameCollision`) rather than one
silently winning.

**Plugin directories are the deliberate exception: they stay underscore on
disk** (`ava_builtins/plugins/ava_code/`), because they are real Python
packages — `__init__.py`, `from . import _code_namespace`, and `python -m
ava_builtins.plugins.ava_fleet.task_maintenance.daemon` all require a legal
module path. Name ≠ directory here: `identifier()` folds the namespace segment
so the surface still reads `ava-code:pr` and `ava-fleet`, and the skills a
plugin carries are dash-named like any other.

## Core API

- `ava.skills.<path>` — Access a skill or namespace (module `__getattr__` lazy resolution). Resolution is exposure, not use — it records nothing. The `skill_invoked` event at depth `loaded` (the only depth ava_self_evolution scores as real use) fires on first SKILL.md body consumption: `ava.help(ava.skills.<path>)` or a direct `__doc__` read loads the body lazily and attributes it there. All attribution rows go through one writer, `_insert_skill_events`, which batches a whole list onto a single connection — prompt injection now covers the entire catalog, and a connect-per-skill would sit on the pre-first-turn critical path.
- `ava.help(ava.skills.<path>)` — Load full text (path line + SKILL.md body).
- `ava.help(ava.skills)` / `dir(ava.skills)` — **Index only**: one `ava.skills.<path>` heading + one-line description per entry, never a body. The child walk reads frontmatter `_description`s and never touches `__doc__`, and node resolution records no `loaded` attribution — listing the catalog is silent. The agent-visible surface is the `__all_for_ava__` property on `_SkillsModule` (the module's own class) — `agent_visible_names` reads it with `getattr_static`, which a PEP 562 module `__getattr__` cannot serve.
- `register_skill_source(provider)` — Plugin extension point: contribute skill roots resolved during scanning (project-local, scanned last can override same-named built-ins).

## Loading Sources

`~/.ava/skills/` is the sole load directory, synced by the converge step (`ava start` / `ava update` / `ava converge`) from repo skills (`<repo>/ava_builtins/skills/*`) and plugin skills (`<repo>/ava_builtins/plugins/<p>/skills/*`) (plugin skills land in `~/.ava/skills/<p>/…`, namespace layer is the folder layer). Top-level directories must be enabled in the install-registry to be loaded (`ava skill register <name>`), otherwise silently ignored. Duplicate SKILL.md content across paths is de-duplicated by SHA256.

## Key Dependencies
- [[system-prompt.ava.okf.md]] — the skill index is injected into the system prompt by the **agent process itself** (`agent/graph/_capabilities.py:_skill_index_lines` scans local `~/.ava/skills`; `_system_prompt.py` imports the rendered sections from `_capabilities`), once, into `# Capabilities` — `AVA_SDK_EXPAND`'s `*` skips `ava.skills` so the expanded SDK reference does not render a second index; gateway only reuses `ava._commands.discover_commands` for `/`-autocomplete, does not inject prompt

## Notes
Skills are essentially instruction text that agents actively load and read, not passively injected plugins (plugins are that). Which skills are available is determined at runtime/config (converge + install-registry), not enumerated in `ava/` source. Malformed SKILL.md are skipped during scanning with a loud warning, without dragging down the entire fleet.
