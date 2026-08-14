---
type: doc
title: ava-self-evolution skill — Mining regressions from real runs
description: Weekly, collect real agent runs from the past week into a trace dataset, dig out failed/stumbling runs, tie them to recently modified skills/plugins, and produce concrete fix reports. Not a pre-merge gate/benchmark/synthetic test — reads what really happened.
tags:
- extensions
- agent-instruction
---

# ava-self-evolution skill — Mining regressions from real runs

## What it is
Ava improves by observing its **real usage**. This skill (`$AVA_HOME/skills/ava-self-evolution/`) weekly turns the real agent runs from the past week into a **trace dataset**, finds failed/stumbling runs, ties them to recently modified skills/plugins, and proposes concrete fixes. The dataset is a cumulative durable asset — growing a little each week, it's the target material for iterating skills/plugins. **It is explicitly not** a pre-merge gate, benchmark, or synthetic test suite: it reads things that really happened.

## Backpropagation analogy
The evaluation loop optimizes skill text like backpropagation optimizes weights: forward pass = agent runs a task with current skills; loss = `rubric.py` scoring (completeness + efficiency); **backward pass (gradient) = directly ask the agent "what should change"**; weight update = edit skill text. The backward pass is **80% agent self-reflection** (the agent that ran the task knows best where it stumbled, ask it first), 20% another worker mining traces.

## Boundaries
Output data lands in `$AVA_HOME/self_evolution/` (per-deployment private), not in the repo. Triggered by a weekly cron job spawn; the skill directory name contains hyphens, reference scripts cannot be imported as a package, only run via `.venv/bin/python`.

## Key dependencies
- [[ava_builtins/skills/self_improvement/self_improvement.ava.okf.md|Self-improvement & evolution skill]] — belongs to functional group
- [[ava/skills.ava.okf.md|Skill System]] — the target of mining regressions is the skill library itself
- [[ava_builtins/skills/self_improvement/skill-creator.ava.okf.md|skill-creator]] — found fixes land via creating/modifying skills
