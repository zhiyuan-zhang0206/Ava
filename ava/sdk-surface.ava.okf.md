---
type: doc
title: SDK Surface — ava.* Tool Overview (Index)
description: 'An agent has only one tool—`execute_code(code: str)`—but obtains all capabilities through the `ava.*` Python namespace. Each submodule corresponds to a separate concept file.'
tags: []
---

# SDK Surface — ava.* Tool Overview (Index)

An agent has only one tool—`execute_code(code: str)`—but obtains all capabilities through the `ava.*` Python namespace. Each submodule corresponds to a separate concept file.

## Module Index

### Files & Shell
- [[files.ava.okf.md]] — File read/write: read / write / edit / delete / glob / append
- [[shell.ava.okf.md]] — Shell commands: run() / run_background() (auto-report on completion) + sessions (new / send / capture / kill)

### Agent Interop
- [[agents.ava.okf.md]] — spawn / fork / send_message / terminate / resurrect / get_neighbors / get_status
- [[ava_builtins/plugins/ava_fleet/tasks/tasks.ava.okf.md|Tasks]] — Task registry `ava.tasks`: create / get / list / update / log (injected by ava_fleet plugin, not core SDK—docs in fleet subtree)
- [[presets.ava.okf.md]] — Configuration presets: list / get

### Tools & External
- [[ava/mcps.ava.okf.md]] — MCP tool servers (determined by config; built-in chrome + servers installed via `ava mcp install` beyond core)
- [[web.ava.okf.md]] — Web access: search + fetch (concurrent)
- [[understand.ava.okf.md]] — Multimodal understanding primitive: understand(targets) → list[str], each target carrying prompt + text|paths
- [[watcher.ava.okf.md]] — Background listener: at / cron / launch (wake self = `ava.agents.send_message`)

### Self & User
- [[self.ava.okf.md]] — Agent self (**core** ava.self): AGENT_ID / MACHINE_SPEC / SELF_MACHINE_NAME / pause_heartbeat / compact / restart / terminate; `log` / `set_label` / `get_label` are separately injected by ava_fleet plugin
- [[ui.ava.okf.md]] — User interface: serve / notify / show / close
- [[ava_builtins/plugins/ava_memory/memory-api.ava.okf.md]] — Long-term memory pool: semantic search
- [[ava/skills.ava.okf.md]] — Skill registry: ava.help(ava.skills.<name>)
