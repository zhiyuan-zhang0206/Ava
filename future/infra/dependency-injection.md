# Dependency injection — target shape and migration

Goal: a function's dependencies are in its signature or its constructor, never
reached through a module global. The direction, the rule that separates what is
injected from what may stay global, and the alternatives rejected are in the
[decision record](../../decisions/2026-10-02-dependency-injection-direction.md);
this page is the target form and what is left. Nothing below is built yet. The
lint rule that holds the line is a separate change and is described here only by
its semantics.

It is the work the other infra pages point at: the
[test patch audit](test-patch-audit.md) counts the tests' global-environment
patches (class E) that this removes at the source, and [locality](locality.md)
supplies the package doors and the shrink-only baseline practice the enforcement follows.

## Where the code is

- **`Settings`** aggregates per-domain sub-models in `base/config/` and is imported
  everywhere. Process profiles (gateway, agent, runner) choose which domains a
  process constructs, and a boot-lite layer serves a small field subset until the
  first read outside it. `AVA_HOME` is read at each call by the path helpers.
- **Per-agent config** resolves as `config_overlay > birth_config > cluster
  default` ([per-agent config lifecycle](../../decisions/2026-07-31-per-agent-config-lifecycle.md)).
  The host turns the two stored maps into pins and binds them in a ContextVar
  around each turn (`base/config/turn_view.py`); plugin config and the turn
  identity are bound the same way.
- **`AvaContext`** (`base/agents/context.py`) is LangGraph's per-run context. The
  host builds one per turn in `services/agent_host/host.py` with the handles (model,
  event publisher, database pool); its string-level config fields default to live
  `settings` reads.
- **One compiled graph** is shared by every agent in a host
  ([agent runtime](../../agent/docs/agent-runtime.ava.okf.md)).

## Target shape

### 1. Configuration is owned by the component that reads it

Each component (a package, or a coherent part of one: the LLM client, the claim
path, the delivery watchdog) defines a frozen config type that holds exactly the
fields it reads and takes it in its constructor or factory. A field read by two
components appears in both types, or, when that proves common, in a type owned by
the lower of the two; the field-ownership survey decides.

`Settings` becomes a catalog. It collects every package's config type so that the
config panel, `ava config set`, the `.env` writer and the per-field metadata
(scope, restart requirement, per-agent lifecycle) keep working. It is read by
tools that enumerate fields, not by code that needs a value.

### 2. Config types live in their package

[The 2026-07-19 decision](../../decisions/2026-07-19-config-ownership-decomposition.md)
kept every sub-model in the base layer because readers imported the aggregate
from the lowest layer. Under injection a component imports only its own type, and
the composition root, which sits above all packages, imports every type, so the
dependency points down the layering. `base` stops defining gateway and agent
fields.

### 3. One composition root per process

Each process kind (gateway, agent host, exec child, ops daemons, CLI verbs,
scheduled jobs) has one root. It is the only code that reads the environment,
`.env`, the gateway bootstrap response and configuration stored in the database
(overlays, birth config, cluster defaults). It builds the config objects, then the
handles and components in dependency order, and starts and stops them. All other
code reads no environment variable.

Values that are read lazily at call time today are read once at the root and passed
down:

- `home` and the paths derived from it come from one read of `AVA_HOME`;
- a `Platform` value carries the operating-system facts that module constants
  carry now;
- the clock and the id and random sources are handed to the components that decide
  with them.

### 4. Lifecycle is written into the type

- **Process.** Built at start and immutable until restart, which is the contract
  each field already declares through its restart requirement.
- **Agent.** A frozen `AgentConfig` built when the agent's runtime is constructed,
  merged from overlay, birth config and cluster default in the existing precedence.
  It splits into an identity part (the fields the registry marks frozen, such as the
  model, reasoning effort, injected skills, communication style and system-prompt
  extras) and an operational part. Only the identity type is accepted by the system-prompt
  builder, so a compaction that quietly rebuilds identity from live defaults, the
  failure that motivated the per-agent lifecycle decision, cannot be written.
- **Turn.** What exists for one turn: identity and incarnation, cancellation, the
  event publisher, the pools the host hands in.

All three ride in `AvaContext`, which LangGraph passes to every node as
`Runtime[AvaContext]` and the host builds once per turn. It replaces the ContextVar
pins for framework config, plugin config and turn identity; the views that read
them and the lint that polices their use are deleted at the end. Code a node calls
receives what it needs as arguments, where a ContextVar reached into everything
the task called.

Two limits shape this. Runtime context reaches graph nodes only, so ContextVars in
non-graph code need their own answer (open questions). And because the host shares
one compiled graph among all agents, closure or `functools.partial` binding at
graph-build time is valid only for process-level dependencies; agent- and
turn-level values must ride the runtime context.

### 5. Least privilege is structural

A secret appears only in the config type of the component that needs it. The
gateway's human bearer stays in a type only the gateway root constructs, and a
runner process never builds or imports it. Today a runtime filter enforces the same
fact (process profiles choose the domains a process constructs, checked by a guard
test); in the target it follows from what the root imports.

### 6. One serialization pair across processes

The environment is a transport between a parent and the processes it launches
(services, the agent host, exec children), not a source. One function turns config
objects into environment entries and one turns them back; the reverse is read only
in the child's composition root. Variable names stay as aliases of fields, so `.env`
files are unchanged. A parent builds its child's environment from its objects and
never forwards its own environment. A remote runner's root constructs objects from
the bootstrap response in the same way.

### 7. Wiring is plain construction

Constructors and factory functions, no container library. Process-level
dependencies reach graph nodes by closure or `functools.partial` when the graph is
built; agent- and turn-level values reach them through the runtime context.

### 8. Background duties are services; request paths start no tasks

- A periodic or event-driven background duty is its own service: an independent
  process supervised by `ava-root`, with its own health check, readiness proof and
  lifecycle. It does not stay inside the service that happens to need it.
- Its body is a sequential loop: scan, act, sleep. A round that has not finished
  prevents the next from starting, so single-flight holds by construction and no
  process-local dedupe dict is needed. Cool-down and retry state lives in durable
  storage, a database column, so a restart does not reset it.
- A round that must act on many agents at once opens an
  `async with asyncio.TaskGroup()` scoped to that round: bounded concurrency, every
  child awaited before the next round, and cancelling the loop at stop cancels the
  children. The lint does not report this. A free-floating `create_task` or thread
  remains a violation.
- **The delivery watchdog's three duties** (resurrecting terminated agents with pending
  chat, reaping crashed corpses that hold stuck chat, recovering stuck hosted turns) are
  one service with three resident sequential loops (ruling A'). Each loop is sequential,
  so single-flight holds by construction. One `TaskGroup` in the service's main function
  owns the three loops; if any loop crashes, the group cancels the others and the process
  exits for `ava-root` to restart it. Cool-down and failure counts live in the database.
  Every other background duty stays its own service.
- A side effect on a request path, such as the notification the gateway sends after
  a database commit, does not start a free-floating task. It is awaited in the
  request, or recorded durably (an outbox row) for a service's loop to consume.

Non-test code has about 70 `asyncio.create_task` sites and about 26
`threading.Thread(` sites, and no `TaskGroup`. The sets that hold tasks alive today
exist because asyncio keeps only weak references to tasks. Two examples: a
best-effort live-UI publish dispatched after a database commit in the gateway is not
awaited or cancelled at shutdown and is safe by a comment; the delivery watchdog's
single-flight and cool-down live in dicts that restart empty.

The costs of this split are open questions below, and so is how each existing duty
maps onto the rule: an audit of all background work is pending.

## Enforcement

The rules are those of the decision record. The mechanism is one structure lint
over the framework packages including `schedules/`, excluding tests, skill scripts
(run as programs) and `__main__` modules. It flags:

- module-level bindings that hold decision state: mutable containers, caches,
  handles, and values computed from an ambient read;
- import-time calls that read ambient state (environment, clock, working
  directory, random, ids, host facts);
- ContextVars and import-time registration;
- creation of a task or thread other than through a `TaskGroup` scoped to one
  round of a service loop.

A closed list of write-only facades (logger, meter, tracer) sits in one guard file
and changes only with a decision record. The baseline freezes every violation that
exists when the lint lands and can only shrink: a new violation fails, fixing one
deletes its entry, and the baseline is never regenerated upward. The limit is that
the lint and its baseline are files an author can edit; the edit is deliberate and
visible in review, not impossible.

Function-body reads of the global `settings` are not module state and the first
lint does not see them. Each slice below is locked by a second rule of the same
family (proposed): a module outside the composition roots may not import the
process-global `settings`, with a per-module shrink-only baseline.

## Migration order

1. **Lint and baseline.** Freezes today's violations, including the 14 log-throttle
   entries annotated as deliberate for this wave and the direct task and thread
   starts.
2. **Import-time configuration reads (44 sites).** Read the value at use instead of
   at import. It still reads the global, but it removes the import-order coupling
   the test fixtures guard and lets overrides reach the value.
3. **Composition roots, one process kind per change.** Each root takes over the
   environment, `.env`, bootstrap and stored-config reads for its process and
   constructs the objects. Import-time registries become explicit `register(...)`
   calls at the root, and `Platform` replaces the host-fact constants in the same
   step.
4. **Config slices by field ownership.** Per slice: define the component's config
   type in its package, build it at the root, take it in the constructor, make tests
   build the type instead of patching `settings`, and remove the slice's baseline
   entries and the second rule's entries. The slice list is not written yet: it
   waits for the field-ownership survey, and this page carries none.
5. **Turn context.** Move the config pins, plugin config and turn identity from
   ContextVars into `AvaContext` once the agent-level slices exist; delete the
   views and the lint that polices them.
6. **Background duties become services.** Driven by the audit result: each duty
   moves into a service with a sequential loop and durable cool-down and retry
   state, and a request-path side effect becomes an awaited call or an outbox row
   that a service consumes. Round-scoped `TaskGroup`s replace the direct task starts
   as each duty moves. How many services this adds depends on the cost questions
   below.
7. **Clear the baseline.** `Settings` is then the catalog. The allowlist of
   environment readers shrinks to the roots, and the boot-lite layer is removed if
   the hypothesis below holds. Completed items are deleted from this page.

## Open questions

- **Log throttling and alert configuration.** Not touched in this wave, and no
  `warn_once` facade is added. The judgment is that the warning and alert
  configuration is probably a self-made wheel; the candidate shapes if it is done
  are a pure reducer over events or event sourcing, not an object that holds state.
  A separate topic.
- **Slice partition.** The survey of which fields have one owner, which are shared,
  and which truly need to change at run time is in progress. The working
  expectation is that none changes under a live component: a change takes effect
  when the component is next constructed, matching each field's restart
  requirement. For live per-agent fields that means the agent's next runtime
  construction instead of every read; the survey must confirm nothing depends on
  the old behavior.
- **ContextVars outside the graph.** The gateway MCP client identity, the Redis
  auth-retry guard and the SDK telemetry frames cannot use runtime context.
  Candidate directions, not decisions: request-scoped dependencies for the
  gateway, an explicit parameter or owned object for a re-entrancy guard, and a
  recorder owned by the exec child's root for the SDK telemetry.
- **Cross-agent contamination audit.** About 25 entries are agent-scoped in meaning
  and process-wide in storage. Unkeyed buffers such as the security findings buffer
  are the first suspects. An audit is underway to decide which are live bugs; fixes
  do not wait for the migration.
- **Background-work audit.** A read-only audit covers all background work: what is
  persisted, what a mid-run kill leaves, and whether stop drains it. Its result
  fixes the order of step 6 and how each existing duty maps onto the rule. Pending.
  Work that is neither a periodic duty nor a request-path side effect (for example a
  drain worker owned by a per-turn object) is classified by the same audit.
- **Cost of splitting into services: memory.** A Python service process holds
  private memory on the order of 50 MiB today (measured with a warmed bytecode
  cache; the full measurement is in progress). Most of it is the import of
  third-party libraries, not this repository's code. A finer split therefore
  presupposes lowering per-process memory. No design is committed here. A read-only
  service-memory audit is in progress, and this page makes no reduction plan until it
  reports.
- **Cost of splitting into services: onboarding.** Adding a service today means
  editing `ops/roster/__init__.py`, `base/host/env/port_table.py` and
  `base/cluster/ports.py`, plus a health-check module under `services/healthchecks/`
  with its documentation and tests. A declarative roster could remove most of that
  boilerplate; it is not designed.
- **Plugins outside the repository.** A plugin installed outside the repository that
  reads `settings` directly cannot be found from here. What a plugin may read of the
  framework configuration, besides its own plugin-scope config, is undecided. No
  shim is carried: such plugins are updated when their runtime rolls out.
- **Where the catalog sits in the import layering,** since it must reach every
  package's config type, and whether the boot-lite layer is still needed. Boot-lite
  exists because constructing the whole `Settings` cost a measurable amount of
  memory in every exec child ([why](../../decisions/2026-09-16-config-boot-lite.md));
  a process that builds only the types it uses may not need it. That is an
  inference that has not been tested.
