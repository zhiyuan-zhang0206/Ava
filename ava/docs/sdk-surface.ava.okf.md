---
type: doc
title: SDK Surface — ava.* Tool Overview (Index)
description: 'An agent has only one tool—`execute_code(code: str)`—but obtains all capabilities through the `ava.*` Python namespace. Each submodule corresponds to a separate concept file.'
tags: []
---

# SDK Surface — ava.* Tool Overview (Index)

An agent has only one tool—`execute_code(code: str)`—but obtains all capabilities through the `ava.*` Python namespace. Each submodule corresponds to a separate concept file.

`ava.help()` reads current module source for constant annotations and attribute
documentation. Reloading a plugin in place updates this view; unavailable source
is retried on the next discovery instead of being retained as an empty result.

## Module Index

### Files & Shell
- [[files.ava.okf.md]] — File read/write: read / write / edit / delete / glob / append
- [[shell.ava.okf.md]] — Shell commands: run() / run_background() (auto-report on completion) + sessions (new / send / capture / kill)

### Agent Interop
- [[ava/external/docs/external.ava.okf.md]] — local external Python attachment to a consented agent lease
- [[ava/agents/docs/agents.ava.okf.md]] — spawn / fork / send_message / terminate / resurrect / get_neighbors / get_ancestors / get_status
- [[ava_builtins/plugins/ava_fleet/docs/tasks/tasks.ava.okf.md|Tasks]] — Task registry `ava.tasks`: create / get / list / update / log (injected by ava_fleet plugin, not core SDK—docs in fleet subtree)
- [[presets.ava.okf.md]] — Configuration presets: list / get

### Tools & External
- [[ava/mcps/docs/mcps.ava.okf.md]] — MCP tool servers (determined by config; built-in chrome + servers installed via `ava mcp install` beyond core)
- [[web.ava.okf.md]] — Web access: search + fetch (concurrent)
- [[understand.ava.okf.md]] — Multimodal understanding primitive: understand(targets) → list[str], each target carrying prompt + text|paths
- [[watcher.ava.okf.md]] — Background listener: at / cron / launch (wake self = `ava.agents.send_message`)

### Context
- `ava.context` — the `AvaContext` this process runs as: `ava.context.identity` (`agent_id`, `owns_loop`, `actor`). The exec child rebuilds the host's context from the exec request envelope and installs it before plugin loading and user code; a script an agent launched derives it from `AVA_AGENT_ID`. It does not exist (raises `AttributeError`) in the agent host and in a bare script, as `ava.state` does not outside an exec turn. `ava.context.sql` / `.redis` / `.gateway` are the connections (lazy; released when the process ends); `ava.DB` / `ava.REDIS` are the same objects under their SDK names. Free functions such as `ava.agents.*` read the identity and clients from this one entry and keep their call shapes. All threads inside that child observe the same binding through normal imports, without a ContextVar or a patch to thread startup. The shared host supplies dependencies explicitly to its helpers and plugin callbacks, and never binds a current agent to the SDK module. Its boot entry fixes a shared-host purpose in the existing SDK process slot before plugins or graph work, so inherited `AVA_AGENT_ID` cannot turn it into a launched child or allow an external attachment. Context bind/release cannot clear that startup purpose. An external controller may bind one attachment for its process and restores its previous context on detach. Lifecycle writes with an omitted source require an established actor before HTTP; an absent identity is an error.

### Self & User
- [[self.ava.okf.md]] — Agent self (**core** ava.self): AGENT_ID / MACHINE_SPEC / SELF_MACHINE_NAME / attach / pause_heartbeat / compact / restart / terminate; `set_label` is separately injected by ava_fleet plugin
- [[ui.ava.okf.md]] — User interface: serve / notify / show / close
- [[ava_builtins/plugins/ava_memory/docs/memory-api.ava.okf.md]] — Long-term memory pool: semantic search
- [[ava/skills/docs/skills.ava.okf.md]] — Skill registry: ava.help(ava.skills.<name>)
