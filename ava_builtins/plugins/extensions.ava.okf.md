---
type: doc
title: Extensions
description: Ava's extension system — three independent mechanisms that give agents additional capabilities beyond the core runtime. Plugins modify agent behavior via hook injection
tags: []
---

# Extensions

## What is it
Ava's extension system — three independent mechanisms that give agents additional capabilities beyond the core runtime. Plugins modify agent behavior via hook injection; Skills are reusable instruction packages loaded on-demand by agents; MCP integrations (MCPs) allow agents to call external tool servers.

The three systems are decoupled — plugins don't need skills, skills don't depend on MCP — but they share the same file discovery and configuration patterns.

## Core Responsibilities
- **Plugin system**: modifies agent runtime behavior via graph-edge hooks (before_llm / before_exec / after_exec), SDK namespace registration, system prompt injection, and state field extension; additionally, it can **register an ops background service** — a plugin provides `plugins/<name>/services.py` exposing `services() -> tuple[ServiceSpec,...]`; `ops/spec.py:_plugin_services()` discovers by plugin code **existence** and folds into the single-source `build_services()` roster (session name conflicts fail-fast). Watchdog / `ava start` / `ava status` all cover this. "Plugin declares, ops discovers", no reverse dependency on plugin domain code. Note that this discovery path **only looks at code presence, not agent-side plugin enable/disable** (the roster is machine-side; the service's own switch uses `ServiceSpec.gate` reading explicit settings). fleet's task-maintenance daemon is registered this way ([[ava_builtins/plugins/ava_fleet/ava_fleet.ava.okf.md|ava_fleet]])
- **Skill system**: markdown instruction packages loaded on demand — agents lazily load the full SKILL.md when doing `ava.help(ava.skills.<path>)`
- **MCP integration**: calls external MCP tool servers via `ava.mcps.<server>.<tool>()`; the server set is a four-layer merge of **built-in `<repo>/ava_builtins/mcps/*` (chrome) + plugin `.mcp.json` + installed `$AVA_HOME/mcps/*` (external packages installed via `ava mcp install`, e.g., x) + per-machine `mcpServers` JSON** (`ava/_mcp_config.py`) — machine file is an override/enable-disable layer, not the sole source

## Key Dependencies
- [[agent/graph/graph.ava.okf.md]] — plugin hook container nodes reside in the execution graph
- [[state.ava.okf.md]] — plugins extend AgentState via `register_plugin_state`
- [[system-prompt.ava.okf.md]] — both plugins and skills inject system prompts
- [[mcp-daemon.ava.okf.md]] — MCP daemon subprocess management
- [[agents-contract.ava.okf.md]] — contracts for plugin context and configuration registration

## Entry Points
- `shared/plugins_config.py:_discover_plugins()` — scans builtin + external plugin directories; `installed_plugin_dirs()` = plugins present on this machine (for service discovery, judged by presence regardless of enable state)
- `plugins/<name>/services.py:services()` + `ops/spec.py:_plugin_services()` — hook for plugin registering ops background services + the discovery side
- `shared/plugin_config_registry.py:register_plugin_config()` — plugin Config class registration
- `agent/hooks/_registry.py:register_before_llm()` — hook registration
- `agent/state.py:register_plugin_state()` — state field registration
- `ava/skills.py` — skill loading and help() rendering
- `ava/mcps.py` — MCP client interface

## Notes
- Plugins and MCPs can be enabled/disabled per machine (`~/.ava/plugins_config.json` / `~/.ava/mcp_enabled.json`)
- 6 built-in plugins are all enabled by default, but can be turned off per machine
- Skills are pure markdown files, with no runtime state — the lightest extension method
