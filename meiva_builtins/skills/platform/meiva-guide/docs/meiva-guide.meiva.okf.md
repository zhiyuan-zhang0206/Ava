---
type: doc
title: Meiva Guide — Understand, deploy, operate, and extend Meiva
description: Shared task guide for humans, external agents, and Meiva agents; one root routes to deployment, operations, extensions, scheduling, and agent execution sub-skills.
tags:
- extensions
- agent-instruction
---

# Meiva Guide

`meiva_builtins/skills/platform/meiva-guide/` is the canonical guide package.
Meiva loads it at `$MEIVA_HOME/skills/meiva-guide/`; the open-standard mirror is
`.agents/skills/meiva-guide`. Host-global convergence projects the complete
package into already-present Codex and Claude Code skill homes.

## Audience and execution surface

Humans and external agents use the CLI; Meiva agents use applicable `meiva.*`
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
- `schedules`: persistent work; [[meiva_builtins/skills/platform/meiva-guide/schedules/docs/schedules.meiva.okf.md]].
- `pages`: artifact publishing, user input, and frontend resources;
  [[meiva_builtins/skills/platform/meiva-guide/pages/docs/pages.meiva.okf.md]].
- `external-agents`: delegated workers and identity takeover;
  [[meiva_builtins/skills/platform/meiva-guide/external-agents/docs/external-agents.meiva.okf.md]].
- `workspace-cleanup`, `onboarding`: workspace disposal and first use/migration.

`meiva-workflow` owns work organization and general work evaluation.
`skill-creator` owns skill authoring and the evaluation contract.
`meiva-self-development` remains a project-local contributor manual.
`impersonator-guide` is a separate built-in executor manual under
`meiva_builtins/skills/platform/impersonator-guide/`. Guide membership changes
neither kernel contribution requirements nor operator authorization.

The shared skill evaluation contract lives once at
`references/evaluation.md`, within the complete externally published Guide
package. Skill Creator owns the methodology and links to that canonical file.
Its optional self-evolution replay path loads the separately available Meiva
capability by name; external Guide copies do not assume it is bundled.

Sub-skills load substantial conditional procedures on demand. External-agent
Mode A reads `references/delegated-workers.md`; takeover keeps its own mode
boundary. Onboarding separates preference questions, intent branches, and
shared-memory note formats. `ops` separates resources and sessions; `operations`
separates diagnosis and authorized-rollout verification. Each entrypoint states
when to read the relevant reference.

## Consumers and distribution

Package and schedule draft endpoints load `meiva.skills.meiva_guide.packages.install`
and `meiva.skills.meiva_guide.schedules`. External copies use independent
`<client>-meiva-guide` ledgers; legacy deployment/operator copies and their ledgers
are preserved and are no longer refreshed by this bridge.

- [[docs/skills/external-agent-operator-bridge.meiva.okf.md]] — external publication and ownership
- [[meiva_builtins/skills/docs/skills.meiva.okf.md]] — built-in catalog
- [[cli/docs/cli.meiva.okf.md]] — CLI owner
