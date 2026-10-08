---
type: doc
title: Plugin System
description: Plugins are Ava's primary extension mechanism—inserting custom behavior into the agent runtime through multiple injection points. Each plugin is a directory containing a `plugin.py` entry point, loaded at agent process startup by `load_extensions()`; a plugin may add an `agent_runtime.py` face. A plugin may use every injection surface at once; `agent/extensions/catalog.py:SURFACES` is the enumeration, and `ava plugins inspect` renders it.
tags: []
---

# Plugin System

## What It Is
Plugins are Ava's primary extension mechanism—inserting custom behavior into the agent runtime through multiple injection points. Each plugin is a directory containing a `plugin.py` entry point, loaded at agent process startup by `load_extensions()`. A plugin may use every injection surface at once; `agent/extensions/catalog.py:SURFACES` is the enumeration, and `ava plugins inspect` renders it.

## Core Responsibilities

### 1. Graph-Edge Hooks (`agent/hooks/_registry.py`)
The four hook container nodes (after_init / before_llm / before_exec / after_exec), the `Hook` ABC instance-registration contract, and reducer-aware state merge: [[okf/plugins/graph-edge-hooks.ava.okf.md]].

### 2. State Field Extension (`agent/state.py`)
Declare a Pydantic `BaseModel` subclass (e.g., `AvaCodeState`, `AvaSdkReminderState`) in `PluginContributions.state`, and its fields are merged into `AgentState`; the plugin keeps a `PluginStateHandle(Cls, plugin)` to `read()` / `update()` within a turn. Fields are isolated with a namespace prefix (`<plugin>__<field>`) and persisted to checkpoints via LangGraph reducers.

**Core-key contract (writable = private fields + `messages` only).** A plugin's own `BaseModel` fields are always private and plugin-writable. Among the framework core keys (`BaseAgentState` fields: messages / halted / update_initiated / compact / memory / context_reset / capabilities), only **`messages`** may be declared and written — with the exact base annotation (`Annotated[list[AnyMessage], add_messages]`); the exec node merges the plugin's messages delta with its own ToolMessage delta, so both reach the checkpoint. Declaring any other core key raises at registration; writing one via `ava.state_update` raises at turn end. Plugins that want to surface notes do it through the after-exec hook — `ava_code`'s AGENTS.md / security-findings injection (`system_note_message`, `NoteTag`) is the model use case — never by touching core lifecycle keys.

### 3. Declared Contributions (`base/packages/plugins/extensions.py`)
System prompt sections and context notes are declared, not registered: `contribute() -> PluginContributions` in the plugin's `agent_runtime.py`, collected by the loader into an `ExtensionRegistry` — [[okf/plugins/declared-contributions.ava.okf.md]].

### 4. SDK Surface, Config and Flags
A plugin's `plugin.py` declares `sdk_namespaces` / `sdk_members` / `sdk_expansions` / `sdk_wraps` / `skill_sources` in `contribute()`. Its pure `default_config.py` may declare one frozen `config` class and the non-sensitive Core `flags` it reads; `configuration_declaration` admits both using the generated boot-lite field and sensitivity indexes, without importing full Settings, the SDK or installing the plugin; the framework installs them into the `ava` module in one place (`ava/sdk_surface/install.py`) — [[okf/plugins/declared-contributions.ava.okf.md]].

## The surface catalog + attribution
Each declaration derives attribution records (`PluginContributions.as_records`): which surface, what identifier (spelled as `ava-plugin.json` declares it), which plugin — the registry entry names it. `agent/extensions/catalog.py:SURFACES` enumerates the injection surfaces, each carrying the live signature of its declaration type. `ava plugins inspect` renders both halves, and `declared_vs_registered` is the read-only form of the manifest gate. [[cli/commands/extensions/packages/docs/packages.ava.okf.md|The verb]]. What actually FIRED is the runtime half, keyed by the same triple: [[activation-telemetry.ava.okf.md]].

## Key Dependencies
- [[agent/graph/docs/graph.ava.okf.md]] — hook container nodes call `make_hook_runner` at graph build time
- [[agent/docs/state.ava.okf.md]] — state field registration
- [[system-prompt.ava.okf.md]] — prompt injection
- [[agent/db/docs/db.ava.okf.md]] — state persisted to Postgres checkpoint

## Entry Points
- `base/packages/plugins/enable_config.py:discover_plugins()` — filesystem scan for `ava_builtins/plugins/<name>/plugin.py` (built-in) and `~/.ava/plugins/<name>/plugin.py` (external)
- `agent/extensions/__init__.py:load_extensions()` — imports plugins according to the enabled set, builds the gated registry from their faces' `contribute()` and installs its SDK surface (which binds configs). A plugin's optional `agent_runtime.py` face loads on the full form only ([[okf/plugins/module-loading/two-faces.ava.okf.md]]); the daemon calls it at host boot. Import mechanics, load order and reload semantics: [[okf/plugins/module-loading/module-loading.ava.okf.md]].
- `agent/graph/_build.py:build_graph()` — at build time hands each hook container its hooks
- `agent/state.py:build_agent_state()` — at build time merges all plugins' state fields
- `agent/extensions/catalog.py:build_catalog()` — loads this machine's enabled plugins and reads back what they declared (`ava plugins inspect`)

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
- Config files are per-machine, supporting different plugin combinations on different machines.

Config metadata records its nullable plugin owner (Core is `None`). A flat config PUT must address one owner; mixed patches reject without writing. The panel splits requests by owner, retains every verdict and successful restart target, and displays partial failures. Each image uses schema validation and its own CAS/atomic writer; no cross-file transaction is promised. Fleet keeps stable field names while moving its five fields out of Core.

Initial image creation rejects an image created concurrently or already present; it cannot reset explicit configuration. Schema updates preserve retained fields and use the same owned CAS/atomic writer as config edits.
