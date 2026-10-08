"""Run every local agent through one shared host and graph.

The dispatcher owns per-agent single-flight and wake delivery. This driver binds an admitted
incarnation, resolves its framework/plugin configuration, and invokes the graph until idle
or native lifecycle return. The turn's identity is a
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
one (docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md). No per-agent process or global identity is created.

Native restart/terminate flushes the final checkpoint and applies its exact-owner
command before releasing single-flight. Normal maintenance waits for continuation
and managed-resource settlement. Explicit force cancellation stays fenced until
those resources close. Database outages retain the original task; recovery checks
its ownership before repairing and continuing, without creating a new inbound.

Each completed invocation flushes its checkpoint before linking it to the current trace.
Expected provider/compaction failures persist halted state, and their
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
from agent.ownership.hosted_completion import (
    completed_hosted_lifecycle_kind,
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
)
from agent.turn.trace_checkpoint import attach_trace_checkpoint_ref
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.agents.history.delta_read_compat import recovery_reconstruction_scope
from base.agents.incarnation.native_work_models import NativeWorkTarget, NativeWorkUncertainError
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
from base.native_process.turn_identity import bind_native_work, bind_turn_identity
from base.packages.plugins.config_view import resolve_agent_plugin_pins
from base.packages.plugins.extensions import EMPTY, ExtensionRegistry
from base.telemetry.tracing import turn_span
from services.agent_runner.agent_host import maintenance as maintenance_receipts
from services.agent_runner.agent_host.db_recovery import database_phase, recover_database
from services.agent_runner.agent_host.dispatcher import PendingInboundWake
from services.agent_runner.agent_host.force_termination import (
    force_termination_outcome,
    force_termination_stop,
    kill_terminating_agent_shells,
)
from services.agent_runner.agent_host.invocation import (
    PendingWorkResult,
    finish_pending_failure,
    recover_completed_work,
    returned_lifecycle_request,
)
from services.agent_runner.agent_host.invocation.native_work import (
    NativeWorkContinuation,
    halt_before_reinvoke,
    hold_native_cancel,
    prepare_native_invocation,
    recover_native_cancel,
    settle_native_invocation,
)
from services.agent_runner.agent_host.recovery.crash import recover_reaped_corpses
from services.agent_runner.agent_host.runtime import (
    HostStats,
    TurnOutcome,
    _AgentRuntime,
    admit_stored_model,
    cached_runtime,
    evict_runtimes,
    read_last_active_at,
)
from services.agent_runner.agent_host.scheduling.admission import TurnAdmission
from services.agent_runner.agent_host.scheduling.pending_wakes import scan_candidates
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
                    from base.agents.compaction.startup import resumable_compact

                    compact_continuation = await resumable_compact(self._control_pool, incarnation)
                    if compact_continuation is not None or await recover_native_cancel(
                        self._control_pool, self._checkpointer, self._graph, incarnation
                    ):
                        runtime = await self._runtime_for(agent_id, stored.fingerprint, slices)
                        outcome = await self._drive_turns(agent_id, runtime, slices)
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
            # plus known relay exit/heartbeat checks (recovery handles confirmed
            # exit or stale heartbeat — provision, spawn, rate-limited stamp; no model
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
        """Build a cold/stale model from this turn's slices, or retain its cache."""
        return await cached_runtime(
            self._runtimes,
            self.stats,
            agent_id,
            fingerprint,
            slices,
            self._build_runtime,
            self._evict,
        )

    async def _build_runtime(
        self, agent_id: int, fingerprint: str, slices: AgentSlices
    ) -> _AgentRuntime:
        """Repair checkpoint/inbound state, then prepare the model."""
        await reconcile_claimed_inbounds_at_startup(self._pool, self._checkpointer, agent_id)
        await repair_dangling_tool_use_at_startup(self._graph, agent_id)
        llm = await boot_agent_scope(agent_id, slices.brain.llm_model, slices.overrides)
        return _AgentRuntime(fingerprint=fingerprint, llm=llm)

    def _evict(self) -> None:
        """Evict settled runtimes by idle age and least-recent use."""
        evict_runtimes(self._runtimes, self._in_flight)

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

    # ── the turn loop ────────────────────────────────────────────────────────

    async def _drive_turns(
        self, agent_id: int, runtime: _AgentRuntime, slices: AgentSlices
    ) -> TurnOutcome:
        """Build an invocation-owned context; the driver owns its publisher
        lifecycle so cached idle runtimes retain no event drain worker."""
        event_publisher = AgentEventPublisher(
            self._bus.async_redis(), self._bus.channel, agent_id=agent_id
        )
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
        from services.agent_runner.agent_host.invocation.driver import drive_context

        return await drive_context(
            self._control_pool,
            self._checkpointer,
            self._graph,
            agent_id,
            ctx,
            self.database_waits,
            self._peek_lock,
            self._invoke_until_done,
            self.drop_agent,
        )

    async def _invoke_until_done(self, agent_id: int, ctx: AvaContext) -> TurnOutcome:
        """Run to idle or lifecycle completion, settling each returned invocation."""
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
            with (
                turn_span(name=f"ava-agent-{agent_id}", session_id=str(agent_id), turn=turn),
                bind_native_work(work.work_id),
            ):
                # Retain the result and trace; recovery cannot claim the next work.
                while True:
                    try:
                        if pending_failure is not None:
                            return await finish_pending_failure(
                                self._graph,
                                self._checkpointer,
                                agent_id,
                                ctx,
                                config,
                                pending_failure,
                                recovering=failure_recovered,
                                native_work=work.target,
                            )
                        prepared = (
                            pending.result
                            if pending is not None
                            else await self._invoke_prepared_graph(agent_id, ctx, config, work)
                        )
                        if isinstance(prepared, PendingTurnFailure):
                            pending_failure = prepared
                            continue
                        if pending is None:
                            pending = PendingWorkResult(prepared, native_work=work.target)
                        outcome = await self._finish_completed_invocation(agent_id, ctx, pending)
                        if outcome is not None:
                            return outcome
                        break
                    except (psycopg.OperationalError, PoolTimeout):
                        incarnation = current_incarnation(agent_id)
                        if incarnation is None:
                            raise
                        kind = await self._recover_completed_work(incarnation, pending, work.target)
                        failure_recovered = pending_failure is not None
                        if kind is not None:
                            return TurnOutcome(exited=kind == "terminate", crashed=False)
                    except NativeWorkUncertainError:
                        await hold_native_cancel(self._control_pool, work.target)
                        self.drop_agent(agent_id)
                        return TurnOutcome(exited=False, crashed=False, native_held=True)
                    except Exception as exc:
                        ended = await force_termination_outcome(exc, self._control_pool, agent_id)
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

    async def _invoke_prepared_graph(
        self,
        agent_id: int,
        ctx: AvaContext,
        config: RunnableConfig,
        work: NativeWorkContinuation,
    ) -> dict[str, object] | PendingTurnFailure:
        incarnation = current_incarnation(agent_id)
        if incarnation is None:
            raise RuntimeError("native graph preparation has no admitted incarnation")
        async with database_phase():
            halted = await halt_before_reinvoke(
                self._control_pool,
                self._checkpointer,
                self._graph,
                incarnation,
                work.target,
                config,
            )
            if halted is not None:
                return halted
            await settle_checkpoint(
                self._graph,
                self._db,
                self._bus,
                agent_id,
                self.relays,
                activate_accepted=False,
            )
            await prepare_native_invocation(self._control_pool, work, incarnation)
        try:
            return await run_invocation_with_stall_guard(
                self._graph,
                agent_id,
                ctx,
                config,
                {  # pyright: ignore[reportArgumentType, reportUnknownMemberType]
                    "turn_active": False,
                    "exit_requested": False,
                    "turn_idle": False,
                    "restart_requested": False,
                    "native_work": work.target,
                    "native_cancel": None,
                },
            )
        except (FatalLLMStreamError, FatalProviderError, CompactionFailedError) as exc:
            return PendingTurnFailure(exc)

    async def _finish_completed_invocation(
        self, agent_id: int, ctx: AvaContext, pending: PendingWorkResult
    ) -> TurnOutcome | None:
        # Correlate the original trace only after its checkpoint is durable.
        async with database_phase():
            if not pending.checkpoint_flushed:
                await flush_checkpoint(self._checkpointer, agent_id)
                pending.checkpoint_flushed = True
            if not pending.native_settled:
                incarnation = current_incarnation(agent_id)
                if incarnation is None:
                    raise RuntimeError("native invocation settlement has no incarnation")
                pending.native_cancelled = await settle_native_invocation(
                    self._control_pool,
                    self._checkpointer,
                    self._graph,
                    incarnation,
                    pending.native_work,
                    {"configurable": {"thread_id": str(agent_id)}},
                )
                pending.native_settled = True
            if not pending.trace_attached:
                await attach_trace_checkpoint_ref(self._graph, ctx, agent_id)
                pending.trace_attached = True
        if await returned_lifecycle_request(self._control_pool, agent_id, pending):
            incarnation = current_incarnation(agent_id)
            if incarnation is None:
                raise RuntimeError("hosted lifecycle return has no admitted incarnation")
            self.drop_agent(agent_id)
            async with database_phase():
                if pending.lifecycle_command_id is None:
                    return TurnOutcome(exited=False, crashed=False)
                kind = await apply_hosted_lifecycle(
                    self._control_pool,
                    incarnation,
                    bus=self._bus,
                    kill_shell_sessions=kill_terminating_agent_shells,
                    expected_command_id=pending.lifecycle_command_id,
                )
                if kind is None:
                    kind = await completed_hosted_lifecycle_kind(
                        self._control_pool, incarnation, pending.lifecycle_command_id
                    )
            logger.info(
                "hosted lifecycle return settled",
                agent_id=agent_id,
                generation=str(incarnation.generation),
                command_kind=kind,
            )
            return TurnOutcome(exited=kind == "terminate", crashed=False)
        if pending.native_cancelled or pending.result["turn_idle"]:
            async with database_phase():
                await settle_checkpoint(self._graph, self._db, self._bus, agent_id, self.relays)
            return TurnOutcome(exited=False, crashed=False)
        return None

    async def _recover_completed_work(
        self,
        incarnation: RuntimeIncarnation,
        pending: PendingWorkResult | None,
        native_work: NativeWorkTarget | None = None,
    ) -> str | None:
        return await recover_completed_work(
            lambda: recover_database(
                pool=self._control_pool,
                checkpointer=self._checkpointer,
                graph=self._graph,
                incarnation=incarnation,
                database_waits=self.database_waits,
                peek_lock=self._peek_lock,
            ),
            self._control_pool,
            incarnation,
            pending,
            native_work,
        )

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
