---
type: doc
title: ava-guide skill — Operate the deployment via the ava CLI
description: "A map skill for the agent to operate its own deployment — the root SKILL.md is an index, with weight carried by seven bare-name sub-skills: ops / mcp / packages / agents / presets / models / onboarding. Provides the mental model and design intent of the CLI (help already gives flags), never restates flags."
tags:
- extensions
- agent-instruction
---

# ava-guide skill — Operate the deployment via the ava CLI

## What is it
The agent runs inside a process, but its surrounding deployment (DB, gateway, peers, machine, reachable skills/MCP) is operated via a CLI tool, **`ava`**. This skill (`$AVA_HOME/skills/ava-guide/`) is a map of that tool: command families, the mental model behind them, and which sub-skill to open for which task. It **never restates flags** (`ava <cmd> --help` is the authoritative parameter reference); it only carries what help cannot: the mental model, when to use what, and design intent. You won't need it for everyday user tasks; reach for it when the task is about **yourself** (adding capabilities / upgrading / inspecting the fleet / understanding why something is cross-machine).

## Seven Sub-skills (Bare Names Carry Weight)
The root is an index; the real content lives in seven sub-skills: `ops` (start/stop/update clusters, cluster/unit/machine model, channels, release cut, sessions), `mcp` (install/manage MCP servers + wrapper pattern), `packages` (install/upgrade/remove skills & plugins + skill-vs-plugin distinction), `agents` (agent/command/preset/schedule concepts), **`presets`** (create/modify/manage agent config presets — turn a user's request for "a new agent type" into a preset; added in d31660c8), `models` (which LLM a spawned agent runs on — tier judgment + cost policy), `onboarding` (first use of a cluster with a new user — preference interview, intent discovery, memory write-up, first task; the one user-facing sub-skill).

## Design Intent (Operate Along the Grain)
- **One tool = one namespace**: the agent has only one action, `execute_code`; each capability is a Python name under `ava.*`. The CLI is the **operator's** surface to the same system.
- **Core is destined to shrink**: as models get stronger, remove scaffolding — log reading, plugin-config merge, Telegram push are *skill/CLI*, not persistent SDK functions; a capability has to be used frequently enough to deserve a short SDK name.
- **Modifying its own code is not a CLI op**: never edit the running source and reload; kernel-code changes go through the full L4 workflow of the `ava-self-development` skill (`.agents/skills/ava-self-development/`; layer routing = the `ava-modification-layers` skill). ava-guide = operations system, ava-self-development = development system.

## Key Dependencies
- [[ava_builtins/skills/ops_lifecycle/ops_lifecycle.ava.okf.md|Ops, Scheduling & Lifecycle Skills]] — parent functional group
- [[../../../cli/cli.ava.okf.md|Command Line]] — the `ava` CLI itself (cluster lifecycle implementation)
- [[ava/presets.ava.okf.md|ava.agents.presets]] — the preset model that the presets sub-skill operates
