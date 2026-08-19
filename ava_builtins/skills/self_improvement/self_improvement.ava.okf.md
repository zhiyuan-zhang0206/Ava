---
type: doc
title: Self-development & evolution skill
description: A set of skills for Ava to improve itself — collect real runs into traces to mine regressions, create/review skills, clean tech debt, auto semantic review of PRs. All built into the core repo (origin=repo); the L4 self-development manual moved to the kernel-contributor family (.agents/skills).
tags:
- extensions
- agent-instruction
---

# Self-development & evolution skill

## What it is
A set of skills Ava uses to **improve itself**: mine regressions from run data, expand/review its own skill library, clean tech debt, review its own PRs. All built into the core repo (`install_registry` origin=repo).

| Skill | Purpose | Details |
|------|------|------|
| ava-self-evolution | Weekly collect real runs into trace dataset, mine skill/plugin regressions and produce fix reports | [[ava_builtins/skills/self_improvement/ava-self-evolution.ava.okf.md]] |
| skill-creator | Create / improve / review skill | [[ava_builtins/skills/self_improvement/skill-creator.ava.okf.md]] |
| sweeper | Tech debt sweep engine (reconcile repo debt tracker, land PR) | [[ava_builtins/skills/self_improvement/sweeper.ava.okf.md]] |
| auto-review | Automatic PR semantic review (AGENTS.md compliance, doc sync, security, test judgment) | [[ava_builtins/skills/self_improvement/auto-review.ava.okf.md]] |

The former group member **ava-self-development** (the L4 kernel-contributor
manual: PR → CI → merge → `ava cluster update`) moved to
`.agents/skills/ava-self-development/` — the kernel-contributor skill family —
per the four-layer modification model
(`decisions/2026-08-19-four-layer-modification-model.md`). Its runtime-facing
replacements are the top-level `ava-modification-layers` (pick the layer +
prod-checkout safety) and `develop-a-plugin` (the L3 ladder) skills, listed in
[[okf/skills/skills.ava.okf.md|Skill System]].

## Key dependencies
- [[ava/skills.ava.okf.md|Skill System]] — skill mechanism and core-vs-instance origin axis
- [[ava/self.ava.okf.md|ava.self]] — `ava.self` SDK (the `update()` it once carried — the cluster-wide rollout — was removed 2026-08; updates go through the CLI)
