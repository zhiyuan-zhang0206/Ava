---
type: doc
title: Skill System
description: Meiva's skill system — how skills are organized, loaded, and used by agents. All built into the core repo (origin=repo).
tags:
- core
- agent-instruction
---

# Skill System

Skills are reusable instruction packs that agents load at runtime. Each repo
skill is a self-contained directory under `meiva_builtins/skills/<group>/<name>/`
(`SKILL.md` + optional `scripts/` / `references/` / `assets/`); the full
catalog, grouped by what each skill does, is
[[meiva_builtins/skills/docs/skills.meiva.okf.md|the skills index]].

## How Skills Work
Skills are loaded on-demand by agents via `meiva.help(meiva.skills.<name>)`.
Each skill is a Markdown file with frontmatter describing when to use it.
Skills can be nested; every agent-visible spelling (index lines, `meiva.help` headings, preloaded-skill sections) is the loadable path, e.g. `meiva.skills.meiva_memory.consolidation`.


## See Also
- [[docs/plugins/plugins.meiva.okf.md|Plugin System]] — skills vs plugins distinction

# meiva.skills — Skill Registry

## What it is

`meiva.skills` is a registry of "skill packages" that agents can load, organized as a **namespace tree mirroring the folder structure**. Each SKILL.md is a leaf node, with its parent folders forming its namespace path. Skills are not code; they are instruction text that agents actively read and follow.

## Namespace Tree

Node three states (like Python packages):
- **Leaf skill**: Only SKILL.md, no children with skills → pure leaf. `meiva.skills.meiva_goal`.
- **root skill**: Has both SKILL.md and children with skills → both callable and a namespace. `meiva.help` renders its own SKILL.md then lists children.
- **Pure namespace**: Only INDEX.md (or neither) → just a labeled layer with a description. `meiva.skills.coding.tdd`, `meiva.skills.superpowers.brainstorming`.

## Naming: dash outside, underscore inside

**Dash is canonical** at every human-facing surface — the on-disk directory
name, SKILL.md frontmatter `name:`, CLI arguments, gateway API fields, the
`skills_to_inject_into_system_prompt` / `skills_to_expand_at_start` config
lists, the frontend, `identifier()`. That is the Agent Skills / Claude Code
ecosystem spelling: a skill directory authored here is a skill directory
anywhere else, and one published elsewhere drops in unmodified.

**Underscore is the Python projection and nothing else.** `-` is not an
identifier character, so the CodeAct namespace reaches
`inspect-a-trace/` as `meiva.skills.inspect_a_trace`. `target()`
renders that projection; `.` separates namespace segments there, while the
display `identifier()` (composer commands, CLI) joins the same segments with `:`
(`web-ai:deep-research`). Agent-visible text uses the `target()` spelling only.

`base/packages/skills/names.py` is the single fold between them — `match_key` inbound
(dash→underscore, and `:`→`.` so an ecosystem-style `meiva-memory:consolidation` resolves),
`display_name` outbound, `find` where a name has to become a real directory or
registry key. Every dash/underscore comparison in the system routes through it,
which is why a legacy underscore directory (a hand-installed
`~/.meiva/skills/page_ocr/`) still loads, still answers to
`meiva.skills.page_ocr`, and still displays as `page-ocr`. Two directories
that fold together are refused outright (`SkillNameCollision`) rather than one
silently winning.

**Plugin directories are the deliberate exception: they stay underscore on
disk** (`meiva_builtins/plugins/meiva_code/`), because they are real Python
packages — `__init__.py`, `from . import _code_namespace`, and `python -m
meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon` all require a legal
module path. Name ≠ directory here: `identifier()` folds the namespace segment
so the surface still reads `meiva-memory:consolidation` and `meiva-fleet`, and the skills a
plugin carries are dash-named like any other.

## Core API

- `meiva.skills.<path>` — Access a skill or namespace (module `__getattr__` lazy resolution). Resolution is exposure, not use — it records nothing. The `skill_invoked` event at depth `loaded` (the only depth meiva_self_evolution scores as real use) fires on SKILL.md body consumption: `meiva.help(meiva.skills.<path>)` or a direct `__doc__` read loads the body lazily and attributes it there. Each body load writes its event on the spot through `_record_skill_invoked` (an `audit_events` row, then the unified stream); the SDK keeps no in-process buffer or dedup set, and a failed write logs an error with traceback and emits `audit_write_failed` without failing the read. The same point notes the skill's identifier in the exec turn's state update (`meiva.state_update["skill_reads"]`, plain strings, once per skill per exec) so skill recall knows the body is already in the window; outside an exec turn nothing is noted.
- `meiva.help(meiva.skills.<path>)` — Load full text (path line + SKILL.md body).
- `meiva.help(meiva.skills)` / `dir(meiva.skills)` — **Index only**: one `meiva.skills.<path>` heading + one-line description per entry, never a body. The child walk reads frontmatter `_description`s and never touches `__doc__`, and node resolution records no `loaded` attribution — listing the catalog is silent. The agent-visible surface is the `__all_for_meiva__` property on `_SkillsModule` (the module's own class) — `agent_visible_names` reads it with `getattr_static`, which a PEP 562 module `__getattr__` cannot serve.
- Plugin skill sources: a plugin declares `skill_sources` (`provider() -> list[Path]`) in `contribute()`; the SDK install adds them and the scan reads them (project-local, scanned last, can override same-named built-ins).

## Loading Sources

`~/.meiva/skills/` is the sole load directory, synced by the converge step (`meiva start` / `meiva update` / `meiva converge`) from repo skills (`<repo>/meiva_builtins/skills/*`) and plugin skills (`<repo>/meiva_builtins/plugins/<p>/skills/*`) (plugin skills land in `~/.meiva/skills/<p>/…`, namespace layer is the folder layer). Top-level directories must be enabled in the install-registry (`meiva skill register <name>`) or are ignored. Duplicate SKILL.md files across paths are deduplicated by full-file SHA256 (including frontmatter).

A mount root's `INDEX.md` is ignored; its `SKILL.md` is retained. Auto-promotion
of a same-named child preserves the old child access path but omits its duplicate
flat-list entry. An existing root skill is not replaced.

Body caching belongs to each proxy: rereading a saved proxy's `__doc__` does not
load or attribute again. A fresh `meiva.skills.<path>` resolution creates a fresh
proxy. Direct skill-file reads, including partial reads, attribute each read.
`meiva/tests/skills/test_skills_attribution.py` checks these boundaries; its failed
audit-write case checks the returned body and logged traceback, not live telemetry
delivery. Mount fixtures in `test_skills_mount.py` compare raw file content.

## Key Dependencies
- [[system-prompt.meiva.okf.md]] — the skill index is injected into the system prompt by the **agent process itself** (`agent/graph/prompt/capabilities.py:_skill_index_lines` scans local `~/.meiva/skills`; `system_prompt.py` imports the rendered sections from `capabilities`), once, into `# Capabilities` (wildcard catalogs fold descendants under real entry skills; explicit lists stay verbatim) — `MEIVA_SDK_EXPAND`'s `*` skips `meiva.skills` so the expanded SDK reference does not render a second index; gateway only reuses `base.packages.composer_commands.CommandCatalog` (over `meiva.skills.names`) for `/`-autocomplete, does not inject prompt

## Notes
Skills are essentially instruction text that agents actively load and read, not passively injected plugins (plugins are that). Which skills are available is determined at runtime/config (converge + install-registry), not enumerated in `meiva/` source. Malformed SKILL.md are skipped during scanning with a loud warning, without dragging down the entire fleet.
