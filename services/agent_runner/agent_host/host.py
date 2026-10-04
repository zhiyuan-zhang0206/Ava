"""Run every local agent through one shared host and graph.

The dispatcher owns per-agent single-flight and wake delivery. This driver binds
an admitted incarnation, resolves its framework/plugin configuration, and invokes
the graph until idle or native lifecycle return. The turn's identity is a
contextvar every graph node inherits; its configuration travels as its `AgentSlices`
on the graph context, and managed exec children receive the same pins through their
existing environment projection.

The daemon shares a workload pool, a separate control pool, one checkpointer
(keyed by thread_id), and one compiled graph. Graph construction loads process-
global plugin definitions, so compiling a graph per agent would corrupt concurrent
turns. Chat models and startup reconciliation are cached per agent and invalidated
by the stored birth/overlay configuration fingerprint. The llm node retries itself on
its agent's own schedule (`agent/graph/llm/_retry.py`).

Cold admission repairs claimed inbound/checkpoint disagreements and dangling tool
pairs, and establishes the workspace. A watcher (`ava.watcher.at/cron/launch`) is
just a shell session running a generated script — nothing here tracks or restarts
one (decisions/2026-09-27-watchers-are-never-restarted.md). No per-agent process
or global identity is created.

Native restart/terminate flushes the final checkpoint and applies its exact-owner
command before releasing single-flight. Normal maintenance waits for continuation
and managed-resource settlement. Explicit force cancellation stays fenced until
those resources close. Database outages retain the original task; recovery checks
its ownership before repairing and continuing, without creating a new inbound.

Each completed invocation flushes its checkpoint before linking it to the current
trace. Expected provider/compaction failures persist halted state, and their
settled abort reconciles the turn's claimed inbounds at once
(`host_abort_reconcile_enabled`), so no row waits for a next boot; unexpected
errors remain visible and propagate. Configuration rejection leaves pending work
durable for a subsequent scan after the configuration is corrected.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict
from datetime import datetime
from uuid import uuid4

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.graph.llm_errors import FatalLLMStreamError, FatalProviderError
from agent.graph.node_log import flush_node_exit_aggregate
from agent.hooks.compact import CompactionFailedError
from agent.impersonation import (
    active_lease,
    flush_checkpoint,
    native_status,
    settle_checkpoint,
    supervise_relay,
)
from agent.ownership.corpse_reap import reap_crash_corpses
from agent.ownership.hosted import (
    admit_hosted_runtime,
    apply_hosted_lifecycle,
    release_hosted_owner,
    renew_hosted_owner,
    settle_hosted_runtime,
)
from agent.process_boot import boot_agent_scope
from agent.startup import (
    reconcile_claimed_inbounds_at_startup,
    repair_dangling_tool_use_at_startup,
)
from agent.state import BaseAgentState
from agent.turn.runloop import (
    PendingTurnFailure,
    emit_error_event,
    graph_config,
    settle_turn_failure,
)
from agent.turn.trace_checkpoint import attach_trace_checkpoint_ref
from ava.sdk_surface import process_context
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.agents.history.delta_read_compat import recovery_reconstruction_scope
from base.agents.observation.db_wait import DatabaseWaits
from base.agents.observation.relay_supervision import RelaySupervision
from base.agents.observation.turn_progress import TurnProgress
from base.cluster.machine import machine_name
from base.config import settings
from base.config.agent_pins import resolve_agent_config_pins
from base.db import Database
from base.deploy.maintenance import admission
from base.events.live.announce import publish_agent_updated
from base.events.live.bus import EventBus
from base.events.live.publisher import AgentEventPublisher
from base.host.env.agent_slices import AgentSlices
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation, current_incarnation
from base.native_process.turn_identity import bind_turn_identity
from base.packages.plugins.config_view import resolve_agent_plugin_pins
from base.packages.plugins.extensions import EMPTY, ExtensionRegistry
from base.telemetry.tracing import turn_span
from services.agent_runner.agent_host import maintenance as maintenance_receipts
from services.agent_runner.agent_host.admission import TurnAdmission
from services.agent_runner.agent_host.crash_recovery import recover_reaped_corpses
from services.agent_runner.agent_host.db_recovery import database_phase, recover_database
from services.agent_runner.agent_host.dispatcher import PendingInboundWake
from services.agent_runner.agent_host.force_termination import (
    force_termination_outcome,
    force_termination_stop,
    kill_terminating_agent_shells,
)
from services.agent_runner.agent_host.pending_wakes import scan_rows
from services.agent_runner.agent_host.runtime import (
    HostStats,
    TurnOutcome,
    _AgentRuntime,
    admit_stored_model,
)
from services.agent_runner.agent_host.settlement import close_hosted_turn
from services.agent_runner.agent_host.stall_guard import run_invocation_with_stall_guard
from services.agent_runner.agent_host.wake_screening import _is_runnable, _read_stored_config

_HostGraph = CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState]

# The host's whole stop must fit ava-root's TERM window (`stop_timeout_s`, 10 s,
# `services/supervision/ava_root/supervisor.py`) or root retains custody of it. With Postgres
# unreachable the release would otherwise wait out the control pool's acquire
# timeout (30 s). A release that cannot land leaves the leases to expire by TTL.
_RELEASE_OWNER_TIMEOUT_S = 3.0


class AgentHost:
    """Runs one agent's turns on demand, over process-wide shared machinery.

    `run_turn` is what `TurnScheduler` calls; the scheduler guarantees at most
    one concurrent call per agent, so nothing here needs a per-agent lock.
    """

    def __init__(
        self,
        *,
        pool: AsyncConnectionPool[psycopg.AsyncConnection],
        control_pool: AsyncConnectionPool[psycopg.AsyncConnection] | None = None,
        checkpointer: AsyncPostgresSaver,
        graph: _HostGraph,
        machine: str | None = None,
        bus: EventBus,
        db: Database,
        extensions: ExtensionRegistry = EMPTY,
    ) -> None:
        self._bus = bus
        self._db = db
        # The connections every turn's `AvaContext` shares (the SDK calls a graph node makes
        # reach them through `ava.sdk_surface.process_context`, bound per turn below).
        self._clients = ClientSet(database=lambda: db)
        self._extensions = extensions
        self._pool = pool
        self._control_pool = control_pool if control_pool is not None else pool
        self._checkpointer = checkpointer
        self._graph = graph
        self._peek_lock = (
            asyncio.Lock()
        )  # one optional interrupt peek on the control pool at a time
        self._machine = machine if machine is not None else machine_name()
        self._owner = uuid4()
        self._runtimes: OrderedDict[int, _AgentRuntime] = OrderedDict()
        self._rejected_configs: dict[int, str] = {}
        self._normalized_configs: dict[int, str] = {}
        # Agents with a turn in flight right now. Eviction skips them: a running
        # turn holds its own reference, so dropping the entry would not break it
        # — it would just throw the work away and make that agent's NEXT turn
        # pay a cold build, which is the opposite of what a cache is for.
        self._in_flight: set[int] = set()
        self._maintenance_failed: dict[int, tuple[str | None, datetime | None]] = {}
        # Each agent's latest turn's config fingerprint (scheduler crash report); cleared per turn.
        self.turn_fingerprints: dict[int, str] = {}
        # What the graph and this host share across turns: handed to every turn's context.
        self.turn_progress = TurnProgress()
        self.relays = RelaySupervision()
        self._recall_log_key = secrets.token_bytes(32)
        self.admission = TurnAdmission(settings.daemon.host_max_concurrent_turns)
        self.database_waits = DatabaseWaits()
        self.stats = HostStats()

    async def run_turn(self, agent_id: int) -> None:
        """Retain the scheduler slot until real work, including threads, settles.

        Cancelling an await of ``to_thread`` does not stop the thread. Shield the
        whole owned turn (including boot and cleanup), not only graph return.
        Durable interrupts still stop cooperative LLM/exec work. Repeated outer
        cancellation must not release this agent to a concurrent successor.
        """
        from base.native_process.turn_identity import HostedTurnResources, bind_hosted_resources

        resources = HostedTurnResources()
        with bind_hosted_resources(resources):
            self.turn_fingerprints.pop(agent_id, None)
            work = asyncio.create_task(self._run_turn(agent_id))
        cancelled = False
        try:
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    cancelled = True
            work.result()
        except BaseException as exc:
            await maintenance_receipts.record_failure(agent_id, exc, self._maintenance_failed)
            raise
        finally:
            from base.agents.incarnation.hosted_force import original_host_force

            if resources.unresolved:
                # Keep the actual domains and scheduler registration alive.
                # No timer/cache reset can turn a failed close into quiescence.
                self._in_flight.add(agent_id)
                logger.error(
                    "hosted resources unresolved; force remains unobserved, "
                    "exact resource inspection required: {requests}",
                    agent_id=agent_id,
                    requests=[str(path) for path in resources.unresolved],
                )
                while resources.unresolved:
                    resources.changed.clear()
                    try:
                        await resources.changed.wait()
                    except asyncio.CancelledError:
                        cancelled = True
                self._in_flight.discard(agent_id)
            # Still inside the existing scheduler's exclusive per-agent pump.
            # No-task wakes also take this path without admitting a runtime.
            if cancelled:
                self.drop_agent(agent_id)
            settlement = asyncio.create_task(
                original_host_force(
                    self._control_pool,
                    agent_id,
                    self._owner,
                    self._machine,
                    quiescent=True,
                    kill_shell_sessions=kill_terminating_agent_shells,
                )
            )
            while not settlement.done():
                try:
                    await asyncio.shield(settlement)
                except asyncio.CancelledError:
                    cancelled = True
            settlement.result()
        if cancelled:
            raise asyncio.CancelledError
        await maintenance_receipts.record_drained(self._control_pool, self._owner, agent_id)

    async def accepts_force(self, agent_id: int, command_id: int) -> bool:
        """Authenticate cancellation against this live host's actual boot owner."""
        from base.agents.incarnation.hosted_force import original_host_force

        return await original_host_force(
            self._control_pool, agent_id, self._owner, self._machine, command_id=command_id
        )

    async def _run_turn(self, agent_id: int) -> None:
        """Run `agent_id` until it has nothing left to claim, then return.

        One read decides everything: whether this agent is ours to run, and what
        config the turn runs under. Re-read every turn rather than cached with
        the runtime — that is what makes `ava.self.restart(config_overlay)` land
        at the next turn boundary, the hosted replacement for "the process exits
        and boots with the merged config".
        """
        # A new turn starts a fresh progress window HERE, before any await:
        # without this, a long-idle agent's stale clock entry would read as
        # "stalled" during this turn's runtime (re)build — whose startup
        # reconcile can be slow — and the dispatcher's turn-level scan would
        # cancel the very recovery turn it just scheduled. First-line, not
        # merely before the build: the scan also runs during the
        # pre-admission reads below, and a task cancelled there refuses its
        # bounded unwind in teardown and takes the whole host down with it
        # (2026-09-11: a resurrect wake for a terminated agent was cancelled
        # on its predecessor's 3.5h-old clock two seconds after it started).
        self.turn_progress.reset(agent_id)
        stored = await _read_stored_config(self._control_pool, agent_id)
        if stored is None or not _is_runnable(self._machine, agent_id, stored):
            self.stats.wakes_skipped += 1
            return
        self.turn_fingerprints[agent_id] = stored.fingerprint

        if await maintenance_receipts.run_held(
            agent_id, stored.status, self._maintenance_failed, self._run_held_controls
        ):
            return

        # An active external lease owns decisions; no native graph or plugin
        # initialization may start on a dispatcher wake or a host restart.
        if await active_lease(self._control_pool, agent_id):
            await self._run_held_controls(agent_id, stored.status)
            return

        async with self.admission.admit(agent_id):
            pins = resolve_agent_config_pins(stored.config_overlay, stored.birth_config)
            plugin_pins = resolve_agent_plugin_pins(stored.config_overlay)
            # The stored model configuration is admitted before ANY turn work,
            # the status flip included: a wake whose model cannot build is
            # consumed without a turn (#2344), and a pin the registry has
            # withdrawn is normalized in place so this turn — and the usage
            # attribution and exec children it feeds — runs the model that
            # serves.
            if not admit_stored_model(
                pins,
                agent_id=agent_id,
                stored=stored,
                stats=self.stats,
                rejected=self._rejected_configs,
                normalized=self._normalized_configs,
            ):
                return
            self.stats.turns_started += 1
            incarnation = await admit_hosted_runtime(
                self._control_pool,
                agent_id,
                self._machine,
                self._owner,
                db=self._db,
                expected_from=stored.status,
            )
            if incarnation is None:
                logger.info(
                    "hosted turn for agent {agent_id} not started — row left {status} "
                    "(concurrent lifecycle op); skipping",
                    agent_id=agent_id,
                    status=stored.status,
                )
                return
            if admission.held():
                # Admission may have waited for prepare's real row lock.
                # Its only permitted continuation now is the owned control;
                # do not build a new runtime or run initialization hooks.
                await self._apply_held_controls(agent_id, incarnation)
                return
            outcome = TurnOutcome(exited=False, crashed=False)
            # Admission is durable before its optional live announce; every await
            # after that commit stays inside the settlement boundary so a
            # cancelled/half-open publish cannot strand a false `running` row.
            self._in_flight.add(agent_id)
            try:
                # The identity and the recovery scope wrap the whole turn; the agent's
                # configuration travels as its slices (the exec child gets it via the
                # re-emitted overlay env — see the module docstring).
                with (
                    bind_turn_identity(agent_id, incarnation=incarnation),
                    recovery_reconstruction_scope(self._checkpointer, str(agent_id)),
                ):
                    await publish_agent_updated(self._bus, agent_id)
                    slices = AgentSlices.resolve(pins, plugin_pins)
                    runtime = await self._runtime_for(agent_id, stored.fingerprint, slices)
                    outcome = await self._drive_turns(agent_id, runtime, slices)
            except asyncio.CancelledError:
                # A cancelled turn (stale-turn scan, force terminate, shutdown)
                # must not keep its runtime either: the next wake re-runs the
                # startup reconcile. External cancellation is not a corpse.
                self.drop_agent(agent_id)
                raise
            except Exception:
                # The scheduler logs and drops the task; dropping the runtime
                # ensures the next admission re-runs startup reconciliation.
                # An unclassified crash is a corpse too: settle it idling but
                # mark it so the reaper terminates it once grace elapses.
                self.drop_agent(agent_id)
                outcome = TurnOutcome(exited=False, crashed=True)
                raise
            finally:
                self._cache_after_turn(agent_id)
                await close_hosted_turn(
                    self._pool,
                    self._control_pool,
                    self._db,
                    self._bus,
                    self._checkpointer,
                    incarnation,
                    outcome,
                )

    async def _run_held_controls(self, agent_id: int, status: str) -> None:
        """Maintain ownership and apply admin intent without touching the graph."""

        incarnation = await admit_hosted_runtime(
            self._control_pool,
            agent_id,
            self._machine,
            self._owner,
            db=self._db,
            expected_from=status,
        )
        if incarnation is None:
            return
        async with force_termination_stop(self._control_pool, incarnation):
            await self._apply_held_controls(agent_id, incarnation)

    async def _apply_held_controls(self, agent_id: int, incarnation: RuntimeIncarnation) -> None:
        from agent.db import claim_inbound_batch

        with bind_turn_identity(agent_id, incarnation=incarnation):
            # An active external lease owns decisions and its claim gate never
            # runs while held, so the held-controls wake is the lease's only
            # native relay-supervision point. Hot path: one native_status read
            # plus a heartbeat comparison (supervise_relay escalates only on a
            # stale heartbeat — provision, spawn, rate-limited stamp; no model
            # calls, no polling). A supervision failure rides the existing
            # held-wake error path (record_failure is a no-op outside a
            # maintenance hold) and the next wake re-drives.
            session = await native_status(self._db, self._bus, agent_id)
            await supervise_relay(self._db, self._bus, session, agent_id, self.relays)
            # An earlier ordinary failure can leave a buffered tail. Preserve
            # it before accepting maintenance intent, without replaying graph work.
            await flush_checkpoint(self._checkpointer, agent_id)
            batch = await claim_inbound_batch(self._control_pool, agent_id, lifecycle_only=True)
            if len(batch) > 1 or any(not item.durable_lifecycle for item in batch):
                raise RuntimeError("held control claim returned an unaccepted command")
            kind = await apply_hosted_lifecycle(
                self._control_pool,
                incarnation,
                bus=self._bus,
                kill_shell_sessions=kill_terminating_agent_shells,
            )
            if kind is None:
                await settle_hosted_runtime(self._control_pool, incarnation, bus=self._bus)
            else:
                self.drop_agent(agent_id)

    # ── the per-agent runtime cache ──────────────────────────────────────────

    def _cache_after_turn(self, agent_id: int) -> None:
        """Refresh retained runtime recency and return excess idle entries."""
        cached = self._runtimes.get(agent_id)
        if cached is not None:
            cached.last_used = time.monotonic()
            self._runtimes.move_to_end(agent_id)
        self._in_flight.discard(agent_id)
        self._evict()

    async def _runtime_for(
        self, agent_id: int, fingerprint: str, slices: AgentSlices
    ) -> _AgentRuntime:
        """This agent's prepared runtime, building it when absent or stale.

        A cold build prepares the agent's model for this turn, from its `slices`.
        """
        cached = self._runtimes.get(agent_id)
        if cached is not None and cached.fingerprint == fingerprint:
            cached.last_used = time.monotonic()
            self._runtimes.move_to_end(agent_id)
            self.stats.cache_hits += 1
            return cached

        reason = "cold" if cached is None else "config_changed"
        self.stats.cache_misses += 1
        started = time.monotonic()
        runtime = await self._build_runtime(agent_id, fingerprint, slices)
        self._runtimes[agent_id] = runtime
        self._runtimes.move_to_end(agent_id)
        logger.info(
            "hosted runtime for agent {agent_id} built ({reason})",
            event="host_agent_prepared",
            agent_id=agent_id,
            reason=reason,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        self._evict()
        return runtime

    async def _build_runtime(
        self, agent_id: int, fingerprint: str, slices: AgentSlices
    ) -> _AgentRuntime:
        """Repair checkpoint/inbound state, then prepare the model."""
        await reconcile_claimed_inbounds_at_startup(self._pool, self._checkpointer, agent_id)
        await repair_dangling_tool_use_at_startup(self._graph, agent_id)
        llm = await boot_agent_scope(agent_id, slices.brain.llm_model, slices.overrides)
        return _AgentRuntime(fingerprint=fingerprint, llm=llm)

    def _evict(self) -> None:
        """Evict idle runtimes by age and least-recent use; misses rebuild on wake.

        Active runtimes survive both bounds, including turns longer than the
        idle TTL. Completion refreshes idle time and LRU position before eviction.
        Active-agent admission can exceed the cache budget; completion returns
        the warm cache to its configured size as active runtimes settle.
        """
        cutoff = time.monotonic() - settings.daemon.host_agent_idle_ttl_seconds
        aged = [
            a
            for a, r in self._runtimes.items()
            if r.last_used < cutoff and a not in self._in_flight
        ]
        for agent_id in aged:
            del self._runtimes[agent_id]
        cap = settings.daemon.host_agent_cache_size
        for agent_id in list(self._runtimes):
            if len(self._runtimes) <= cap:
                break
            if agent_id not in self._in_flight:
                del self._runtimes[agent_id]

    async def last_active_at(self, agent_id: int) -> datetime | None:
        """This agent's real activity clock — `agents_meta.last_active_at`.

        Handed to `TurnScheduler` so an uncancellable-turn report can say how
        long the agent has actually been silent. Deliberately THIS column and not
        the `/api/agents` field of the same name: that one is
        `MAX(inbound_messages.created_at)` (`base/agents/observation/snapshot.py`) and goes
        stale during exactly the long turns where "is it wedged?" is a real
        question — issue #183. This column is written on every completed LLM step
        (`agent/graph/llm/node.py:_persist_last_active`).

        Returns None when the row is gone; raising is left to the caller's
        best-effort wrapper, which runs on the shutdown path.
        """
        async with self._control_pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT last_active_at FROM agents_meta WHERE id = %s", (agent_id,)
                )
            ).fetchone()
        return None if row is None else row[0]

    async def pending_inbound_wakes(self, stale_after_s: float) -> list[PendingInboundWake]:
        """Find queued work and expired predecessors missed by Redis wakes.

        Database age identifies backlog; cancellation additionally requires the
        dispatcher's current turn-progress clock to be stale. Expired foreign
        owners include quiet idle rows whose lease expires after boot. Wakes
        retain admission/resource fences; an empty halted claim spends no model
        call. Held maintenance wakes its restart cohort; outside a hold this
        scan also keeps the steady held rows on its cadence: an open lease
        needs the held pass's pull-based supervision (task #3998), never wake
        delivery alone.
        """
        held_wakes = maintenance_receipts.pending_wakes(self._maintenance_failed)
        if held_wakes is not None:
            # The drain's held re-drive is update machinery, never the paced
            # cohort: its pace belongs to the drain windows (task #4652).
            return held_wakes
        rows = await scan_rows(self._control_pool, self._owner, self._machine, stale_after_s)
        return [PendingInboundWake(agent_id=row[0], stale=row[1], recovery=row[2]) for row in rows]

    def drop_agent(self, agent_id: int) -> None:
        """Forget an agent's cached runtime — the hosted equivalent of the
        fresh-process half of `ava.self.restart()`. The checkpointer thread, the
        real state, is untouched, exactly as a process restart leaves it."""
        self._runtimes.pop(agent_id, None)
        self.relays.drop(agent_id)

    # ── the turn loop ────────────────────────────────────────────────────────

    async def _drive_turns(
        self, agent_id: int, runtime: _AgentRuntime, slices: AgentSlices
    ) -> TurnOutcome:
        """Build this turn task's context and invoke the graph until it is done.

        The event publisher is created per turn task rather than cached with the
        runtime: it owns a background drain worker, and a worker per idle agent
        is precisely the per-idle-agent cost the hosted model deletes. It shares
        the process's Redis client, so creating one is a queue and a task.
        """
        event_publisher = AgentEventPublisher(
            self._bus.async_redis(), self._bus.channel, agent_id=agent_id
        )
        await event_publisher.start()
        ctx = AvaContext(
            ops_pool=self._pool,
            llm=runtime.llm,
            event_publisher=event_publisher,
            db=self._db,
            bus=self._bus,
            agent=slices,
            extensions=self._extensions,
            turn_progress=self.turn_progress,
            relays=self.relays,
            recall_log_key=self._recall_log_key,
            identity=AgentIdentity(agent_id=agent_id, owns_loop=True),
            clients=self._clients,
            # The dispatcher owns subscriptions; an empty claim ends this task.
        )
        try:
            with process_context.scoped(ctx):
                return await self._invoke_until_done(agent_id, ctx)
        finally:
            await event_publisher.aclose()

    async def _invoke_until_done(self, agent_id: int, ctx: AvaContext) -> TurnOutcome:  # noqa: PLR0915 — the turn exit boundary
        """Run until a durable lifecycle command or idle state ends this turn.

        Normal return flushes before applying lifecycle; restart retains its
        successor pointer, termination returns terminal intent, idle releases
        the task without manufacturing an inbound or model call.
        Reset only transient flags; halted/message/plugin state survives cold admission.
        """
        tags = ["ava", f"agent-{agent_id}", "hosted"]
        metadata: dict[str, object] = {"agent_id": agent_id, "hosted": True}
        config: RunnableConfig = graph_config(
            agent_id, tags, metadata, ctx.require_agent().kernel.checkpoint_interval
        )
        turn = 0
        pending_failure: PendingTurnFailure | None = None
        while True:
            try:
                async with database_phase():
                    await settle_checkpoint(
                        self._graph,
                        self._db,
                        self._bus,
                        agent_id,
                        self.relays,
                        activate_accepted=False,
                    )
                turn += 1
                with turn_span(name=f"ava-agent-{agent_id}", session_id=str(agent_id), turn=turn):
                    if pending_failure is not None:
                        # Database recovery must finish the original abort, not
                        # invoke a model again before halted/breaker state is saved.
                        async with database_phase():
                            await settle_turn_failure(
                                self._graph,
                                self._checkpointer,
                                config,
                                ctx,
                                agent_id,
                                pending_failure,
                            )
                            await attach_trace_checkpoint_ref(self._graph, ctx, agent_id)
                        return TurnOutcome(exited=False, crashed=True, aborted=True)
                    try:
                        result: dict[str, object] = await run_invocation_with_stall_guard(
                            self._graph,
                            agent_id,
                            ctx,
                            config,
                            {  # pyright: ignore[reportArgumentType, reportUnknownMemberType]
                                "turn_active": False,
                                "exit_requested": False,
                                "turn_idle": False,
                                "restart_requested": False,
                            },
                        )
                    except (FatalLLMStreamError, FatalProviderError, CompactionFailedError) as exc:
                        pending_failure = PendingTurnFailure(exc)
                        async with database_phase():
                            await settle_turn_failure(
                                self._graph,
                                self._checkpointer,
                                config,
                                ctx,
                                agent_id,
                                pending_failure,
                            )
                            await attach_trace_checkpoint_ref(self._graph, ctx, agent_id)
                        return TurnOutcome(exited=False, crashed=True, aborted=True)
                    # The trace must remain current until its final checkpoint is
                    # durable; an N-step buffered ID is not yet readable by the UI.
                    async with database_phase():
                        await flush_checkpoint(self._checkpointer, agent_id)
                        await attach_trace_checkpoint_ref(self._graph, ctx, agent_id)
                if result["exit_requested"] or result["restart_requested"]:
                    incarnation = current_incarnation(agent_id)
                    if incarnation is None:
                        raise RuntimeError(  # noqa: TRY301 — report through the turn error boundary
                            "hosted lifecycle return has no admitted incarnation"
                        )
                    self.drop_agent(agent_id)
                    async with database_phase():
                        kind = await apply_hosted_lifecycle(
                            self._control_pool,
                            incarnation,
                            bus=self._bus,
                            kill_shell_sessions=kill_terminating_agent_shells,
                        )
                    logger.info(
                        "hosted lifecycle return settled",
                        agent_id=agent_id,
                        generation=str(incarnation.generation),
                        command_kind=kind,
                    )
                    return TurnOutcome(exited=kind == "terminate", crashed=False)
                if result["turn_idle"]:
                    async with database_phase():
                        await settle_checkpoint(
                            self._graph, self._db, self._bus, agent_id, self.relays
                        )
                    return TurnOutcome(exited=False, crashed=False)
            except (psycopg.OperationalError, PoolTimeout):
                incarnation = current_incarnation(agent_id)
                if incarnation is None:
                    raise
                await recover_database(
                    pool=self._control_pool,
                    checkpointer=self._checkpointer,
                    graph=self._graph,
                    incarnation=incarnation,
                    database_waits=self.database_waits,
                    peek_lock=self._peek_lock,
                )
            except Exception as exc:
                ended = await force_termination_outcome(exc, self._control_pool, agent_id)
                if ended is not None:
                    self.drop_agent(agent_id)
                    return ended
                emit_error_event(
                    ctx, agent_id, f"{type(exc).__name__}: {exc}", error_class=type(exc).__name__
                )
                raise
            finally:
                flush_node_exit_aggregate(agent_id)

    async def aclose(self) -> None:
        """Drop every cached runtime. The pool, checkpointer and graph belong to
        the daemon that built them and are closed there."""
        self._runtimes.clear()
        await asyncio.to_thread(self._clients.close)
        try:
            async with asyncio.timeout(_RELEASE_OWNER_TIMEOUT_S):
                await release_hosted_owner(
                    self._control_pool, self._machine, self._owner, self._in_flight
                )
        except TimeoutError as exc:
            raise TimeoutError(
                f"hosted ownership release did not land within {_RELEASE_OWNER_TIMEOUT_S:g}s; "
                "its leases expire by TTL"
            ) from exc

    async def renew_ownership(self) -> None:
        """Existing daemon health beat also proves idle runtime responsibility.

        Renewal first, corpse reap second: a reap failure must not starve
        healthy rows' leases (the next beat retries the reap). The reaped
        corpses' recovery-wake attempts ride the same step — their wake rows
        are already committed, so a dropped attempt is deferred, not lost
        (task #4039).
        """
        await renew_hosted_owner(self._control_pool, self._machine, self._owner)
        try:
            reaped = await reap_crash_corpses(
                self._control_pool, self._machine, self._owner, bus=self._bus
            )
            await recover_reaped_corpses(self._db, self._bus, reaped)
        except Exception:
            logger.exception(
                "corpse reap failed — retrying next beat",
                event="corpse_reaper_failed",
            )
