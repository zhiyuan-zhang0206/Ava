---
type: doc
title: Self-development & evolution skill
description: A set of skills for Ava to improve itself — modify own code and make it effective, collect real runs into traces to mine regressions, create/review skills, clean tech debt, auto semantic review of PRs. All built into the core repo (origin=repo).
tags:
- extensions
- agent-instruction
---

# Self-development & evolution skill

## What it is
A set of skills Ava uses to **improve itself**: modify own code, mine regressions from run data, expand/review its own skill library, clean tech debt, review its own PRs. All built into the core repo (`install_registry` origin=repo).

| Skill | Purpose | Details |
|------|------|------|
| ava-self-development | Ava modifies own code and makes it effective: PR → `ava cluster update` (the CLI; `ava.self.update()` removed 2026-08) | [[ava_builtins/skills/self_improvement/ava-self-development.ava.okf.md]] |
| ava-self-evolution | Weekly collect real runs into trace dataset, mine skill/plugin regressions and produce fix reports | [[ava_builtins/skills/self_improvement/ava-self-evolution.ava.okf.md]] |
| skill-creator | Create / improve / review skill | [[ava_builtins/skills/self_improvement/skill-creator.ava.okf.md]] |
| sweeper | Tech debt sweep engine (reconcile repo debt tracker, land PR) | [[ava_builtins/skills/self_improvement/sweeper.ava.okf.md]] |
| auto-review | Automatic PR semantic review (AGENTS.md compliance, doc sync, security, test judgment) | [[ava_builtins/skills/self_improvement/auto-review.ava.okf.md]] |

## Key dependencies
- [[ava/skills.ava.okf.md|Skill System]] — skill mechanism and core-vs-instance origin axis
- [[ava/self.ava.okf.md|ava.self]] — `ava.self` SDK (the `update()` it once carried — the cluster-wide rollout — was removed 2026-08; updates go through the CLI)
