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

SDK operations read configuration from the `ConfigAuthority` retained by their
existing plugin installation. Web requests, understanding calls, neighbor
defaults and shell capture defaults retain their operation-time reads; changing
that installation's configuration affects subsequent reads. Explicit arguments
still take precedence over defaults. A claim-side security scan receives its
policy from the agent's own slices, while SDK scans use the installed agent
configuration. Missing required installation inputs fail at their first use.
Default SDK clients capture startup's delivered environment at their first
configuration read. Bare plugin installation uses the same read-only capture;
neither operation repeats dotenv delivery or changes the process environment.
Explicit process roots retain their supplied configuration owner and overlays.
Timestamped SDK operations obtain a fresh clock from their bound context, or
from the installed process factory when no context is bound. Neither binding
evaluates the factory; a partial owner without one refuses a clock operation.

Gateway retry counts, each retry delay, and the default memory-search deadline
come from live readers configured on that context's existing client owner.
An explicit host context uses its own inputs without requiring an SDK
installation. The transport input object performs no reads at construction
and owns no second HTTP connection.

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

An SDK installation retains its root-supplied Clock factory without reading it
at installation. A bound context owns Clock operations; a partial context
missing that capability fails explicitly rather than borrowing the installation.
An unbound installed SDK uses its retained factory. `ava.loaded_code_image()`
returns the immutable first-load code fact and does not reread a moved checkout
on reload or late attachment.

SDK recorders capture the installation's lazy event producer, or the admitted
caller's ClientSet producer together with identity, tally and durable capture.
Changing context inside a call cannot redirect that event. Sampling happens
before producer construction; durable capture precedes enqueue. An unbound
Python caller remains a system caller and uses the installation's retained
producer, without creating an AvaContext. Reload preserves that producer.
