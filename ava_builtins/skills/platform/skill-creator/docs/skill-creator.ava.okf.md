---
type: doc
title: skill-creator skill — Create/modify/review skill
description: Authoring and evaluation workflow for Ava skills, informed by Anthropic and OpenAI. Owns cases, metrics, baseline comparison, evidence, precise discovery, and progressive disclosure; Preset Maker uses the same evaluation contract.
tags:
- extensions
- agent-instruction
---

# skill-creator skill — Create/modify/review skill

## What it is
An **authoring and evaluation method** for Ava skills
(`$AVA_HOME/skills/skill-creator/`), informed by Anthropic and OpenAI and adapted
to Ava's tools. `SKILL.md` carries the concise workflow;
`references/evaluation.md` owns the shared evaluation contract for skills and
presets. `evals/evals.json` and `evals/metrics.md` carry the creator's own
authoring cases and grading definitions. Run artifacts stay outside the package.

`references/audit.md` owns catalog-audit criteria and primary guidance.
`evals/catalog-audit.json` and `evals/catalog-audit-metrics.md` carry portable
metadata-routing and operational-navigation probes. These are offline evaluator
inputs; the metrics distinguish packaging checks, proxies, and live execution.

## Judgments carried
- **Three-level loading model**: metadata (name+description, always in context) / SKILL.md body (loaded when triggered) / bundled resources (scripts/references/assets, on demand). When writing a skill, distribute content across these three levels — description should let the agent determine "when to reach for it."
- **When to create a skill**: when an instruction has no existing config field to carry it, and will be used repeatedly, creating a small skill is cleaner and more reusable than piling into config (same origin as [[ava/docs/presets.ava.okf.md|presets]]'s "no carrying field, just create a small skill").
- **skill vs plugin**: skill is a pure markdown instruction package, no runtime state; to modify agent behavior / inject hooks, use plugin.
- **Evaluation is a deliverable**: cases, metric definitions, acceptance criteria,
  and a baseline precede execution. Results retain per-case output/trace evidence
  and execution gaps; incomplete evaluation remains a draft. Quality and
  efficiency are reported separately.
- **Discovery vs execution**: descriptions are precise rather than broadly
  pushy. Implicit activation is evaluated separately from an explicitly loaded
  or preloaded skill's task behavior.
- **Harness boundary**: the case-file convention is not a new runtime API.
  The existing self-evolution harness accepts replay-safe dataset records and
  produces proxy scores; task correctness needs its own checks.

## Key dependencies
- [[ava_builtins/skills/docs/skills.ava.okf.md|Skills index]] — full skills catalog
- [[ava/skills/docs/skills.ava.okf.md|Skill System]] — created things land in this mechanism
- [[okf/plugins/plugins.ava.okf.md|Plugin system]] — boundary between skill and plugin
- [[ava_builtins/skills/platform/ava-self-evolution/docs/ava-self-evolution.ava.okf.md|Self-evolution]] — existing trace replay, isolation limits, and proxy scoring
