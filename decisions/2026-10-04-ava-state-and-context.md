# `ava.state` is the graph state, `ava.context` is the runtime context, the plugin wiring is a value

## Context

The `ava` package is a namespace of free functions that agent code calls inside an exec child, a
process the agent host spawns for one `execute_code` call. Free functions cannot be handed
anything, so the package grew module-level holders: the agent identity (`_agent_id`, `_owns_loop`,
`_actor`, the external-controller slots), the lazy `ava.DB` / `ava.REDIS` connections, the gateway
HTTP client, the MCP event loop with its sessions, the attachment buffer, the active external
attachment, the plugin-load latches, the wrap layers, the metering ledgers, the skill-source
providers and the render ContextVars. About forty `ambient_state` baseline entries under `ava/`
were one of these.

They are of three different kinds, and the package treated them as one.

1. **What must persist across a turn.** The plugin state, an attachment, a finding. It already had
   a home: the LangGraph graph state. The exec node snapshots it into the request envelope, the
   child writes a delta (`ava.state_update`), the node commits it as `Command(update=...)` through
   the reducers, and the checkpoint carries it ([hooks operate on the graph state](2026-10-04-hooks-operate-on-graph-state.md)
   made `ava.state` exist only in the exec child).
2. **What a process depends on and never persists.** Who it acts as, and the connections it holds
   (PG, Redis, the gateway client, the MCP clients with their loop and sessions). LangGraph has a
   home for this too: the Runtime context. The host already builds an `AvaContext` per turn and
   hands it to every node as `Runtime[AvaContext]`. The exec child had no such object and rebuilt
   the same facts out of module globals and the environment.
3. **How the SDK is assembled in a process.** The plugin wraps, the skill-source providers, the
   metering recorders, the SDK-disable entries, the plugin-load latches, and the import-time calls
   that fill them. It is neither of the above: it is not read to run a turn, it is the shape of the
   SDK itself, written once when plugins load.

## Decision

1. **`ava.state` / `ava.state_update` are the graph state's bridge; only what persists goes
   there.** Unchanged in mechanism, and now the only route for anything that must outlive the
   exec child: an output the child produces for the turn (an attachment, a finding) travels back
   as a state update, not as a buffer the child drains at exit.

2. **`ava.context` is the Runtime context's projection into the exec child.** It is the same
   `AvaContext` type the host builds, holding everything non-persistent a process depends on:
   identity (`agent_id`, `owns_loop`, `actor`, an external attachment's lease) and the connections
   (database, Redis, gateway client, MCP clients). The host puts `AvaContext.describe()` in the
   exec request envelope: serializable, and secret-free (identity, endpoints and references,
   never a password or token). The child builds its instance with `from_description()` and
   resolves credentials itself from its own settings and environment, as it did before. Clients
   are created lazily and die with the process.

3. **One ContextVar is the in-process entry.** Reading the current context follows LangGraph's
   `get_runtime()`: a context variable, `ava.sdk_surface.process_context._CURRENT`. The exec
   child, a script an agent launched (identity from `AVA_AGENT_ID`, `owns_loop=False`), a
   gateway-hosted schedule runner (an actor, no agent) and an external attachment (its lease) bind
   it; the agent host does not, because it serves many agents and its identity is the turn
   contextvar. Where nothing is bound, `ava.context` raises `AttributeError`, as `ava.state` does
   outside an exec turn.

4. **The SDK assembly is an immutable installation.** `install(registry)` produces one value
   holding the wraps, providers, metering state and disable entries; nothing outside it is
   written after plugin load. The single holder of the installation is the one remaining
   non-context entry in `ava/`.

5. **Render parameters are arguments.** `help.compact_classes` and
   `discovery.hidden_surface_members` are passed to the renderer, not set in ContextVars.

6. **`AvaContext` is light to import.** The exec child builds it at boot, so the module imports no
   psycopg, redis or langchain at runtime: the handle types are annotation-only and the plugin
   registry is read through `plugin_registry()`.

Agent-visible behavior: `ava.context` exists (documented: identity, read-only; connection objects
are provided by the framework). The `ava.*` free functions keep their call shapes. `ava.context`
raises outside an exec child, launched script or attachment. Threads that agent code starts after
the context is bound carry it (see below).

## Alternatives rejected

- **Keep module slots, move them behind functions.** The holders would still be per-process state
  that every reader has to know the owner of, and the host cannot hold one per agent.
- **Put the connections in the graph state.** A connection is not serializable and not durable;
  checkpointing it is wrong and excluding it from the checkpoint is a second mechanism.
- **Use LangGraph's own contextvar in the child.** It would put langgraph on the exec child's
  boot path, which the child keeps off on purpose, for a runtime the child does not run.
- **Pass the context to every `ava.*` function.** It would change every call shape the agent
  writes and every plugin wrapper; the free-function surface is the contract.
- **Bind the context in the agent host too.** The host is many agents in one process; a bound
  context there would be one agent's, and the turn contextvar already answers per task.
- **A process-wide fallback beside the ContextVar** (a module holder the thread case reads). It is
  the global being removed. A thread starts with an empty context, so `process_context` makes the
  threads started after `bind_process` carry this one variable (not the others: the host's turn
  identity keeps its rule that a thread starts empty).
- **Pass the identity as a plain agent id in the envelope.** It keeps working until a second
  identity fact (an actor, a lease) is needed; the context is where those belong.

## Consequences

- The `contextvar` rule's exception list gains `process_context._CURRENT`; the rule's statement
  that a ContextVar is a violation stands for everything else.
- `ava.agent_identity` keeps its functions (`agent_id()`, `require_actor()`, ...) as readers over
  the bound context and holds no state; `establish` / `establish_actor` are gone. Tests pin an
  identity with `pin_agent(...)`, which binds a context; the `identity_restore` fixture puts the
  previous one back.
- The exec request envelope carries `context` (the description) instead of a bare agent id.
- A pool thread started before a context was bound does not see it; one started after does, with
  the value bound at `start()`.
- An external attachment borrows an identity by binding a context carrying its lease for its
  lifetime and putting the process's own back at detach.

## Ruling: a context comes only from a named source (2026-10-04)

There is no anonymous fallback: where none of the bindings below applies, `ava.context` keeps
raising rather than constructing an identity-less context. Outside the exec child, a context has
exactly four legitimate sources:

1. **The exec child** — built from the context description in the request envelope (PR1 of this
   work; shipped).
2. **The agent's own persistent shell and the processes it launches** — a script an agent runs
   in its shell is still that agent and has an agent id; it constructs a **complete**
   `AvaContext` (identity plus the database, Redis and gateway clients) from the identity its
   shell environment describes, so `ava.DB`, `ava.REDIS` and the gateway-backed `ava.*` calls
   work as in the child. The description is secret-free: credentials resolve from the process's
   own settings, exactly as in the child. **Not yet shipped** — PR2 adds this source, first
   verifying which identity and credential references the persistent shell's environment already
   carries and injecting the description at session creation where it is missing.
3. **Other coding agents (Claude Code, Codex, ...)** — expected to use the `ava` CLI; they do
   not carry an SDK context.
4. **Impersonation — the one exception**: constructing the impersonated ava agent's context
   through the existing impersonation lease / `ava.external.attach` path; the identity is the
   impersonated agent's.
