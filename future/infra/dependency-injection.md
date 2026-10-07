# Dependency injection — target shape and migration

Goal: a function's dependencies are in its signature or its constructor, never
reached through a module global. The direction, the rule that separates what is
injected from what may stay global, and the alternatives rejected are in the
[decision record](../../docs/decisions/2026-10-02-dependency-injection-direction.md);
this page is the target form and what is left. Built so far: the ambient-state lint
and the first configuration slices ([Config slices](#config-slices-first-batch-done));
the rest is target form.

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
  default` ([per-agent config lifecycle](../../docs/decisions/2026-07-31-per-agent-config-lifecycle.md)).
  The host turns the two stored maps into pins (`base/config/agent_pins.py`) and
  resolves them, with the plugin pins, into the agent's `AgentSlices` each turn; only
  the turn identity is still bound in a ContextVar.
- **`AvaContext`** (`base/agents/context/__init__.py`) is LangGraph's per-run context. The
  host builds one per turn in `services/agent_runner/agent_host/host.py` with the handles (model,
  event publisher, database pool, `Database`, `EventBus`) and the agent's resolved
  `AgentSlices` (`base/host/env/agent_slices.py`); graph code reads
  `runtime.context.agent`.
- **One compiled graph** is shared by every agent in a host
  ([agent runtime](../../agent/docs/agent-runtime.ava.okf.md)).

## Target shape

### 1. Configuration is owned by the component that reads it

Each component (a package, or a coherent part of one: the LLM client, the claim
path, the delivery watchdog) defines a frozen config type that holds exactly the
fields it reads and takes it in its constructor or factory. A field read by two
components appears in both types, or, when that proves common, in a type owned by
the lower of the two; the field-ownership survey decides.

`Settings` stays the flat field registry and nothing more: the single source of the
env, overlay and API surface, with field names and aliases unchanged and the per-field
metadata (scope, restart requirement, per-agent lifecycle). `base` holds no slice
directory. A slice is a frozen dataclass its owning package defines; each process
entry lists the slices it needs and builds them from the flat values in its
composition root, without pydantic, so boot-lite can use them. The package that owns a
slice names the fields under their flat names, so a root builds it by reading
`settings.<domain>.<field>` field by field.

### 2. Config types live in their package

[The 2026-07-19 decision](../../docs/decisions/2026-07-19-config-ownership-decomposition.md)
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

Function-body reads of the global `settings` are not module state. Each sliced package
is locked by a second rule of the same family, `settings-read`
(`scripts/structure/ambient_state/sliced.py`): a package that declares itself sliced
(`settings = [...]` in its own `ambient_roots.toml`, collected into `SLICED_PACKAGES`)
names its composition-root modules, and any other non-test module in it that imports
`settings`, `get_field`, `set_field`, `ensure_eager` or `base.config`
is a site. Sites are frozen in the same shrink-only baseline, so a package that cannot
finish in one change freezes what is left; a finished one has none.

## Migration order

1. **Lint and baseline.** Freezes today's violations, including the 14 log-throttle
   entries annotated as deliberate for this wave and the direct task and thread
   starts.
2. **Import-time configuration reads (44 sites).** Read the value at use instead of
   at import. It still reads the global, but it removes the import-order coupling
   the test fixtures guard and lets overrides reach the value. Done, with the
   `import-time-read` baseline keys deleted batch by batch: the service daemons, the
   gateway (schedule runner, SSE), the SDK gateway client, the claim database timeout,
   the clock lattice and the schedule templates (their timezone is
   `schedules.catchup.cluster_timezone()`). The `import-time-read` keys still in the
   baseline are clock, random and home-path reads (process start stamps, the recall-log
   key, the home captured at load, the tmpfs base, the SDK-disable switch), which the
   clock and `Platform` injection step takes.
3. **Composition roots, one process kind per change.** Each root takes over the
   environment, `.env`, bootstrap and stored-config reads for its process and
   constructs the objects. Import-time registries become explicit `register(...)`
   calls at the root, and `Platform` replaces the host-fact constants in the same
   step.
4. **Config slices by field ownership.** Per slice: define the component's config
   type in its package, build it at the root, take it in the constructor, make tests
   build the type instead of patching `settings`, and declare the package sliced in
   its `ambient_roots.toml`. Order: the slices no other package reads (31 in the survey)
   first, then the shared kernel and the secrets. The IM bridge batch is done
   (below); the other slices of that kind follow the same pattern.
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

## Config slices (done)

Batches so far: the IM bridge daemon, then the events-maintenance and labeler daemons
(below). Each package declares itself sliced in its `ambient_roots.toml` with its daemon as the root.

### IM bridge

The IM bridge daemon: three slices, 21 fields read across eight modules, all in one
process (the gateway-side `services.entrypoints.im_bridge.daemon`), none read by another package.

- `ImBridgeConfig` (retry delays, push backoff, SSE timeout, timeline and replay windows,
  notice limits, disabled adapters), `TelegramCredentialsConfig` and
  `FeishuCredentialsConfig` (credentials and poll timing, plus the replay window the
  Feishu cursor needs) live in `services/entrypoints/im_bridge/config.py`. Field names are the flat
  registry names. Secrets sit in their own slices, so the Telegram token is not in the
  bridge core's type.
- `services/entrypoints/im_bridge/daemon.py` is the composition root and the only module there that
  reads `settings`. Its `im_bridge_config()`, `telegram_config()`, `feishu_config()` and
  `gateway_client()` build the objects; `run()` hands `IMBridgeCore(config, gateway)` its
  slice and its gateway client, and `_load_adapters` hands each adapter the slice it
  names (Weixin takes none). The gateway URL and the cluster secret reach `GatewayClient`
  as arguments from the root; the secret is not part of any slice.
- The adapter fallbacks that swallowed a missing `settings.feishu` domain are gone: a
  slice is always complete.
- Tests build slices with `services/entrypoints/im_bridge/tests/slices.py` (the daemon's builders plus
  `dataclasses.replace`) instead of patching `settings`; `test_im_bridge_daemon.py` pins
  that each slice field equals the live flat field of the same name and that `run()` and
  `_load_adapters` hand the slices down.
- The gateway profile still lists the `telegram` and `feishu` domains, because the root
  reads them. `telegram-send-file` keeps reading the bot token from the environment: exec
  is fully trusted and shares the runner's OS user, so moving the token out of the skill
  isolates nothing.

### Events maintenance and labeler

Two more gateway-side daemons, one root each, no reader outside the package.

- `EventsMaintenanceConfig` (`services/upkeep/events_maintenance/config.py`): the nine
  `events_*` daemon fields, plus `telemetry_loki_url` and `timezone`, which other
  components read too and so appear in this slice as well. `LabelerConfig`
  (`services/derived/labeler/config.py`): `labeler_model`, `labeler_max_chars`.
- Each `daemon.py` builds its slice (`events_maintenance_config()`, `labeler_config()`)
  and passes it down: loops, `compute_rollup`, `recover_observations`/`replay_loki`,
  `run_resolution_slice`, `run_blob_vacuum(timezone=)`, `generate_label_async`. The
  Loki readers and the I/O seams take the slice instead of reading `settings`. The
  operator CLI `observed_metrics.main()` gets its slice from the daemon's builder, so
  the daemon stays the package's only root.
- Tests use `tests/slices.py` of each package; a `test_*_config.py` per package pins
  that every slice field equals the live flat field and that `run()` hands the same
  slice to the loops.
- Left for later: `services/backup/scheduler` (`backup_hour` is also read by
  `services/backup/dump.py` and `base/host/system/walg_job.py`), and `memory_indexer` (read by ops and the CLI).

### Computer use, page server and memory search

Three more daemons, one slice each, each package declaring itself sliced:

- `ComputerUseConfig` (`services/desktop/computer`, root `mcp_daemon.py`): lease, queue timeout and
  session idle, taken by `ComputerMcpDaemon`.
- `PageServerConfig` (`services/agent_runner/page_server`, root `daemon.py`): the poll interval and the
  live-event channel, threaded through the reconcile pass to the PageClosed publication.
- `MemorySearchConfig` (`services/derived/memory_search`, root `daemon.py`): pidfile, data directory,
  port and the batch bound `build_app` takes. The embedding provider still comes from the
  memory-indexer factory, which three consumers share (gateway router, bring-up, this
  daemon) and which waits for the shared-kernel batch.

Not sliceable yet, found by re-scanning the code: `delivery_watchdog`, `ttl_reaper` and
`schedule_manager` are being reworked; `memory_indexer` and `cli/commands/cluster` have
several entries; the physical-backup package the first survey listed no longer exists.

## Shared kernel: handles, not configuration (in progress)

The kernel (database, event bus, service endpoints, clock, cluster secret) is read by too many
components to slice by owner. The rule for it: **a component receives a handle, not the config**.
Components that need Postgres want a connection, not the URL (which carries the login), so
`DbConfig` is read only by the constructor of the handle. The same shape follows for the event bus
and the clock; the endpoint table is indexed by service name, a daemon taking only its own row.

- **Done, the database** (`base/db`): `DbConfig` (`config.py`) is built from the live settings in
  one place; `Database` (`handle.py`) binds one config to `connect`, `pool`, `async_pool`,
  `direct_url` and `write_transaction`, and a root builds it with `Database.from_settings()`. The
  module-level `base.db.connect()` / `pool()` / `async_pool()` / `direct_db_url()` remain as a shim
  that builds the same dial at each call; `base.db.transaction.write_transaction` takes the pool
  it borrows from.
- **Held by a rule, package by package**: the `ambient-db` rule of the ambient-state gate bans the
  shim in the packages listed in `DB_HANDLE_PACKAGES` (`scripts/structure/ambient_state/
  allowlist.py`), and bans `Database.from_settings()` outside the roots named there. Listed so far:
  labeler, page server, events maintenance, IM bridge. A package joins when its
  last ambient dial is gone, in the same change; the shim is deleted with the last package.
- **Done, the endpoint table** (`base/daemon/endpoints.py`): `ServiceEndpoints.from_settings()`
  builds one row per health daemon (`ServiceEndpoint`: name, healthz port, pidfile under
  `$AVA_HOME/run`) from the fixed port table, the unit's `AVA_<NAME>_HEALTH_PORT` override and
  `AVA_HOME`. A daemon root takes its own row (`.of("labeler")`) and hands the port to
  `start_health_server`, whose port is a required argument. The old lookup functions
  (`health_port`, `pid_path`) are gone. The `ambient-endpoint` rule fails `ServiceEndpoints.
  from_settings()` outside the modules named in `ENDPOINT_PACKAGES`: every daemon root, and the
  entry points of the commands and probes that read a daemon's row (agent pause, cluster pause,
  lifecycle, cluster status, roster, IM alert delivery, machine registration). Those entry points
  are their own roots; threading the table through them is not done.
- **Done, the event bus** (`base/events/live/bus.py`): `EventBusConfig` (Redis URL, events
  channel) is read once, by `EventBus.from_settings()`; the handle gives `async_redis()` (one
  shared client per event loop), `open_async_redis()`, `sync_redis()` and the never-raise
  `publish_best_effort` / `publish_best_effort_sync` on the events channel (or a named one). The
  transport (resilience kwargs, auth retry, publish discipline) stays in `redis_client`, which
  no longer reads the settings; `get_async_redis`, `sync_redis` and the module-level publishes
  are gone. The announce hints (`announce.publish_*`) and the ops lifecycle event publishes take
  the bus as their first argument. A daemon root builds one bus and passes it down; the gateway
  builds `app.state.bus` in the lifespan and its handlers read it from `request`. The
  `ambient-bus` rule fails `EventBus.from_settings()` outside the modules named in `BUS_PACKAGES`
  (the daemon roots of agent host, delivery watchdog, heartbeat, page server and TTL reaper, the
  CLI health probe, and no root at all in `gateway/events`, `gateway/alerts`, `gateway/routers`).
  Library code (`base/agents`, `ops`, `agent/ownership`, the fleet plugin) builds its own bus at
  the call; threading one through those call chains is not done.
- **Done, the clock** (`base/clock`): `ClockConfig` (the cluster timezone name, the authoritative
  name when this process holds one, the message-timestamp weekday flag) is read once, by
  `Clock.from_settings()`; the handle gives `timezone`, `authoritative_timezone`, `zone()` (None =
  the host-zone fallback signal of a settings-lite process), `explicit_zone()`, the injectable
  `now()` and the one agent-facing `format_timestamp` / `now_timestamp`. `base.config.cluster_tz`,
  `format_timestamp` and `now_timestamp` are gone; `apply_cluster_timezone` and `host_tz_name`
  stay in `base.config` (they act on the process or the host, not on a handle). The `ambient-clock`
  rule fails `Clock.from_settings()` outside the roots named in `CLOCK_PACKAGES` (the schedules
  command and the cluster-status probe); libraries build their own clock at the call. The
  endpoint, bus and clock rules share one shape (`scripts/structure/ambient_state/rootrule.py`).
- **Done, the cluster secret** (behavior change, `docs/decisions/2026-10-03-cluster-secret-contraction.md`):
  outside the gateway, the CLI and the root, no component holds the human secret. im_bridge
  presents its machine API token and accepts the write generation's tokens on `/send`
  (`base.cluster.machine.daemon_acceptance`, shared with the ops server); the callers of `/send`
  send `gateway_auth_headers()`; the heartbeat's station probe reads the telemetry token from a
  private file the gateway home's start writes. A remote-managed data plane keeps the human secret
  (it issues no token), in `daemon_acceptance` and `gateway_bearer` only.
- **Root-local bundles**: a root may gather what it wires into a frozen dataclass marked
  `base.wiring.root_bundle`. The `bundle-leak` rule fails any annotation of such a class outside
  its defining module, so the bundle stays a local variable of the root and never becomes a
  parameter type (a function handed the whole bundle can reach any member).
- **Not yet**: the agent-side per-turn slices carried in `AvaContext`.

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
  and process-wide in storage. Unkeyed buffers are the first suspects (the security
  findings and skill-invocation buffers are gone; the attachment transport remains). An audit is underway to decide which are live bugs; fixes
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
  `base/cluster/ports.py`, plus a health-check module under `services/supervision/healthchecks/`
  with its documentation and tests. A declarative roster could remove most of that
  boilerplate; it is not designed.
- **Plugins outside the repository.** A plugin installed outside the repository that
  reads `settings` directly cannot be found from here. What a plugin may read of the
  framework configuration, besides its own plugin-scope config, is undecided. No
  shim is carried: such plugins are updated when their runtime rolls out.
- **Whether the boot-lite layer is still needed.** Boot-lite
  exists because constructing the whole `Settings` cost a measurable amount of
  memory in every exec child ([why](../../docs/decisions/2026-09-16-config-boot-lite.md));
  a process that builds only the types it uses may not need it. That is an
  inference that has not been tested.
