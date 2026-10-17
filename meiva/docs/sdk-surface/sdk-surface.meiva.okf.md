---
type: doc
title: SDK Surface — meiva.* Tool Overview (Index)
description: 'An agent has only one tool—`execute_code(code: str)`—but obtains all capabilities through the `meiva.*` Python namespace. Each submodule corresponds to a separate concept file.'
tags: []
---

# SDK Surface — meiva.* Tool Overview (Index)

An agent has only one tool—`execute_code(code: str)`—but obtains all capabilities through the `meiva.*` Python namespace. Each submodule corresponds to a separate concept file.

`meiva.help()` reads current module source for constant annotations and attribute
documentation. Reloading a plugin in place updates this view; unavailable source
is retried on the next discovery instead of being retained as an empty result.

The SDK's pytest parent import boundary is documented in [[test-imports.meiva.okf.md]].

## Module Index

SDK operations read configuration from the `ConfigAuthority` retained by their
existing plugin installation. Web requests, understanding calls, neighbor
defaults and shell capture defaults retain their operation-time reads; changing
that installation's configuration affects subsequent reads, while explicit
arguments take precedence. A claim-side security scan receives its policy from
the agent's slices; SDK scans use the installed agent configuration. Missing
required installation inputs fail at first use. A launched script captures
startup's delivered environment once when its first parent import composes the
context and plugin installation; neither cold client owner repeats dotenv
delivery, mutates the environment, or opens connections at construction.
Explicit process roots retain their supplied configuration owner and overlays.

Gateway retry counts, each retry delay, and the default memory-search deadline
come from live readers on that context's client owner. An explicit host context
uses its own inputs without requiring an SDK installation; the transport input
object performs no reads at construction and owns no second HTTP connection.

`base.agents.context.gateway_client` owns transport, launch-retry receipts and
memory-search results. Its HTTP operations require the caller's `MeivaContext` and
never select an SDK process context. The Meiva gateway entry resolves the process
context for typed operations, `get` and `use_client`; an explicit context takes
precedence for `get`, and a shared host must supply one. Borrowed identity
jitter revalidates its lease before using the agent ID.

### Files & Shell
- [[files.meiva.okf.md]] — File read/write: read / write / edit / delete / glob / append
- [[shell.meiva.okf.md]] — Shell commands: run() / run_background() (auto-report on completion) + sessions (new / send / capture / kill)

### Agent Interop
- [[meiva/impersonation/docs/attachment.meiva.okf.md]] — local external Python attachment to a consented agent lease
- [[meiva/agents/docs/agents.meiva.okf.md]] — spawn / fork / send_message / terminate / resurrect / get_neighbors / get_ancestors / get_status
- [[meiva_builtins/plugins/meiva_fleet/docs/tasks/tasks.meiva.okf.md|Tasks]] — Task registry `meiva.tasks`: create / get / list / update / log (injected by meiva_fleet plugin, not core SDK—docs in fleet subtree)

### Tools & External
- [[meiva/mcps/docs/mcps.meiva.okf.md]] — MCP tool servers (determined by config; built-in chrome + servers installed via `meiva mcp install` beyond core)
- [[meiva/web/docs/web.meiva.okf.md]] — Web access: search + fetch (concurrent)
- [[meiva/understand/docs/understand.meiva.okf.md]] — Multimodal understanding primitive: understand(targets) → list[str], each target carrying prompt + text|paths
- [[meiva/watcher/docs/watcher.meiva.okf.md|watcher]] — Background listener: at / cron / launch (wake self = `meiva.agents.send_message`)

### Context

Execution bootstrap binds request identity, machine, clients and the
configuration owner's clock before loading plugin surfaces. External attachment
bootstrap supplies its own or borrowed machine, while the installation's
fallback event producer owns separate cold clients until process exit; detaching
closes only the attachment's clients. Surface loading and runtime faces upgrade
each happen once. Agent-launched bare scripts complete context and plugin
composition before the first parent import returns, so core and plugin
from-imports obtain installed interfaces and re-importing retains them. Formal
execution children initialize after request validation, attachments after lease
validation, and hosts keep explicit caller contexts.
- `meiva.context` — the `MeivaContext` this process runs as: `meiva.context.identity` (`agent_id`, `owns_loop`, `actor`). The exec child rebuilds the host's context from the exec request envelope before plugin loading and user code; a launched script derives it from `MEIVA_AGENT_ID`. It raises `AttributeError` in the agent host and bare scripts, as `meiva.state` does outside an exec turn. `meiva.context.sql` / `.redis` / `.gateway` are lazy connections released at process exit (`meiva.DB` / `meiva.REDIS` alias them). Free functions such as `meiva.agents.*` read identity and clients from this entry across all threads without a ContextVar. The shared host supplies dependencies explicitly and never binds a current agent to the SDK module. Context and exec-turn state live in `meiva/sdk_surface/process_context.py` (`meiva.context` / `meiva.state` / `meiva.state_update` delegate there across reloads). `meiva/boot.py` builds lazy clients (`process_clients`), launched-script context, and plugin load (`ensure_plugins_loaded`, `initialize_context`); its boot entry fixes a shared-host purpose before plugins or graph work so inherited `MEIVA_AGENT_ID` cannot turn it into a launched child or allow attachment, and context bind/release cannot clear that purpose. An external controller may bind one attachment and restores the prior context on detach. Lifecycle writes with an omitted source require an established actor before HTTP.

### Self & User
- [[meiva/self/docs/self.meiva.okf.md|self]] — Agent self (**core** meiva.self): AGENT_ID / MACHINE_SPEC / attach / pause_heartbeat / compact / restart / terminate; `set_label` is separately injected by meiva_fleet plugin
- [[meiva/docs/ui.meiva.okf.md]] — User interface: serve / notify / show / close
- [[meiva_builtins/plugins/meiva_memory/docs/memory-api.meiva.okf.md]] — Long-term memory pool: semantic search
- [[meiva/skills/docs/skills.meiva.okf.md]] — Skill registry: meiva.help(meiva.skills.<name>)

Installation lifetime — the retained clock factory, event producer and the
atexit owner that `uninstall()` removes — is in [[install-lifetime.meiva.okf.md]].

The Fleet launched-boot probe uses a fresh interpreter with environment and
cluster configuration pinned before its first `import meiva`. It checks one boot
read, preserved context/installation identity on reload, and that the listed
SQL, Redis, Gateway and event clients plus the sampler remain unstarted. This
is a lazy-resource boundary, not a claim that imports have no side effects.
