---
type: doc
title: skill-creator skill — Create/modify/review skill
description: Methodology and workflow for creating, improving, and reviewing Ava skills. Derived from Claude Code's Skill Creator, adapted to Ava's architecture and tool ecosystem — three-level loading model, when to create a skill vs stuffing config, directory structure conventions.
tags:
- extensions
- agent-instruction
---

# skill-creator skill — Create/modify/review skill

## What it is
A **methodology** for creating high-quality Ava skills (`$AVA_HOME/skills/skill-creator/`), derived from Claude Code's Skill Creator, adapted to Ava's architecture and tool ecosystem. It exists because skill is the most suitable extension point for users/agents to customize, but "writing a good skill" has tricks: who is it for, how much to put in, when to create a new skill instead of stuffing instructions elsewhere.

## Judgments carried
- **Three-level loading model**: metadata (name+description, always in context) / SKILL.md body (loaded when triggered) / bundled resources (scripts/references/assets, on demand). When writing a skill, distribute content across these three levels — description should let the agent determine "when to reach for it."
- **When to create a skill**: when an instruction has no existing config field to carry it, and will be used repeatedly, creating a small skill is cleaner and more reusable than piling into config (same origin as [[ava/presets.ava.okf.md|presets]]'s "no carrying field, just create a small skill").
- **skill vs plugin**: skill is a pure markdown instruction package, no runtime state; to modify agent behavior / inject hooks, use plugin.

## Key dependencies
- [[ava_builtins/skills/self_improvement/self_improvement.ava.okf.md|Self-improvement & evolution skill]] — belongs to functional group
- [[ava/skills.ava.okf.md|Skill System]] — created things land in this mechanism
- [[plugins.ava.okf.md|Plugin system]] — boundary between skill and plugin
