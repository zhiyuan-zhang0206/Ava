# Dependencies are injected; a global is allowed only when nothing reads it to decide

## Context

`base.config.Settings` is an importable process-global. A function reaches
configuration by importing it, so what the function depends on is written in its
body, not its signature: about 950 read sites over about 500 fields. It is a
service locator, and it is not the only one. The same shape recurs in
module-level caches, registries, handles, clocks and identity holders. A one-off
AST survey of module-level bindings in the framework packages (`agent/`, `ava/`,
`ava_builtins/`, `base/`, `cli/`, `gateway/`, `ops/`, `services/`, `schedules/`;
tests excluded) classified 684 of them:

| Class | Bindings | What they are |
|---|---|---|
| Read to decide | 236 | config values, caches, registries, flags, handles, bookkeeping dicts, clocks, id and random sources, paths |
| Wiring | 245 | route and router tables, locks, ContextVars, registration calls, import side effects |
| Write-only sinks | 137 | loggers, meters, tracers and their internals |
| Constants and pure values | 53 | compiled patterns, code locations, memoized pure functions |
| Unclear | 13 | mostly sets that hold background tasks alive |

**Observed cost.** The test suite patches `settings` objects at about 1,000
sites. The root fixture must set 22 environment variables before any project
module is imported, and an assertion guards that import order, because module
level code reads them at import. 44 configuration values are computed at import
and frozen there, so a later override cannot reach them. One leaked `AVA_HOME`
failed about 150 tests at once. None of this is a testing problem; the tests are
the place where the hidden dependencies have to be made visible by hand.

**Root cause.** A module-level singleton is correct when the process is the
scope. That held when each agent ran in its own process: one agent's identity,
config, buffers and counters were the process's. The agent host replaced that
model. One host process serves many agents' turns as concurrent asyncio tasks
([agent runtime](../agent/docs/agent-runtime.ava.okf.md)), so every global that
is agent-level in meaning and process-level in storage is a possible cross-agent
leak. The repository has patched this one global at a time: the per-turn config
view and the plugin-config view bind overlays in ContextVars around each turn
(`base/config/turn_view.py`, `base/packages/plugins/config_view.py`), further
ContextVars carry the turn identity, and a lint forces turn-scoped code to read
through the overlay. Each patch makes a global look scoped while every reader
still has to know which of two paths is the right one. The survey found about 25
more entries of this kind. The clearest is the security findings buffer in
`ava/security.py`, a module-level list whose comment still justifies it with
"one agent per process".

Two earlier stances stand in the way and are reversed here. The `AvaContext`
module docstring records that threading the context through read sites was
judged not worth the blast radius. The
[config ownership decomposition](2026-07-19-config-ownership-decomposition.md)
split the aggregate by domain but kept every sub-model in the base layer and
rejected colocating config classes with their owners.

## Decision

**Dependencies are passed in.** Configuration, clocks, random and id sources, the
home directory and paths, host facts, database, Redis and HTTP handles, caches
and flags are given to the component that uses them; none is fetched from a
module global. The shape is chosen without regard to migration cost: leaving the
950 read sites in place makes every later change harder, so the migration is
sliced and tracked behind a baseline, not avoided. The target form is in
[dependency injection](../future/infra/dependency-injection.md).

**The line is whether a value is read to make a decision**, not whether it is
cross-cutting or vertical. Anything read to decide something is injected, with no
exception. Anything only written out (the logger, the meter, the tracer) may be a
process-global facade, because it cannot change the answer a function returns.
That list is closed: it lives in one guard file, and changing it needs a new
decision record.

**A structure lint holds the line.** A new violation fails the build; violations
that exist today are frozen in a baseline that can only shrink. The lint is a file
that whoever changes the code can also change. What it buys is that a violation
stops being a silent import and becomes a visible edit of the guard file.

Rulings that fix the boundary of the rule:

1. **A ContextVar is a violation, not a sanctioned carrier.** LangGraph already
   provides a per-run context: graph nodes receive `Runtime[AvaContext]`, which
   the host builds once per turn. Decision inputs of a turn (the agent's pinned
   config, the turn identity) travel there, not in ContextVars. ContextVars in
   code that does not run inside the graph (the gateway MCP client identity, the
   Redis auth-retry guard, the SDK telemetry frames) cannot use runtime context;
   their replacement is decided case by case and until then they sit in the
   baseline.
2. **Registries filled at import are violations.** The composition root calls an
   explicit `register(...)`. There is no "sealed after boot" exemption.
3. **Host facts are injected.** Facts such as the operating system become an
   injected `Platform` value instead of module constants. A pure function whose
   memoized result cannot change an answer (regex compiles, static tables) stays,
   from a closed list that names the reason for each entry.
4. **Background duties are services with sequential loops, and request paths
   start no tasks.**
   - Every periodic or event-driven background duty is its own service: an
     independent process supervised by `ava-root`, with its own health check,
     readiness proof and lifecycle. It does not stay inside the service that
     happens to need it.
   - A duty's body is a sequential loop: scan, act, sleep. A round that has not
     finished prevents the next from starting, so single-flight holds by
     construction and no process-local dedupe dict is needed. Cool-down and retry
     state lives in durable storage (a database column), so a restart does not
     reset it.
   - A round that must act on many agents concurrently uses an
     `async with asyncio.TaskGroup()` scoped to that round: bounded concurrency,
     every child awaited before the next round, and cancelling the loop at stop
     cancels the children. This is allowed and the lint does not report it. A
     free-floating `asyncio.create_task` and a free-floating thread remain
     violations.
   - The delivery watchdog's three duties (resurrection, reaping, hosted-turn
     recovery) are decided as ruling A': one service with three resident sequential
     loops, so single-flight holds by construction. One `TaskGroup` in the service's
     main function owns the loops; a crash in any loop exits the process and
     `ava-root` restarts it. Cool-down and failure counts live in the database.
     Rejected: one phased loop (the slowest RPC delays all three duties) and three
     services (three more processes of private memory, health checks and
     onboarding boilerplate). Every other
     background duty stays its own service.
   - A side effect on a request path (for example the notification the gateway
     sends after a database commit) does not start a free-floating task. It is
     awaited in the request, or written as a durable record (an outbox row) that a
     service's loop consumes.
   - The same lint locks the rule, with a shrink-only baseline. The facts behind
     it: non-test code has about 70 `asyncio.create_task` sites and about 26
     `threading.Thread(` sites, and no `TaskGroup`. The gateway's best-effort
     live-UI publish after a database commit is not awaited or cancelled at
     shutdown, and its safety rests on a comment. The delivery watchdog keeps its
     single-flight and cool-down in process-local dicts that restart from zero.
   - A read-only audit of all background work (what is persisted, what a mid-run
     kill leaves, whether stop drains it) is in progress and decides how each
     existing duty maps onto this rule; this record states no conclusion from it.
     The cost of splitting is tracked as open questions in the
     [plan](../future/infra/dependency-injection.md).
5. **Agent-scoped state must not be held process-wide.** A keyed dict is still
   ambient state and stays a violation; an unkeyed buffer shared between agents is
   a cross-agent bug. The survey's roughly 25 entries are audited for actual
   cross-agent effect separately, and the comments that still describe one agent
   per process are corrected with the fixes.
6. **Configuration is injected as narrow slices.** A component receives a frozen
   config type holding only the fields it reads, never the whole `Settings`. A
   field-ownership survey (which of the fields have one owner and which are
   shared) comes before any slice is cut.
7. **All rules apply from the first day of the lint.** Import-time calls that read
   ambient state are included, and `schedules/` is in scope. Test code and skill
   scripts are out of scope: a skill script runs as a program and is not an
   importable module.
8. **Log throttling and alert configuration are not touched in this wave.** No
   `warn_once` facade is added. The 14 throttle and dedupe entries stay in the
   baseline, annotated as deliberate. The judgment behind it is that the warning
   and alert configuration is probably a self-made wheel; if it is revisited, the
   candidate shapes are a pure function over events (a reducer) or event
   sourcing, not an object that holds state. That is a separate topic.

## Alternatives rejected

- **Give `Settings` invalidation hooks or a generation counter so the global can
  be reset in tests.** It makes the global look testable and leaves the
  dependency direction untouched: readers still reach for the world, the
  agent-level leak remains, and every new reader adds another hidden edge.
- **Inject everything, the logger included.** About 785 logging call sites would
  carry a parameter that can never change what the function decides. A rule that
  taxes every call site is the rule most likely to be bypassed, with exactly the
  module-level shortcuts it exists to stop. A narrow rule with a closed list can be
  checked mechanically and gives nobody a reason to evade it.
- **Thread configuration down as parameters, layer by layer.** Passing the whole
  object (or a bag of values) through every function moves the world-dependency
  into each signature and forces every intermediate layer to know every field.
  Ownership belongs at the component boundary: the constructor receives the
  component's slice and the layers in between do not see it.
- **A dependency-injection container library.** It adds a second wiring language
  and resolves dependencies at run time, which hides the dependency graph from a
  reader and from the type checker; that is the service locator again. Plain
  constructors and factory functions keep wiring greppable, and LangGraph already
  supplies the per-run context. It also fails the small-core principle.
- **Keep ContextVar as the per-turn carrier.** It works, and it is how the pins
  reach turn tasks today, but it is an ambient channel that any code can read
  without declaring it, and tasks copy it implicitly into everything they spawn.
  The graph's runtime context is explicit and already built per turn.
- **Exempt registries once they are sealed after boot.** It leaves import-order
  dependence in place and needs a seal state that every registry must remember to
  set.
- **Hold background tasks long-term in a component-owned `TaskGroup` inside the
  service that owns the component.** It keeps the process count down, but every
  such task then needs its own start, drain-on-stop, durability and health proof
  inside a host not built around it, and single-flight needs process-local state
  again. An independent service with a sequential loop answers those once, through
  the supervisor, and makes single-flight a property of the loop. The price is one
  process per duty, recorded in the plan's open questions.
- **Add a `warn_once` facade for the throttle entries.** It would turn a
  write-only concern into a stateful shared object this wave and entrench the
  home-made alerting configuration before anyone has judged it.

## Consequences

- Two read paths coexist until the migration ends: the global `settings` with its
  per-turn views, and injected configs. The views stay until their last reader
  moves.
- Tests construct the config type they need instead of patching a global; the
  import-order assertion and most of the environment pinning disappear as slices
  land.
- The 2026-07-19 decision's rejection of colocating config classes with their
  owners is reversed (a forward link is added to that entry), and the `AvaContext`
  docstring changes with the migration.
- A plugin that imports `settings` directly breaks when its field moves. The
  repository carries no compatibility shim; plugins installed outside it are
  updated when their runtime rolls out.
- The baseline starts large: the survey's read-to-decide bindings, the
  ContextVars, the import-time registries and the direct task and thread starts
  are frozen in it unless they are fixed first. It is the standing form of the
  survey, and reducing it is the migration's progress measure.
- Existing background duties move into services, or into outbox rows consumed by a
  service's loop, as the audit identifies them; the 12 sets that hold tasks alive
  and the direct task and thread starts are baselined until then. Each new
  service costs a supervised process and onboarding work; both are open in the
  plan.
- The throttle and dedupe entries (14) stay frozen as deliberate residue.

- Forward: `decisions/2026-10-04-ava-state-and-context.md` makes one ContextVar a sanctioned exception to ruling 1: the exec child has no LangGraph runtime to carry the per-run context, so the process's `AvaContext` is read through a single ContextVar (`ava.sdk_surface.process_context`), the way `get_runtime()` does it.
