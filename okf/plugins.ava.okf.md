---
type: doc
title: Plugin System
description: Plugins are Ava's primary extension mechanism—inserting custom behavior into the agent runtime through multiple injection points. Each plugin is a directory containing a `plugin.py` entry point, loaded at agent process startup by `_load_extensions()`. Plugins can use all five injection mechanisms simultaneously.
tags: []
---

# Plugin System

## What It Is
Plugins are Ava's primary extension mechanism—inserting custom behavior into the agent runtime through multiple injection points. Each plugin is a directory containing a `plugin.py` entry point, loaded at agent process startup by `_load_extensions()`. Plugins can use all five injection mechanisms simultaneously.

## Core Responsibilities

### 1. Graph-Edge Hooks (`agent/hooks/_registry.py`)
Four hook container nodes sit in the LangGraph execution graph (topology: START → after_init → init_context → claim → before_llm → llm → before_exec → exec → after_exec → claim):
- `after_init` — runs once after the checkpoint is loaded, before `claim` (e.g., restores `os.chdir` on agent restart; introduced 2026-07-23, used by ava_code's `register_after_init(sync_cwd_after_init)`)
- `before_llm` — after claim completion, before LLM invocation
- `before_exec` — after LLM completion, before code execution
- `after_exec` — after code execution completion, before the next node

Plugins register an **instance** of a `Hook` ABC subclass (PyTorch `nn.Module` shape—base class locks the signature, subclasses fill the body), rather than a bare async function + decorator:
```python
from agent.hooks import Hook, register_before_llm

class MyHook(Hook):
    async def __call__(self, state, runtime, config, /) -> dict | None:
        return None  # or return a dict for state update

register_before_llm(MyHook())
```

The signature is inherited and checked by pyright strict's `reportIncompatibleMethodOverride`—narrowing parameter types / widening return value / missing parameters are caught at type-checking time, no longer just a convention described in a Protocol. The four hook points share the same `Hook` base class (same signature); the only difference is which list they are registered into. Instances can carry per-hook state in `__init__`. Returning a dict can modify state; returning None is a no-op. When two hooks write the same key in the same round, the merge is **reducer-aware** (`agent/hooks/_registry.py:202-229`): keys with reducers (e.g., `messages`→`add_messages`) merge both values; **only keys without reducers raise RuntimeError** (avoiding silent overwrites).

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
Packages may ship an `ava-plugin.json` at their root declaring identity
(name/version/`engines.ava` range), dependencies (`plugins` /
`pythonPackages` / `hostCapabilities`), contribution surfaces, and lifecycle
shape. Install paths validate it via `shared/plugin_manifest.py`; runtime
loading, lifecycle states, and context gates land post-open-source. Full
contract: [conventions/plugin-spec-v2.md](../conventions/plugin-spec-v2.md).
## Notes
- Plugins can carry **skills** (`ava_builtins/plugins/<p>/skills/`, converge syncs them with the plugin name as the top-level directory; nodes hang under each plugin subtree) and **MCP server definitions** (`.mcp.json`), and can also register **ops services** (`services.py` declaring `ServiceSpec`, e.g., ava_fleet's task-maintenance).
- All hooks share a single global HOOKS list—`make_hook_runner` snapshots the reference, not a copy.
- Plugin loading order = order of the `config.plugins` dict (sorted alphabetically by `shared/plugins_config.py`), with `_build.py:_load_extensions` importing one by one—**no dependency declaration / topological sort mechanism**; configs are uniformly bound after all imports complete.
- Config files are per-machine, supporting different plugin combinations on different machines.
