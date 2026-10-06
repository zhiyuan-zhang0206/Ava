---
name: ava-guide
description: Guides humans, external agents, and Ava agents in understanding, deploying, operating, and extending Ava. Use for Ava CLI or SDK operations, cluster health, packages, plugins, schedules, agent configuration, page publishing and user input, or external-agent delegation and takeover.
---

# Ava Guide

This is the shared guide to using Ava: understand the system, deploy a cluster,
operate it, add capabilities, schedule work, and work with agents. It serves
humans, external agents such as Codex or Claude Code, and agents inside Ava.
The root is a map; load the relevant sub-skill for the task.

## Choose your execution surface

- **Humans and external agents:** use the `ava` CLI from a shell. On a
  production host, `ava` points to the production checkout. In a development
  checkout, use its own `.venv/bin/ava` and an isolated development home.
- **Ava agents:** use the `ava.*` SDK for capabilities available to your agent;
  use `ava.shell.run(...)` for CLI operations. Load a sub-skill with
  `ava.help(ava.skills.ava_guide.<subskill>)`.
- **Scripts and schedules:** use the documented CLI/API or SDK surfaces that
  support their execution identity. A schedule is not an agent; agent-only
  capabilities require handing the work to an agent.

`ava --help` and `ava <command> --help` own CLI argument syntax. This guide owns
concepts, task routing, operational judgment, and verification. Repository
references such as `docs/conventions/runbook.md` are relative to the Ava source
checkout, not the installed skill copy. For a deployment, that source is
`$AVA_HOME/source`; an external reader uses the checkout they are operating.

## Find the task

| Task | Read |
|---|---|
| Install Ava, initialize a home, or join a runner | [deploy](deploy/SKILL.md) |
| Start, stop, update, or configure the cluster; manage releases | [ops](ops/SKILL.md) |
| Check health, triage alerts, diagnose failures, or recover infrastructure | [operations](operations/SKILL.md) |
| Decide where a change belongs and how it takes effect (L1–L4) | [modification-layers](modification-layers/SKILL.md) |
| Understand skills/plugins or manage installed packages | [packages](packages/SKILL.md) |
| Find a capability, compare candidates, install, and verify it | [packages.install](packages/install/SKILL.md) |
| Add, inspect, or remove MCP servers | [mcp](mcp/SKILL.md) |
| Understand Ava plugins or develop a native plugin | [plugins](plugins/SKILL.md), then [plugins.develop](plugins/develop/SKILL.md) |
| Create or manage agents, commands, and config overlays | [agents](agents/SKILL.md) |
| Create and manage reusable agent configurations | [presets](presets/SKILL.md) |
| Choose a model for an agent | [models](models/SKILL.md) |
| Write, create, inspect, or change persistent scheduled work | [schedules](schedules/SKILL.md) |
| Publish an artifact or collect user input through a page | [pages](pages/SKILL.md) |
| Launch and supervise an external coding agent, or arrange a takeover | [external-agents](external-agents/SKILL.md) |
| Dispose of dead agents' workspaces | [workspace-cleanup](workspace-cleanup/SKILL.md) |
| Begin using Ava or migrate from another tool | [onboarding](onboarding/SKILL.md) |

## Shared boundaries

The guide explains **how to use Ava's capabilities**. `ava-workflow` owns how
to clarify goals, calibrate assumptions, plan, delegate, and evaluate work.
`skill-creator` owns reusable skill-writing methodology; use
[modification-layers](modification-layers/SKILL.md) to decide where a skill edit
belongs. Kernel contributors follow `AGENTS.md`, `docs/contributing.md`, and
the project-local `ava-self-development` skill.

A built-in skill or plugin edit is a kernel contribution (L4), even when its
content resembles a local extension (L2/L3). Develop in an isolated checkout;
the production source is the tree from which the live cluster boots. Kernel
integration and operator rollout are separate actions. Guide membership does
not grant operator authority: use the authorization and verification rules
for the action you are taking.

External agents may operate Ava through the CLI without borrowing an agent
identity. A takeover is a separate mode: its launcher follows
[external-agents](external-agents/SKILL.md), and its executor follows the
project-local `impersonator-guide` under `.agents/skills/impersonator-guide/`.

## Design intent

- Ava keeps one agent tool, `execute_code`, with capabilities under `ava.*`.
- Prefer existing CLI, API, and SDK primitives over parallel implementations.
- Keep shared facts with their owners. Use CLI help for flags, repository
  conventions for runtime contracts, and sub-skills for operational procedures.
- Load only the detail needed for the task; keep this root a compact map.
