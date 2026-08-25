---
name: ava-guide
description: Explains Ava's CLI, runtime, packages, MCP servers, presets, schedules, and agent concepts. Use whenever the task mentions operating Ava itself or any `ava` command, even if the requested action sounds routine.
---

# Operating Ava (the `ava` CLI)

You run as an agent process, but the deployment around you — the database, the
gateway, your peers, the machines they run on, the skills and MCP servers you
can reach — is operated through one command-line tool: **`ava`**. This skill is
the map of that tool: what the command families are, the mental model behind
them, and which sub-skill to open for a given job.

You do not need this for the work the user asks you to do day to day. Reach for
it when the task is about **yourself**: adding a capability, upgrading, checking
the fleet, understanding why something is split across machines. The one
user-facing exception is [onboarding](onboarding/SKILL.md) — the first-use
conversation with a new user.

## Running the CLI

Run `ava` through your shell (`ava.shell.run(...)`).

- On a production host the command is `ava ...` — a symlink on `PATH` that
  always points at the production checkout.
- In a dev checkout you prefix it: `.venv/bin/ava ...`.

`ava --help` lists every command; `ava <cmd> --help` drills into one. The help
text is the authoritative argument reference — this skill does **not** restate
flags. It carries the things help cannot: the mental model, when to use what,
and the design intent.

## Sub-skills

| If you need to… | Read |
|---|---|
| Start/stop/update the cluster, understand cluster/unit/machine model, manage channels, cut releases | [ops](ops/SKILL.md) |
| Dispose of dead agents' workspaces — cold-data disposal, tombstone, ledger | [workspace-cleanup](workspace-cleanup/SKILL.md) |
| Add, list, remove, enable/disable MCP servers | [mcp](mcp/SKILL.md) |
| Install, upgrade, remove skills & plugins; understand the difference | [packages](packages/SKILL.md) |
| Understand agents, commands, presets, schedules — the agent-level concepts | [agents](agents/SKILL.md) |
| Create, update, or manage agent config presets — turn user needs into a preset | [presets](presets/SKILL.md) |
| Pick the model a spawned agent runs on — tier judgment, cost policy, `config_overlay` | [models](models/SKILL.md) |
| Onboard a new user — interview preferences, discover intent, record memory, start the first task; migrate a user from Claude Code / Codex / OpenClaw / Hermes | [onboarding](onboarding/SKILL.md) |

Cluster + host config cuts across all of these: `ava config get/set/unset`
reads and writes the cluster's `.env` — the single source of truth for settings
like the update channel (`AVA_TRACK_BRANCH`). See [ops](ops/SKILL.md) for the
config-driven surfaces (channels, release cut).

## Division of Operations and Development

When you face a task related to Ava itself, first determine which category it falls into:

| If you want to… | Read this skill |
|---|---|
| Run the cluster, update code, switch tracks, manage MCP servers, install skills/plugins | **ava-guide** (this skill) |
| Change the deployment itself — edit a skill, develop a plugin, or modify kernel source | **ava-modification-layers** (pick the layer L1–L4; L3 continues into **develop-a-plugin**, L4 into **ava-self-development**) |

Simply put: **Ava Guide = operating system**, **the modification layers = development system**.

Things not handled by this skill:
- Modifying a line of SDK code and then making the cluster take effect → this is the full process of ava-self-development (PR → CI → merge → `ava cluster update`)
- Directly modifying files in the production checkout and then reloading → **never do this** (there is no "in-process shortcut")
- Manually using `git checkout` to switch branches in the production checkout → will break the startup of all new agents

## Design intent (so you operate with the grain, not against it)

- **One tool, exposed as a namespace.** You have exactly one action —
  `execute_code` — and every capability is a Python name under `ava.*`. The CLI
  is the *operator's* surface for the same system; reach for it when a job is
  about the deployment, not about composing capabilities in code.
- **The core is meant to shrink.** Scaffolding around the model is removed as
  the model gets stronger. That is why log reading, plugin-config merge, and
  Telegram push are *skills / CLI*, not permanent SDK functions: a capability
  earns a short SDK name only by being used often enough to pay for itself.
- **Changing your own code is not a CLI op.** You never edit the running source
  and reload it. Code changes go through PR -> CI -> merge -> `ava cluster update`.
  That whole flow is its own skill: **ava-self-development**. This guide stops
  at *operating* the deployment; self-modification lives there.
- **Use the existing primitive.** When you want a deployment to do something,
  there is almost always a command or an SDK call for it already. Prefer it over
  a clever shortcut (a hand-rolled SQL write, a bespoke transient channel) — the
  shortcut is the thing that breaks on the next upgrade.
