"""Run local agents through a shared graph and explicit per-turn context.

The dispatcher owns single-flight; the daemon owns pools and checkpointer.
Cold admission and database recovery repair the original task before model work.
Lifecycle requests retain original command and resource settlement fences.
Unknown failures propagate; expected failures settle their checkpoint.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from functools import partial
from uuid import UUID, uuid4

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from pydantic import BaseModel

from agent.graph.llm_errors import FatalLLMStreamError, FatalProviderError
from agent.graph.node_log import flush_node_exit_aggregate
from agent.hooks.compact import CompactionFailedError
from agent.impersonation import (
    active_lease,
    flush_checkpoint,
    native_status,
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
from agent.startup import reconcile_claimed_inbounds_at_startup, repair_dangling_tool_use_at_startup
from agent.state import BaseAgentState
from agent.turn.runloop import (
    PendingTurnFailure,
    emit_error_event,
    graph_config,
)
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.agents.history.delta_read_compat import recovery_reconstruction_scope
from base.agents.incarnation.native_work_models import NativeWorkTarget, NativeWorkUncertainError
from base.agents.observation.db_wait import DatabaseWaits
from base.agents.observation.relay_supervision import RelaySupervision
from base.agents.observation.turn_progress import TurnProgress
from base.cluster.machine import machine_name
from base.config.agent_pins import resolve_agent_config_pins
from base.db import Database
from base.deploy.maintenance import admission
from base.events.live.announce import publish_agent_updated
from base.events.live.bus import EventBus
from base.events.live.publisher import AgentEventPublisher
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedServiceResources, HostedTurnResources
from base.packages.plugins.config_view import resolve_agent_plugin_pins
from base.packages.plugins.extensions import EMPTY, ExtensionRegistry
from base.telemetry.tracing import turn_span
from services.agent_runner.agent_host.db_recovery import recover_database
from services.agent_runner.agent_host.dispatcher import PendingInboundWake
from services.agent_runner.agent_host.force_termination import (
    force_termination_outcome,
    force_termination_stop,
    kill_terminating_agent_shells,
)
from services.agent_runner.agent_host.invocation import (
    PendingWorkResult,
    finish_completed_invocation,
    finish_pending_failure,
    recover_completed_work,
)
from services.agent_runner.agent_host.invocation.checkpoints import TurnCheckpoints
from services.agent_runner.agent_host.invocation.driver import drive_context
from services.agent_runner.agent_host.invocation.native_work import (
    NativeWorkContinuation,
    hold_native_cancel,
    invoke_prepared_graph,
    recover_native_cancel,
)
from services.agent_runner.agent_host.lifecycle import maintenance as maintenance_receipts
from services.agent_runner.agent_host.lifecycle.shutdown import close_host_resources
from services.agent_runner.agent_host.recovery.crash import recover_reaped_corpses
from services.agent_runner.agent_host.runtime import (
    HostPolicy,
    HostStats,
    TurnOutcome,
    _AgentRuntime,
    admit_stored_model,
    build_runtime,
    cached_runtime,
    evict_runtimes,
    read_last_active_at,
    refresh_cached_runtime,
)
from services.agent_runner.agent_host.scheduling.admission import TurnAdmission
from services.agent_runner.agent_host.scheduling.pending_wakes import scan_candidates
from services.agent_runner.agent_host.settlement import (
    close_hosted_turn,
    wait_retained_resources,
    wait_shielded_task,
)
from services.agent_runner.agent_host.stall_guard import run_invocation_with_stall_guard
from services.agent_runner.agent_host.wake_screening import _is_runnable, _read_stored_config

_HostGraph = CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState]

# The host's whole stop must fit ava-root's TERM window (`stop_timeout_s`, 10 s,
# `services/supervision/ava_root/supervisor.py`) or root retains custody of it. With Postgres
# unreachable the release would otherwise wait out the control pool's acquire
# timeout (30 s). A release that cannot land leaves the leases to expire by TTL.
_RELEASE_OWNER_TIMEOUT_S = 3.0


class AgentHost:
    """Runs one agent's turns on demand, over shared machinery.
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
        catalog: ModelCatalog,
        policy: HostPolicy,
        clients: ClientSet | None = None,
        extensions: ExtensionRegistry = EMPTY,
        plugin_configs: Mapping[str, BaseModel] | None = None,
    ) -> None:
        self._bus = bus
        self._db = db
        self._catalog = catalog
        self._policy = policy
        # Shared clients are passed through each turn's explicit `AvaContext`.
        self._clients = clients if clients is not None else ClientSet(database=lambda: db)
        self._extensions = extensions
        self._plugin_configs = plugin_configs or {}
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
        # Eviction preserves in-flight runtimes so the next turn can reuse the same build.
        self._in_flight: set[int] = set()
        self._maintenance_failed: dict[int, tuple[str | None, datetime | None]] = {}
        # Each agent's latest turn's config fingerprint (scheduler crash report); cleared per turn.
        self.turn_fingerprints: dict[int, str] = {}
        # What the graph and this host share across turns: handed to every turn's context.
        self.turn_progress = TurnProgress()
        self.relays = RelaySupervision()
        self._recall_log_key = secrets.token_bytes(32)
        self.admission = TurnAdmission(policy.max_concurrent_turns)
        self.database_waits = DatabaseWaits()
        self.stats = HostStats()
        self._resource_service = HostedServiceResources()

    @property
    def runtime_owner(self) -> UUID:
        """The immutable owner this actual hosted service advertises to health clients."""
        return self._owner

    async def run_turn(self, agent_id: int) -> None:
        """Hold single-flight through boot, execution and cleanup, including threads.
        Outer cancellation retains unjoined turns; durable interrupts still cancel work.
        """
        with self._resource_service.hold_turn(asyncio.current_task()):
            resources = await self._resource_service.turn()
            self.turn_fingerprints.pop(agent_id, None)
            work = asyncio.create_task(self._run_turn(agent_id, resources=resources))
            self._resource_service.retain_task(resources, work)
            cancelled = False
            primary: BaseException | None = None
            try:
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        cancelled = True
                work.result()
            except BaseException as exc:
                primary = exc
                try:
                    await maintenance_receipts.record_failure(
                        agent_id, exc, self._maintenance_failed
                    )
                except BaseException as receipt_error:
                    exc.add_note(f"hosted failure receipt failed: {receipt_error!r}")
                raise
            finally:
                try:
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
                        cancelled = await wait_retained_resources(resources) or cancelled
                        self._in_flight.discard(agent_id)
                    # The exclusive pump also covers no-task wakes.
                    if cancelled:
                        self.drop_agent(agent_id)
                    from services.agent_runner.agent_host.invocation.compact.source import (
                        finish_force_and_compact,
                    )

                    settlement = asyncio.create_task(
                        finish_force_and_compact(
                            original_host_force(
                                self._control_pool,
                                agent_id,
                                self._owner,
                                self._machine,
                                quiescent=True,
                                kill_shell_sessions=kill_terminating_agent_shells,
                            ),
                            self._control_pool,
                            self._checkpointer,
                            self._graph,
                            agent_id,
                            self._owner,
                            resources,
                            self._bus,
                            self.drop_agent,
                            self.database_waits,
                            self._peek_lock,
                            work=None,
                            catalog=self._catalog,
                            llm_override=self._policy.llm_override(),
                            default_reader=self._policy.default_reader,
                            reconcile_inputs=self._policy.reconcile_inputs,
                        )
                    )
                    self._resource_service.retain_task(resources, settlement)
                    cancelled = await wait_shielded_task(settlement) or cancelled
                    settlement.result()
                except BaseException as cleanup_error:
                    if primary is None:
                        raise
                    primary.add_note(f"hosted turn cleanup failed: {cleanup_error!r}")
            if cancelled:
                raise asyncio.CancelledError
            await maintenance_receipts.record_drained(self._control_pool, self._owner, agent_id)

    async def accepts_force(self, agent_id: int, command_id: int) -> bool:
        """Authenticate cancellation against this live host's actual boot owner."""
        from base.agents.incarnation.hosted_force import original_host_force

        return await original_host_force(
            self._control_pool, agent_id, self._owner, self._machine, command_id=command_id
        )

    async def _run_turn(self, agent_id: int, *, resources: HostedTurnResources | None) -> None:
        """Run to idle, re-reading ownership and configuration each turn boundary.
        Restart overlays reach the next turn even when its model remains cached.
        """
        # Reset before any admission await: recovery may take time, and the
        # dispatcher's stall scan must not cancel this turn using its predecessor's clock.
        self.turn_progress.reset(agent_id)
        stored = await _read_stored_config(self._control_pool, agent_id)
        if stored is None or not _is_runnable(self._machine, agent_id, stored):
            self.stats.wakes_skipped += 1
            return
        self.turn_fingerprints[agent_id] = stored.fingerprint

        if await maintenance_receipts.run_held(
            agent_id,
            stored.status,
            self._maintenance_failed,
            partial(self._run_held_controls, resources=resources),
        ):
            return

        # An active external lease owns decisions; no native graph or plugin
        # initialization may start on a dispatcher wake or a host restart.
        if await active_lease(self._control_pool, agent_id):
            await self._run_held_controls(agent_id, stored.status, resources=resources)
            return

        async with self.admission.admit(agent_id):
            pins = resolve_agent_config_pins(stored.config_overlay, stored.birth_config)
            plugin_pins = resolve_agent_plugin_pins(stored.config_overlay, self._plugin_configs)
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
                catalog=self._catalog,
                llm_override=self._policy.llm_override(),
                default_model=self._policy.default_model,
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
                await self._apply_held_controls(
                    agent_id, incarnation, work=None, resources=resources
                )
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
                with recovery_reconstruction_scope(
                    self._checkpointer, str(agent_id)
                ) as reconstruction:
                    checkpoints = TurnCheckpoints(self._checkpointer, self._graph).bind(
                        reconstruction
                    )
                    await publish_agent_updated(self._bus, agent_id)
                    slices = self._policy.resolve_slices(pins, plugin_pins, self._plugin_configs)
                    from base.agents.compaction.startup import resumable_compact

                    compact_continuation = await resumable_compact(self._control_pool, incarnation)
                    if compact_continuation is not None or await recover_native_cancel(
                        self._control_pool,
                        checkpoints.saver,
                        checkpoints.graph,
                        incarnation,
                        resources=resources,
                    ):
                        runtime = await self._runtime_for(
                            agent_id,
                            stored.fingerprint,
                            slices,
                            incarnation=incarnation,
                            checkpoints=checkpoints,
                        )
                        outcome = await self._drive_turns(
                            agent_id,
                            runtime,
                            slices,
                            incarnation=incarnation,
                            resources=resources,
                            checkpoints=checkpoints,
                        )
                    else:
                        self.drop_agent(agent_id)
                        outcome = TurnOutcome(exited=False, crashed=False, native_held=True)
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
                refresh_cached_runtime(self._runtimes, self._in_flight, agent_id, self._evict)
                await close_hosted_turn(
                    self._pool,
                    self._control_pool,
                    self._db,
                    self._bus,
                    self._checkpointer,
                    incarnation,
                    outcome,
                    resources=resources,
                    wake_enabled=self._policy.recovery_wake_enabled,
                    prompt_reap_enabled=self._policy.recrash_reap_enabled,
                    reconcile_inputs=self._policy.reconcile_inputs,
                )

    async def _run_held_controls(
        self, agent_id: int, status: str, *, resources: HostedTurnResources | None
    ) -> None:
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
            await self._apply_held_controls(agent_id, incarnation, work=None, resources=resources)

    async def _apply_held_controls(
        self,
        agent_id: int,
        incarnation: RuntimeIncarnation,
        *,
        work: NativeWorkTarget | None,
        resources: HostedTurnResources | None,
    ) -> None:
        from agent.db import claim_inbound_batch

        # An external lease owns decisions; held wakes alone supervise its relays.
        # One native_status read checks known exits/heartbeats without models or polling.
        # Confirmed exit/staleness triggers recovery; failures use held-wake error handling.
        # record_failure is a no-op outside maintenance; the next wake re-drives recovery.
        session = await native_status(self._db, self._bus, agent_id, incarnation=incarnation)
        await supervise_relay(
            self._db, self._bus, session, agent_id, self.relays, incarnation=incarnation
        )
        # An earlier ordinary failure can leave a buffered tail. Preserve
        # it before accepting maintenance intent, without replaying graph work.
        await flush_checkpoint(self._checkpointer, agent_id)
        batch = await claim_inbound_batch(
            self._control_pool,
            agent_id,
            lifecycle_only=True,
            incarnation=incarnation,
            work=work,
        )
        if len(batch) > 1 or any(not item.durable_lifecycle for item in batch):
            raise RuntimeError("held control claim returned an unaccepted command")
        kind = await apply_hosted_lifecycle(
            self._control_pool,
            incarnation,
            bus=self._bus,
            kill_shell_sessions=kill_terminating_agent_shells,
            resources=resources,
        )
        if kind is None:
            await settle_hosted_runtime(
                self._control_pool, incarnation, bus=self._bus, resources=resources
            )
        else:
            self.drop_agent(agent_id)

    async def _runtime_for(
        self,
        agent_id: int,
        fingerprint: str,
        slices: AgentSlices,
        *,
        incarnation: RuntimeIncarnation,
        checkpoints: TurnCheckpoints | None = None,
    ) -> _AgentRuntime:
        """Build a cold/stale model from this turn's slices, or retain its cache."""
        checkpoints = checkpoints or TurnCheckpoints(self._checkpointer, self._graph)
        return await cached_runtime(
            self._runtimes,
            self.stats,
            agent_id,
            fingerprint,
            slices,
            partial(self._build_runtime, incarnation=incarnation, checkpoints=checkpoints),
            self._evict,
        )

    async def _build_runtime(
        self,
        agent_id: int,
        fingerprint: str,
        slices: AgentSlices,
        *,
        incarnation: RuntimeIncarnation | None,
        checkpoints: TurnCheckpoints | None = None,
    ) -> _AgentRuntime:
        """Repair checkpoint/inbound state, then prepare the model."""
        checkpoints = checkpoints or TurnCheckpoints(self._checkpointer, self._graph)
        await reconcile_claimed_inbounds_at_startup(
            self._pool,
            checkpoints.saver,
            agent_id,
            incarnation=incarnation,
            inputs=self._policy.reconcile_inputs,
        )
        await repair_dangling_tool_use_at_startup(checkpoints.graph, agent_id)
        return await build_runtime(
            agent_id,
            fingerprint,
            slices,
            catalog=self._catalog,
            llm_override=self._policy.llm_override(),
        )

    def _evict(self) -> None:
        """Evict settled runtimes by idle age and least-recent use."""
        evict_runtimes(
            self._runtimes, self._in_flight, policy=self._policy.cache(), now=time.monotonic()
        )

    async def last_active_at(self, agent_id: int) -> datetime | None:
        """Read the scheduler's actual LLM activity clock."""
        return await read_last_active_at(self._control_pool, agent_id)

    async def pending_inbound_wakes(self, stale_after_s: float) -> list[PendingInboundWake]:
        """Use the scheduling owner's durable work/maintenance scan."""
        return await scan_candidates(
            self._control_pool, self._owner, self._machine, stale_after_s, self._maintenance_failed
        )

    def drop_agent(self, agent_id: int) -> None:
        """Forget an agent's cached runtime — the hosted equivalent of the
        fresh-process half of `ava.self.restart()`. The checkpointer thread, the
        real state, is untouched, exactly as a process restart leaves it."""
        self._runtimes.pop(agent_id, None)
        self.relays.drop(agent_id)

    async def _drive_turns(
        self,
        agent_id: int,
        runtime: _AgentRuntime,
        slices: AgentSlices,
        *,
        incarnation: RuntimeIncarnation,
        resources: HostedTurnResources | None,
        checkpoints: TurnCheckpoints | None = None,
    ) -> TurnOutcome:
        """Build this invocation's context; the driver owns its publisher lifecycle."""
        checkpoints = checkpoints or TurnCheckpoints(self._checkpointer, self._graph)
        event_publisher = AgentEventPublisher(
            self._bus.async_redis(), self._bus.channel, agent_id=agent_id
        )
        ctx = AvaContext(
            ops_pool=self._pool,
            llm=runtime.llm,
            catalog=self._catalog,
            event_publisher=event_publisher,
            db=self._db,
            clock_factory=self._policy.clock_factory,
            bus=self._bus,
            agent=slices,
            extensions=self._extensions,
            turn_progress=self.turn_progress,
            relays=self.relays,
            recall_log_key=self._recall_log_key,
            identity=AgentIdentity(agent_id=agent_id, owns_loop=True),
            original_incarnation=incarnation,
            hosted_resources=resources,
            clients=self._clients,
            # The dispatcher owns subscriptions; an empty claim ends this task.
        )
        return await drive_context(
            self._control_pool,
            checkpoints.saver,
            checkpoints.graph,
            agent_id,
            ctx,
            self.database_waits,
            self._peek_lock,
            partial(self._invoke_until_done, checkpoints=checkpoints),
            self.drop_agent,
            reconstruction=checkpoints.reconstruction,
            reconcile_inputs=self._policy.reconcile_inputs,
        )

    async def _invoke_native_graph(
        self,
        agent_id: int,
        config: RunnableConfig,
        ctx: AvaContext,
        initial: dict[str, object],
        *,
        graph: _HostGraph | None = None,
    ) -> dict[str, object] | PendingTurnFailure:
        try:
            return await run_invocation_with_stall_guard(
                graph if graph is not None else self._graph, agent_id, ctx, config, initial
            )
        except (FatalLLMStreamError, FatalProviderError, CompactionFailedError) as exc:
            return PendingTurnFailure(exc)

    async def _invoke_until_done(
        self, agent_id: int, ctx: AvaContext, *, checkpoints: TurnCheckpoints | None = None
    ) -> TurnOutcome:
        """Run to idle or lifecycle completion, settling each returned invocation."""
        checkpoints = checkpoints or TurnCheckpoints(self._checkpointer, self._graph)
        tags = ["ava", f"agent-{agent_id}", "hosted"]
        metadata: dict[str, object] = {"agent_id": agent_id, "hosted": True}
        config: RunnableConfig = graph_config(
            agent_id, tags, metadata, ctx.require_agent().kernel.checkpoint_interval
        )
        turn = 0
        pending_failure: PendingTurnFailure | None = None
        failure_recovered = False
        while True:
            turn += 1
            pending: PendingWorkResult | None = None
            work = NativeWorkContinuation(uuid4())
            with turn_span(name=f"ava-agent-{agent_id}", session_id=str(agent_id), turn=turn):
                # Retain the result and trace; recovery cannot claim the next work.
                while True:
                    try:
                        if pending_failure is not None:
                            return await finish_pending_failure(
                                checkpoints.graph,
                                checkpoints.saver,
                                agent_id,
                                ctx,
                                config,
                                pending_failure,
                                notes=self._policy.handoff_notes,
                                recovering=failure_recovered,
                                native_work=work.target,
                            )
                        prepared = (
                            pending.result
                            if pending is not None
                            else await invoke_prepared_graph(
                                self._control_pool,
                                checkpoints.saver,
                                checkpoints.graph,
                                agent_id,
                                ctx,
                                config,
                                work,
                                db=self._db,
                                bus=self._bus,
                                relays=self.relays,
                                notes=self._policy.handoff_notes,
                                invoke=partial(
                                    self._invoke_native_graph,
                                    agent_id,
                                    config,
                                    graph=checkpoints.graph,
                                ),
                            )
                        )
                        if isinstance(prepared, PendingTurnFailure):
                            pending_failure = prepared
                            continue
                        if pending is None:
                            pending = PendingWorkResult(prepared, native_work=work.target)
                        outcome = await finish_completed_invocation(
                            self._control_pool,
                            checkpoints,
                            agent_id,
                            ctx,
                            pending,
                            self.drop_agent,
                            kill_terminating_agent_shells,
                            db=self._db,
                            bus=self._bus,
                            relays=self.relays,
                            notes=self._policy.handoff_notes,
                        )
                        if outcome is not None:
                            return outcome
                        break
                    except (psycopg.OperationalError, PoolTimeout):
                        incarnation = ctx.require_original_incarnation(agent_id)
                        kind = await self._recover_completed_work(
                            incarnation, pending, work.target, checkpoints=checkpoints
                        )
                        failure_recovered = pending_failure is not None
                        if kind is not None:
                            return TurnOutcome(exited=kind == "terminate", crashed=False)
                    except NativeWorkUncertainError:
                        await hold_native_cancel(self._control_pool, work.target)
                        self.drop_agent(agent_id)
                        return TurnOutcome(exited=False, crashed=False, native_held=True)
                    except Exception as exc:
                        ended = await force_termination_outcome(
                            exc, self._control_pool, agent_id, incarnation=ctx.original_incarnation
                        )
                        if ended is not None:
                            self.drop_agent(agent_id)
                            return ended
                        emit_error_event(
                            ctx,
                            agent_id,
                            f"{type(exc).__name__}: {exc}",
                            error_class=type(exc).__name__,
                        )
                        raise
                    finally:
                        flush_node_exit_aggregate(agent_id)

    async def _recover_completed_work(
        self,
        incarnation: RuntimeIncarnation,
        pending: PendingWorkResult | None,
        native_work: NativeWorkTarget | None = None,
        *,
        checkpoints: TurnCheckpoints | None = None,
    ) -> str | None:
        checkpoints = checkpoints or TurnCheckpoints(self._checkpointer, self._graph)
        return await recover_completed_work(
            lambda: recover_database(
                pool=self._control_pool,
                checkpointer=checkpoints.saver,
                graph=checkpoints.graph,
                incarnation=incarnation,
                database_waits=self.database_waits,
                peek_lock=self._peek_lock,
                work=native_work,
                reconstruction_parent=checkpoints.reconstruction,
                reconcile_inputs=self._policy.reconcile_inputs,
            ),
            self._control_pool,
            incarnation,
            pending,
            native_work,
        )

    @property
    def resources_joined(self) -> bool:
        return self._resource_service.joined

    async def aclose(self, *, resource_deadline: float | None = None) -> None:
        """Join retained resources before closing clients and releasing owners.

        Failed scopes remain unresolved; the daemon closes pools only after this join.
        """
        await close_host_resources(
            self._resource_service,
            self._clients,
            lambda: release_hosted_owner(
                self._control_pool, self._machine, self._owner, self._in_flight
            ),
            clear_runtimes=self._runtimes.clear,
            resource_deadline=resource_deadline,
            release_timeout=_RELEASE_OWNER_TIMEOUT_S,
        )

    async def renew_ownership(self) -> None:
        """Renew healthy leases before reaping corpses and retrying committed wakes."""
        await renew_hosted_owner(self._control_pool, self._machine, self._owner)
        try:
            reaped = await reap_crash_corpses(
                self._control_pool,
                self._machine,
                self._owner,
                bus=self._bus,
                wake_enabled=self._policy.recovery_wake_enabled,
            )
            await recover_reaped_corpses(self._db, self._bus, reaped)
        except Exception:
            logger.exception(
                "corpse reap failed — retrying next beat",
                event="corpse_reaper_failed",
            )
