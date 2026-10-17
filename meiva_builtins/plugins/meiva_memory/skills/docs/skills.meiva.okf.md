---
type: doc
title: Meiva Memory Skills — shared memory and assigned maintenance roles
description: Shared-memory entrypoint carried by meiva_memory, with on-demand user-dimension guidance, an assigned Memory Arbiter role guide, and consolidation procedures selected by deployment and role.
tags:
- extensions
- agent-instruction
---

# Meiva Memory Skills

The plugin's skill root directly contains SKILL.md, loaded as
meiva.skills.meiva_memory. It routes ordinary queries, user-preference maintenance,
and consolidation without assigning a maintenance identity to the reader.

## Task and role selection

- Queries use the available shared-memory search/read capabilities; note ownership
  is context, not an instruction to adopt the author's role.
- User-preference writes read reference/user-dimension.md and maintain existing
  standing notes rather than accumulating duplicate records.
- Only an assigned Memory Arbiter reads reference/arbiter-role.md for health
  checks, curation, schedules, and collaboration duties.
- [[meiva_builtins/plugins/meiva_memory/skills/consolidation/docs/consolidation.meiva.okf.md|consolidation]]
  selects single-box, multi-host arbiter, or per-machine steward procedures.
  Each substantial procedure lives in a reference read for that deployment
  and assigned role. Local-only work does not acquire multi-host responsibilities.

The Arbiter owns coherence of standing user-profile and preference notes during
assigned consolidation. Reading this skill does not authorize new schedules,
messages, merges, or unrelated maintenance.

## Distribution and dependencies

The root is itself a skill, with consolidation beneath it. Its package is
distributed by the plugin's skill-root provider, preserving the existing
meiva-memory identity and Python projection meiva_memory.

- [[meiva/skills/docs/skills.meiva.okf.md|Skill System]] — plugin-origin skill loading
- [[meiva_builtins/skills/docs/skills.meiva.okf.md|Skills index]] — built-in catalog
- [[meiva_builtins/plugins/meiva_memory/docs/meiva_memory.meiva.okf.md|meiva_memory plugin]] — runtime hooks
