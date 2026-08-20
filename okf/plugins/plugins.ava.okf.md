---
type: doc
title: Plugin System
description: Plugins are Ava's primary extension mechanism—inserting custom behavior into the agent runtime through multiple injection points. Each plugin is a directory containing a `plugin.py` entry point, loaded at agent process startup by `_load_extensions()`. A plugin may use every injection surface at once; `agent/plugin_catalog.py:SURFACES` is the enumeration, and `ava plugins inspect` renders it.
tags: []
---

# Plugin System

## What It Is
Plugins are Ava's primary extension mechanism—inserting custom behavior into the agent runtime through multiple injection points. Each plugin is a directory containing a `plugin.py` entry point, loaded at agent process startup by `_load_extensions()`. A plugin may use every injection surface at once; `agent/plugin_catalog.py:SURFACES` is the enumeration, and `ava plugins inspect` renders it.

## Core Responsibilities

### 1. Graph-Edge Hooks (`agent/hooks/_registry.py`)
The four hook container nodes (after_init / before_llm / before_exec / after_exec), the `Hook` ABC instance-registration contract, and reducer-aware state merge: [[okf/plugins/graph-edge-hooks.ava.okf.md]].

### 2. State Field Extension (`agent/state.py`)
`register_plugin_state(Cls)` — pass a Pydantic `BaseModel` subclass (e.g., `AvaCodeState`, `AvaSdkReminderState`), and its fields are merged into `AgentState`, returning a `PluginStateHandle` for the plugin to `read()` / `update()` within a turn. Fields are isolated with a namespace prefix (`<plugin>__<field>`) and persisted to checkpoints via LangGraph reducers.

**Core-key contract (writable = private fields + `messages` only).** A plugin's own `BaseModel` fields are always private and plugin-writable. Among the framework core keys (`BaseAgentState` fields: messages / halted / update_initiated / compact / memory / context_reset / capabilities), only **`messages`** may be declared and written — with the exact base annotation (`Annotated[list[AnyMessage], add_messages]`); the exec node merges the plugin's messages delta with its own ToolMessage delta, so both reach the checkpoint. Declaring any other core key raises at registration; writing one via `ava.state_update` raises at turn end. Plugins that want to surface notes do it through the after-exec hook — `ava_code`'s AGENTS.md / security-findings injection (`system_note_message`, `NoteTag`) is the model use case — never by touching core lifecycle keys.

### 3. System Prompt Injection (`agent/graph/_system_prompt.py`)
`register_system_prompt_section(fn)` — register a `() -> str` contributor function as a **decorator**; `build_system_prompt()` calls them in registration order at boot time to compose the system prompt. Returning an empty string means no contribution.

### 4. SDK Namespace Registration
`ava.register_namespace(name, module)` — registers a new namespace under `ava.*` (e.g., `ava.cwd` from the ava_code plugin). `ava.register_sdk_expand(name)` promotes that namespace into the system prompt's expanded SDK reference. `ava.register_namespace_member(namespace, name, fn)` — attaches a callable member to an already-registered namespace (used by ava_fleet to inject task helpers); all three are exported from `ava/__init__.py`.

### 5. Plugin Config (`shared/plugin_config_registry.py`)
Two-phase design:
- `register_plugin_config(Cls)` — registers a Pydantic BaseModel class
- `bind_from_disk()` — the framework reads from `~/.ava/configs/<plugin>/config.json` and instantiates

Config instances are frozen (`ConfigDict(frozen=True)`) and cannot be modified by agents. Fields marked `json_schema_extra={"per_agent": True}` allow per-agent CLI overlay.

## The surface catalog + registration attribution
Every `register_*` entry point above also writes one record to the attribution
ledger (`shared/plugin_contributions.py`): which surface, what identifier
(the hook point, the `ava` namespace, the wrap target, the state channel key —
spelled the way `ava-plugin.json` declares it), and which plugin, read off the
`PluginContext` the loader opens. Only registrations made inside that context are
recorded, so the ledger holds plugin contributions alone and
`clear_plugin_registrations` clears it with the registries it shadows.

`agent/plugin_catalog.py:SURFACES` is the enumeration of the injection surfaces —
the ones above, plus SDK wraps (`ava.extend.wrap`, [[extensions.ava.okf.md]]),
context notes (`agent/graph/_context_notes.py:register_context_note`) and skill
sources (`ava/skills.py:register_skill_source`) — each carrying the live
signature of its entry point rather than a transcribed one. `ava plugins inspect`
renders both halves, and `declared_vs_registered` is the read-only form of the
plugin-spec-v2 S3 gate. [[cli/commands/packages.ava.okf.md|The verb]].

The ledger records what was REGISTERED; what actually FIRED is the runtime half,
keyed by the same triple: [[activation-telemetry.ava.okf.md]].

## Key Dependencies
- [[agent/graph/graph.ava.okf.md]] — hook container nodes call `make_hook_runner` at graph build time
- [[state.ava.okf.md]] — state field registration
- [[system-prompt.ava.okf.md]] — prompt injection
- [[db.ava.okf.md]] — state persisted to Postgres checkpoint
- [[agents-contract.ava.okf.md]] — `PluginContext` ContextVar ensures registration isolation

## Entry Points
- `shared/plugins_config.py:_discover_plugins()` — filesystem scan for `ava_builtins/plugins/<name>/plugin.py` (built-in) and `~/.ava/plugins/<name>/plugin.py` (external)
- `agent/graph/_build.py:_load_extensions()` — imports plugins according to the enabled set (each `plugin.py` import wrapped with `with PluginContext(name):`), after which `bind_from_disk()` uniformly instantiates configs
- `agent/graph/_build.py:build_graph()` — at build time calls `make_hook_runner` to snapshot hook lists
- `agent/state.py:build_agent_state()` — at build time merges all plugins' state fields
- `agent/plugin_catalog.py:build_catalog()` — loads this machine's enabled plugins and reads back what they registered (`ava plugins inspect`)

## Built-in Plugins
| Plugin | Responsibility |
|--------|----------------|
| ava_code | cwd management + AGENTS.md auto-injection |
| ava_fleet | multi-agent collaboration tool registration and task system |
| ava_memory | long-term memory pool—read/write and semantic search of markdown notes |
| ava_sdk_reminder | reminds agent to use more appropriate APIs after SDK calls |
| ava_silent_idle | controls output behavior when idle |
| ava_syntax_fix | deterministic syntax fixes for agent code |

## Package Manifest (spec v2)
The `ava-plugin.json` a package may ship — identity, dependencies, declared
contribution surfaces (including the console's `contributions.ui`), lifecycle
shape — and where each is validated: [[okf/plugins/package-manifest.ava.okf.md]].

## Notes
- Plugins can carry **skills** (`ava_builtins/plugins/<p>/skills/`, converge syncs them with the plugin name as the top-level directory; nodes hang under each plugin subtree) and **MCP server definitions** (`.mcp.json`), and can also register **ops services** (`services.py` declaring `ServiceSpec`, e.g., ava_fleet's task-maintenance).
- All hooks share a single global HOOKS list—`make_hook_runner` snapshots the reference, not a copy.
- Plugin loading order = order of the `config.plugins` dict (sorted alphabetically by `shared/plugins_config.py`), with `_build.py:_load_extensions` importing one by one—**no dependency declaration / topological sort mechanism**; configs are uniformly bound after all imports complete.
- Config files are per-machine, supporting different plugin combinations on different machines.
