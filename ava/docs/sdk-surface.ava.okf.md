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
memory-search results. Its HTTP operations require the caller's `AvaContext` and
never select an SDK process context. The Ava gateway entry resolves the process
context for typed operations, `get` and `use_client`; an explicit context takes
precedence for `get`, and a shared host must supply one. Borrowed identity
jitter revalidates its lease before using the agent ID.

### Files & Shell
- [[files.ava.okf.md]] — File read/write: read / write / edit / delete / glob / append
- [[shell.ava.okf.md]] — Shell commands: run() / run_background() (auto-report on completion) + sessions (new / send / capture / kill)

### Agent Interop
- [[ava/impersonation/docs/attachment.ava.okf.md]] — local external Python attachment to a consented agent lease
- [[ava/agents/docs/agents.ava.okf.md]] — spawn / fork / send_message / terminate / resurrect / get_neighbors / get_ancestors / get_status
- [[ava_builtins/plugins/ava_fleet/docs/tasks/tasks.ava.okf.md|Tasks]] — Task registry `ava.tasks`: create / get / list / update / log (injected by ava_fleet plugin, not core SDK—docs in fleet subtree)
- [[presets.ava.okf.md]] — Configuration presets: list / get

### Tools & External
- [[ava/mcps/docs/mcps.ava.okf.md]] — MCP tool servers (determined by config; built-in chrome + servers installed via `ava mcp install` beyond core)
- [[ava/web/docs/web.ava.okf.md]] — Web access: search + fetch (concurrent)
- [[ava/understand/docs/understand.ava.okf.md]] — Multimodal understanding primitive: understand(targets) → list[str], each target carrying prompt + text|paths
- [[ava/watcher/docs/watcher.ava.okf.md|watcher]] — Background listener: at / cron / launch (wake self = `ava.agents.send_message`)

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
- `ava.context` — the `AvaContext` this process runs as: `ava.context.identity` (`agent_id`, `owns_loop`, `actor`). The exec child rebuilds the host's context from the exec request envelope before plugin loading and user code; a launched script derives it from `AVA_AGENT_ID`. It raises `AttributeError` in the agent host and bare scripts, as `ava.state` does outside an exec turn. `ava.context.sql` / `.redis` / `.gateway` are lazy connections released at process exit (`ava.DB` / `ava.REDIS` alias them). Free functions such as `ava.agents.*` read identity and clients from this entry across all threads without a ContextVar. The shared host supplies dependencies explicitly and never binds a current agent to the SDK module. Context and exec-turn state live in `ava/sdk_surface/process_context.py` (`ava.context` / `ava.state` / `ava.state_update` delegate there across reloads). `ava/boot.py` builds lazy clients (`process_clients`), launched-script context, and plugin load (`ensure_plugins_loaded`, `initialize_context`); its boot entry fixes a shared-host purpose before plugins or graph work so inherited `AVA_AGENT_ID` cannot turn it into a launched child or allow attachment, and context bind/release cannot clear that purpose. An external controller may bind one attachment and restores the prior context on detach. Lifecycle writes with an omitted source require an established actor before HTTP.

### Self & User
- [[ava/self/docs/self.ava.okf.md|self]] — Agent self (**core** ava.self): AGENT_ID / MACHINE_SPEC / attach / pause_heartbeat / compact / restart / terminate; `set_label` is separately injected by ava_fleet plugin
- [[ui.ava.okf.md]] — User interface: serve / notify / show / close
- [[ava_builtins/plugins/ava_memory/docs/memory-api.ava.okf.md]] — Long-term memory pool: semantic search
- [[ava/skills/docs/skills.ava.okf.md]] — Skill registry: ava.help(ava.skills.<name>)

An SDK installation retains its root-supplied Clock factory without evaluating
it at binding. Timestamped operations obtain a fresh clock from a bound context,
or from the installed process factory when unbound; a partial context missing a
clock fails explicitly rather than borrowing the installation.
`ava.sdk_surface.process_context.loaded_code_image()` returns the immutable
first-load code fact and does not reread a moved checkout on reload or late
attachment.

SDK recorders capture the installation's lazy event producer, or the admitted
caller's ClientSet producer with identity, tally and durable capture. Changing
context mid-call cannot redirect that event. Sampling precedes producer
construction; durable capture precedes enqueue. An unbound Python caller uses
the installation's retained producer as a system caller without creating an
AvaContext, and reload preserves that producer.
