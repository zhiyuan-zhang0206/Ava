---
type: doc
title: Ava Guide — Understand, deploy, operate, and extend Ava
description: Shared task guide for humans, external agents, and Ava agents; one root routes to deployment, operations, extensions, scheduling, and agent execution sub-skills.
tags:
- extensions
- agent-instruction
---

# Ava Guide

`ava_builtins/skills/platform/ava-guide/` is the canonical guide package.
Ava loads it at `$AVA_HOME/skills/ava-guide/`; the open-standard mirror is
`.agents/skills/ava-guide`. Host-global convergence projects the complete
package into already-present Codex and Claude Code skill homes.

## Audience and execution surface

Humans and external agents use the CLI; Ava agents use applicable `ava.*`
capabilities and the CLI through their shell. Schedules use the documented
surfaces available to their schedule identity. The root describes these entry
points, source-checkout references, and authority boundaries. It remains a map;
sub-skills carry executable procedures and their bundled resources.

## Task hierarchy

- `deploy`: acquire dependencies, initialize, and join a runner.
- `ops`: cluster lifecycle, settings, tracks, and releases.
- `operations`: health, incident triage, diagnosis, and recovery.
- `modification-layers`: route L1 installation, L2 skill edits, L3 native plugin
  development, and L4 kernel contributions; built-in changes are L4.
- `packages` / `packages.install`: installed-package mechanics and the full
  discovery, approval, installation, verification, and judgment flow. The
  install skill's `references/sources.md` owns the shared map of skill
  publishers, MCP directories and providers, and prompt sources.
- `mcp`, `plugins` / `plugins.develop`: MCP management and native plugin development.
- `agents`, `models`: agent concepts, configuration, and model choice.
- `presets`: Preset Maker composes role instructions, researched prompts,
  skills, MCP prerequisites, and registered model settings into reusable
  configurations; role cards remain instance-owned skills. The maker uses
  `skill-creator`'s evaluation contract and delivers cases, metrics, a baseline
  comparison, and evidence for the complete composition.
- `schedules`: persistent work; [[ava_builtins/skills/platform/ava-guide/schedules/docs/schedules.ava.okf.md]].
- `pages`: artifact publishing, user input, and frontend resources;
  [[ava_builtins/skills/platform/ava-guide/pages/docs/pages.ava.okf.md]].
- `external-agents`: delegated workers and identity takeover;
  [[ava_builtins/skills/platform/ava-guide/external-agents/docs/external-agents.ava.okf.md]].
- `workspace-cleanup`, `onboarding`: workspace disposal and first use/migration.

`ava-workflow` owns work organization and general work evaluation.
`skill-creator` owns skill authoring and the evaluation contract used by Preset
Maker. `ava-self-development` and `impersonator-guide`
remain project-local contributor/executor manuals. Guide membership changes
neither kernel contribution requirements nor operator authorization.

Sub-skills load substantial conditional procedures on demand. External-agent
Mode A reads `references/delegated-workers.md`; takeover keeps its own mode
boundary. Onboarding separates preference questions, intent branches, and
shared-memory note formats. `ops` separates resources and sessions; `operations`
separates diagnosis and authorized-rollout verification. Each entrypoint states
when to read the relevant reference.

## Consumers and distribution

Package and schedule draft endpoints load `ava.skills.ava_guide.packages.install`
and `ava.skills.ava_guide.schedules`. The Presets page uses plain agent spawn,
preloads `ava-guide:presets`, and opens the maker conversation with an optional
initial request. External copies use independent
`<client>-ava-guide` ledgers; legacy deployment/operator copies and their ledgers
are preserved and are no longer refreshed by this bridge.

- [[okf/skills/external-agent-operator-bridge.ava.okf.md]] — external publication and ownership
- [[ava_builtins/skills/docs/skills.ava.okf.md]] — built-in catalog
- [[cli/docs/cli.ava.okf.md]] — CLI owner
- [[ava/docs/presets.ava.okf.md]] — agent preset model
