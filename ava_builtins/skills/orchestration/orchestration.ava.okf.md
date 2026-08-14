---
type: doc
title: Workflow orchestration skill
description: A set of skills for multi-agent / long-task orchestration — three-actors / evaluation-thread workflow methodology, parallel workers explore→fork→join→reduce, goal supervision, driving Claude Code / Codex CLI. All built into the core repo (origin=repo).
tags:
- extensions
- agent-instruction
---

# Workflow orchestration skill

## What it is
A set of skills that **break large tasks into multi-agent / long tasks and drive them**: three-phase methodology, parallel worker fan-out and re-collection, supervising another agent to achieve a goal, driving external coding CLIs. All built into the core repo (origin=repo).

| Skill | Purpose | Details |
|------|------|------|
| ava-workflow | Three actors (agents / human / real world), three phases (Calibrate / Align / Plan) with Evaluation threaded through all of them; Plan is only for very large or parallel tasks; sub: calibrate / align / plan / work-eval | [[ava_builtins/skills/orchestration/ava-workflow.ava.okf.md]] |
| ava-dynamic-workflow | Orchestrate parallel workers: explore→fork→join→reduce | [[ava_builtins/skills/orchestration/ava-dynamic-workflow.ava.okf.md]] |
| ava-goal | Supervise another agent to achieve a goal (watcher wakes up on target idle to judge) | [[ava_builtins/skills/orchestration/ava-goal.ava.okf.md]] |
| ava-use-claude-code-and-codex | Drive Claude Code / OpenAI Codex CLI for long tasks | [[ava_builtins/skills/orchestration/ava-use-claude-code-and-codex.ava.okf.md]] |

## Key dependencies
- [[ava/skills.ava.okf.md|Skill System]] — skill mechanism and core-vs-instance origin axis
- [[agents.ava.okf.md]] — parallel workers rely on `ava.agents.spawn/fork/send_message` for fan-out and collection
