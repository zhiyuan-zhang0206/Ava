"""Run every local agent through one shared host and graph.

The dispatcher owns per-agent single-flight and wake delivery. This driver binds
an admitted incarnation, resolves its framework/plugin configuration, and invokes
the graph until idle or native lifecycle return. Contextvars surround invocation
so every graph node inherits the same identity and configuration; managed exec
children receive those pins through their existing environment projection.

The daemon shares a workload pool, a separate control pool, one checkpointer
(keyed by thread_id), and one compiled graph. Graph construction loads process-
global plugin definitions, so compiling a graph per agent would corrupt concurrent
turns. Chat models and startup reconciliation are cached per agent and invalidated
by the stored birth/overlay configuration fingerprint. Shared graph retry policy
still uses cluster-level settings (issue #174).

Cold admission repairs claimed inbound/checkpoint disagreements and dangling tool
pairs, establishes the workspace, and reconciles persistent watchers. Boot watcher
work also joins the existing durable scan, so a missed Redis wake cannot strand
an idle agent's recurring work. No per-agent process or global identity is created.

Native restart/terminate flushes the final checkpoint and applies its exact-owner
command before releasing single-flight. Normal maintenance waits for continuation
and managed-resource settlement. Explicit force cancellation stays fenced until
those resources close. Database outages retain the original task; recovery checks
its ownership before repairing and continuing, without creating a new inbound.

Each completed invocation flushes its checkpoint before linking it to the current
trace. Expected provider/compaction failures persist halted state; unexpected errors
remain visible and propagate. Configuration rejection leaves pending work durable
for a subsequent scan after the configuration is corrected.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from datetime import datetime
from uuid import uuid4

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent._process_boot import boot_agent_scope, reconcile_agent_watchers
from agent._runloop import PendingTurnFailure, _emit_error_event, _graph_config, settle_turn_failure
from agent._trace_checkpoint import attach_trace_checkpoint_ref
from agent._turn_progress import reset_turn_progress
from agent.graph._llm_errors import FatalLLMStreamError, FatalProviderError
from agent.graph._node_log import flush_node_exit_aggregate
from agent.hooks.compact import CompactionFailedError
from agent.hosted_ownership import (
    admit_hosted_runtime,
    apply_hosted_lifecycle,
    release_hosted_owner,
    renew_hosted_owner,
    settle_hosted_runtime,
)
from agent.impersonation import active_lease, flush_checkpoint, settle_checkpoint
from agent.startup import (
    _reconcile_claimed_inbounds_at_startup,
    _repair_dangling_tool_use_at_startup,
)
from agent.state import BaseAgentState
from services.agent_host import maintenance as maintenance_receipts
from services.agent_host.db_recovery import database_phase, recover_database
from services.agent_host.dispatcher import PendingInboundWake
from services.agent_host.runtime import (
    HostStats,
    _active_turn_config_fingerprint,
    _AgentRuntime,
    _copy_active_turn_context,
    _StoredConfig,
)
from services.agent_host.stall_guard import run_invocation_with_stall_guard
from shared import maintenance
from shared.config import settings
from shared.config.turn_view import bind_agent_config, resolve_agent_config_pins
from shared.context import AvaContext
from shared.db_transaction import async_write_transaction
from shared.event_publisher import AgentEventPublisher
from shared.live_announce import publish_agent_updated
from shared.lm.factory import validate_model_config
from shared.log import logger
from shared.machine import machine_name
from shared.plugin_config_view import bind_agent_plugin_config, resolve_agent_plugin_pins
from shared.redis_client import get_async_redis
from shared.runtime_incarnation import RuntimeIncarnation, current_incarnation
from shared.trace import turn_span
from shared.turn_identity import bind_turn_identity

_HostGraph = CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState]

# Statuses whose owner must not be handed a turn. `terminated` is the real one:
# a wake for a dead agent is the delivery watchdog's resurrect business, and
# running a turn would revive it behind the gateway's back. Unclaimed `idling` and
# `restarting` belong to the boot / respawn path, which owns the row until it
# reaches a running state.
_UNRUNNABLE_STATUSES = frozenset({"terminated", "restarting"})


async def settle_stale_running_rows(pool: AsyncConnectionPool, machine: str) -> list[int]:
    """Restore rows left running by a previous hosted-runner instance.

    A pidless row may belong to another live host instance. Only unknown or
    expired hosted ownership licenses this atomic startup status settlement.
    """
    async with async_write_transaction(pool) as conn:
        rows = await (
            await conn.execute(
                "UPDATE agents_meta SET status = 'idling' "
                "WHERE status = 'running' AND pid IS NULL AND machine = %s "
                "AND (runtime_kind IS NULL OR runtime_kind = 'hosted') "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= now()) RETURNING id",
                (machine,),
            )
        ).fetchall()
    settled = [row[0] for row in rows]
    logger.info(
        "hosted stale-running settle: settled {n} row(s)",
        event="host_stale_running_settled",
        n=len(settled),
    )
    return settled


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
    ) -> None:
        self._pool = pool
        self._control_pool = control_pool if control_pool is not None else pool
        self._checkpointer = checkpointer
        self._graph = graph
        self._machine = machine if machine is not None else machine_name()
        self._owner = uuid4()
        self._runtimes: OrderedDict[int, _AgentRuntime] = OrderedDict()
        self._rejected_configs: dict[int, str] = {}
        # Agents with a turn in flight right now. Eviction skips them: a running
        # turn holds its own reference, so dropping the entry would not break it
        # — it would just throw the work away and make that agent's NEXT turn
        # pay a cold build, which is the opposite of what a cache is for.
        self._in_flight: set[int] = set()
        self._watcher_recovery_pending: set[int] = set()
        self._maintenance_failed: dict[int, tuple[str | None, datetime | None]] = {}
        self._turn_slots = asyncio.Semaphore(settings.daemon.host_max_concurrent_turns)
        self.stats = HostStats()

    # ── the scheduler's entry point ──────────────────────────────────────────

    async def run_turn(self, agent_id: int) -> None:
        """Retain the scheduler slot until real work, including threads, settles.

        Cancelling an await of ``to_thread`` does not stop the thread. Shield the
        whole owned turn (including boot and cleanup), not only graph return.
        Durable interrupts still stop cooperative LLM/exec work. Repeated outer
        cancellation must not release this agent to a concurrent successor.
        """
        from shared.turn_identity import HostedTurnResources, bind_hosted_resources

        resources = HostedTurnResources()
        with bind_hosted_resources(resources):
            # Keep the child's Context so its config fingerprint can be copied
            # back to the scheduler task before a crash is re-raised. A normal
            # create_task copy would isolate the value from the crash logger.
            turn_context = _copy_active_turn_context()
            work = asyncio.create_task(self._run_turn(agent_id), context=turn_context)
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
            _active_turn_config_fingerprint.set(turn_context.get(_active_turn_config_fingerprint))
            from shared.hosted_force import original_host_force

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
                    self._control_pool, agent_id, self._owner, self._machine, quiescent=True
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
        from shared.hosted_force import original_host_force

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
        stored = await self._read_stored_config(agent_id)
        if stored is None or not self._is_runnable(agent_id, stored):
            self._watcher_recovery_pending.discard(agent_id)
            self.stats.wakes_skipped += 1
            return
        _active_turn_config_fingerprint.set(stored.fingerprint)

        if await maintenance_receipts.run_held(
            agent_id, stored.status, self._maintenance_failed, self._run_held_controls
        ):
            return

        # An active external lease owns decisions; no native graph or plugin
        # initialization may start on a dispatcher wake or a host restart.
        if await active_lease(self._control_pool, agent_id):
            await self._run_held_controls(agent_id, stored.status)
            return

        # A new turn starts a fresh progress window the moment it is entered:
        # without this, a long-idle agent's stale clock entry would read as
        # "stalled" during this turn's runtime (re)build — whose startup
        # reconcile can be slow — and the dispatcher's turn-level scan would
        # cancel the very recovery turn it just scheduled.
        reset_turn_progress(agent_id)

        async with self._turn_slots:
            pins = resolve_agent_config_pins(stored.config_overlay, stored.birth_config)
            plugin_pins = resolve_agent_plugin_pins(stored.config_overlay)
            # Fail fast before ANY turn work — the status flip included: an
            # overlay naming a model the registry does not know would otherwise
            # explode inside build_chat_model on every wake (dispatcher drops the
            # task, the pending scan re-wakes the still-pending inbound, and the
            # host loops on crash tracebacks — incident #2344). The effective
            # model resolves exactly as the turn view does (overlay > birth >
            # cluster default). The wake is consumed without raising: the durable
            # inbound stays pending, and a fixed overlay is served on the next
            # scan.
            model = pins.get("llm_model") or settings.lm.llm_model
            try:
                validate_model_config(model=model)
            except ValueError as exc:
                self.stats.config_rejected += 1
                if self._rejected_configs.get(agent_id) != stored.fingerprint:
                    self._rejected_configs[agent_id] = stored.fingerprint
                    logger.error(
                        "hosted wake for agent {agent_id} rejected before turn — "
                        "its model config cannot build: {reason}. Fix the agent's "
                        "llm_model (restart(config_overlay=...) or the spawn "
                        "overlay) and the next wake serves normally.",
                        event="host_config_rejected",
                        agent_id=agent_id,
                        reason=str(exc),
                    )
                return
            self._rejected_configs.pop(agent_id, None)
            self.stats.turns_started += 1
            incarnation = await admit_hosted_runtime(
                self._control_pool,
                agent_id,
                self._machine,
                self._owner,
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
            if maintenance.held():
                # Admission may have waited for prepare's real row lock.
                # Its only permitted continuation now is the owned control;
                # do not build a new runtime or run initialization hooks.
                await self._apply_held_controls(agent_id, incarnation)
                return
            exited = False
            # Admission is durable before its optional live announce; every await
            # after that commit stays inside the settlement boundary so a
            # cancelled/half-open publish cannot strand a false `running` row.
            self._in_flight.add(agent_id)
            try:
                # All three binds wrap the whole turn (the exec child gets agent
                # config via the re-emitted overlay env instead — see the module
                # docstring); the runtime build is inside them: build_chat_model
                # reads turn_settings.lm.llm_model, only this agent's while bound.
                with (
                    bind_turn_identity(agent_id, incarnation=incarnation),
                    bind_agent_config(pins),
                    bind_agent_plugin_config(plugin_pins),
                ):
                    await publish_agent_updated(self._control_pool, agent_id)
                    runtime = await self._runtime_for(agent_id, stored.fingerprint)
                    exited = await self._drive_turns(agent_id, runtime)
            except asyncio.CancelledError:
                # A cancelled turn (stale-turn scan, force terminate, shutdown)
                # must not keep its runtime either: the next wake's runtime build
                # re-runs the startup reconcile — the hosted equivalent of a
                # fresh boot. The in-flight ref keeps the current build alive;
                # only the cache entry is dropped.
                self.drop_agent(agent_id)
                raise
            except Exception:
                # The scheduler logs and drops the task; dropping the runtime
                # ensures the next admission re-runs startup reconciliation.
                self.drop_agent(agent_id)
                raise
            finally:
                self._in_flight.discard(agent_id)
                if not exited:
                    await settle_hosted_runtime(self._control_pool, incarnation)

    async def _run_held_controls(self, agent_id: int, status: str) -> None:
        """Maintain ownership and apply admin intent without touching the graph."""

        incarnation = await admit_hosted_runtime(
            self._control_pool, agent_id, self._machine, self._owner, expected_from=status
        )
        if incarnation is None:
            return
        await self._apply_held_controls(agent_id, incarnation)

    async def _apply_held_controls(self, agent_id: int, incarnation: RuntimeIncarnation) -> None:
        from agent.db import claim_inbound_batch

        with bind_turn_identity(agent_id, incarnation=incarnation):
            batch = await claim_inbound_batch(self._control_pool, agent_id, lifecycle_only=True)
            if len(batch) > 1 or any(not item.durable_lifecycle for item in batch):
                raise RuntimeError("held control claim returned an unaccepted command")
            kind = await apply_hosted_lifecycle(self._control_pool, incarnation)
            if kind is None:
                await settle_hosted_runtime(self._control_pool, incarnation)
            else:
                self.drop_agent(agent_id)

    # ── locality / runnability ───────────────────────────────────────────────

    def _is_runnable(self, agent_id: int, stored: _StoredConfig) -> bool:
        """Whether this host should hand `agent_id` a turn right now.

        Two rejections, deliberately quiet rather than WARNING: a foreign
        agent's wake is normal cross-talk (the dispatcher's pattern subscription
        is cluster-wide, so every runner sees every wake), and a terminated
        agent's wake is the delivery watchdog's resurrect path doing its job.
        Neither is a fault of this host.
        """
        if stored.machine != self._machine:
            logger.debug(
                "hosted wake for agent {agent_id} belongs to machine {owner} — not ours",
                agent_id=agent_id,
                owner=stored.machine,
            )
            return False
        if stored.status in _UNRUNNABLE_STATUSES:
            logger.info(
                "hosted wake for agent {agent_id} ignored — status {status} is not runnable",
                agent_id=agent_id,
                status=stored.status,
            )
            return False
        return True

    async def _read_stored_config(self, agent_id: int) -> _StoredConfig | None:
        """This agent's machine, status and two config maps in one round trip.

        None = the row is gone, which is a real anomaly (a wake was published for
        an agent that does not exist) and says so, unlike the two ordinary
        rejections above.
        """
        async with self._control_pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT machine, status, config_overlay, birth_config "
                    "FROM agents_meta WHERE id = %s",
                    (agent_id,),
                )
            ).fetchone()
        if row is None:
            logger.warning(
                "hosted wake for agent {agent_id} has no agents_meta row — ignoring",
                agent_id=agent_id,
            )
            return None
        return _StoredConfig(
            machine=row[0], status=row[1], config_overlay=row[2], birth_config=row[3]
        )

    # ── the per-agent runtime cache ──────────────────────────────────────────

    async def _runtime_for(self, agent_id: int, fingerprint: str) -> _AgentRuntime:
        """This agent's prepared runtime, building it when absent or stale.

        Called with the turn's three binds already in effect, so a cold build
        reads this agent's config rather than the cluster default.
        """
        cached = self._runtimes.get(agent_id)
        if cached is not None and cached.fingerprint == fingerprint:
            cached.last_used = time.monotonic()
            self._runtimes.move_to_end(agent_id)
            self.stats.cache_hits += 1
            if agent_id in self._watcher_recovery_pending:
                await self._restore_watchers(agent_id)
            return cached

        reason = "cold" if cached is None else "config_changed"
        self.stats.cache_misses += 1
        started = time.monotonic()
        runtime = await self._build_runtime(agent_id, fingerprint)
        await self._restore_watchers(agent_id)
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

    async def _restore_watchers(self, agent_id: int) -> None:
        if await reconcile_agent_watchers(agent_id):
            self._watcher_recovery_pending.discard(agent_id)
        else:
            self._watcher_recovery_pending.add(agent_id)

    async def _build_runtime(self, agent_id: int, fingerprint: str) -> _AgentRuntime:
        """Repair checkpoint/inbound state, then prepare the model."""
        await _reconcile_claimed_inbounds_at_startup(self._pool, self._checkpointer, agent_id)
        await _repair_dangling_tool_use_at_startup(self._graph, agent_id)
        llm = await boot_agent_scope(agent_id)
        return _AgentRuntime(fingerprint=fingerprint, llm=llm)

    def _evict(self) -> None:
        """Drop runtimes past the idle TTL and past the size cap (LRU first).

        Two bounds because they answer different questions: the cap keeps a
        fleet-wide wake burst from holding one runtime per local agent, and the
        TTL keeps a lightly loaded runner from holding a long-silent agent's
        runtime forever. Eviction is pure bookkeeping — a dropped runtime costs
        its agent one rebuild on its next wake, nothing more.

        Agents with a turn in flight are skipped by BOTH bounds. `last_used` is
        stamped when a turn starts, so a turn that runs longer than the TTL —
        an autonomous loop, which the design explicitly allows to run for days —
        would otherwise evict its own runtime while still using it. The cap
        therefore behaves as a soft bound whenever more turns are in flight than
        it allows; the concurrency semaphore is what keeps that from being
        unbounded, and a cache smaller than `host_max_concurrent_turns` is a
        config error rather than a case to handle here.
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
        `MAX(inbound_messages.created_at)` (`shared/agent_snapshot.py`) and goes
        stale during exactly the long turns where "is it wedged?" is a real
        question — issue #183. This column is written on every completed LLM step
        (`agent/graph/_llm.py:_persist_last_active`).

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

    async def watcher_boot_wakes(self) -> list[int]:
        """Keep boot recovery in the existing scan until watchers are reconciled."""
        async with self._control_pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT DISTINCT m.id FROM agents_meta m JOIN agent_watchers w ON w.agent_id=m.id "
                    "WHERE m.machine=%s AND m.status IN ('running','idling') AND w.status='running'",
                    (self._machine,),
                )
            ).fetchall()
        agents = [row[0] for row in rows]
        self._watcher_recovery_pending.update(agents)
        return agents

    async def pending_inbound_wakes(self, stale_after_s: float) -> list[PendingInboundWake]:
        """Find pending work and lifecycle pointers missed by Redis wakes.

        Stale cancellation requires both pending-message age and completed-turn
        age to exceed the grace period. Held maintenance only wakes its restart
        cohort, preserving the current iteration until its ordinary claim.
        """
        held_wakes = maintenance_receipts.pending_wakes(self._maintenance_failed)
        if held_wakes is not None:
            return held_wakes
        async with self._control_pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT m.id, "
                    "  (m.last_active_at IS NULL "
                    "   OR m.last_active_at < now() - make_interval(secs => %s)) "
                    "  AND EXISTS ("
                    "    SELECT 1 FROM inbound_messages stale "
                    "    WHERE stale.agent_id = m.id AND stale.status = 'pending' "
                    "      AND stale.created_at < now() - make_interval(secs => %s)"
                    "  ) "
                    "FROM agents_meta m "
                    "WHERE ("
                    "    m.status = 'idling' "
                    "    OR (m.status='running' AND m.runtime_owner IS DISTINCT FROM %s "
                    "        AND EXISTS (SELECT 1 FROM agent_impersonations takeover "
                    "          WHERE takeover.agent_id=m.id AND takeover.status IN "
                    "          ('requested','accepted','active'))) "
                    "    OR (m.status='terminated' AND m.runtime_kind='hosted' "
                    "        AND m.runtime_owner=%s AND EXISTS ("
                    "          SELECT 1 FROM inbound_messages force "
                    "          WHERE force.id=m.lifecycle_command_id AND force.agent_id=m.id "
                    "          AND force.target_generation=m.runtime_generation "
                    "          AND force.target_owner=m.runtime_owner AND force.kind='terminate' "
                    "          AND force.status='claimed' AND force.applied_at IS NOT NULL "
                    "          AND force.observed_at IS NULL)) "
                    "    OR (m.status = 'running' "
                    "        AND (m.last_active_at IS NULL "
                    "             OR m.last_active_at < now() - make_interval(secs => %s)) "
                    "        AND EXISTS ("
                    "          SELECT 1 FROM inbound_messages stale2 "
                    "          WHERE stale2.agent_id = m.id AND stale2.status = 'pending' "
                    "            AND stale2.created_at < now() - make_interval(secs => %s)"
                    "        )"
                    "    )"
                    "  ) "
                    "  AND m.machine = %s "
                    "  AND (m.lifecycle_command_id IS NOT NULL OR EXISTS ("
                    "    SELECT 1 FROM inbound_messages pending "
                    "    WHERE pending.agent_id = m.id AND pending.status = 'pending'"
                    "  ) OR EXISTS (SELECT 1 FROM agent_impersonations lease "
                    "    WHERE lease.agent_id=m.id AND (lease.status IN "
                    "    ('requested','accepted','active') OR lease.delta_version>lease.applied_version))) "
                    "  AND (m.runtime_owner IS DISTINCT FROM %s OR NOT EXISTS ("
                    "    SELECT 1 FROM agent_impersonations held "
                    "    WHERE held.agent_id=m.id AND held.status='active' "
                    "    AND held.expires_at>clock_timestamp()) "
                    "    OR EXISTS (SELECT 1 FROM inbound_messages control "
                    "    WHERE control.agent_id=m.id AND control.status IN ('pending','claimed') "
                    "    AND control.kind IN ('restart','terminate')))",
                    (
                        stale_after_s,
                        stale_after_s,
                        self._owner,
                        self._owner,
                        stale_after_s,
                        stale_after_s,
                        self._machine,
                        self._owner,
                    ),
                )
            ).fetchall()
        wakes = [PendingInboundWake(agent_id=row[0], stale=row[1]) for row in rows]
        pending = {wake.agent_id for wake in wakes}
        wakes.extend(
            PendingInboundWake(agent_id=agent_id, stale=False)
            for agent_id in self._watcher_recovery_pending - pending
        )
        return wakes

    def drop_agent(self, agent_id: int) -> None:
        """Forget an agent's cached runtime — the hosted equivalent of the
        fresh-process half of `ava.self.restart()`. The checkpointer thread, the
        real state, is untouched, exactly as a process restart leaves it."""
        self._runtimes.pop(agent_id, None)

    # ── the turn loop ────────────────────────────────────────────────────────

    async def _drive_turns(self, agent_id: int, runtime: _AgentRuntime) -> bool:
        """Build this turn task's context and invoke the graph until it is done.

        The event publisher is created per turn task rather than cached with the
        runtime: it owns a background drain worker, and a worker per idle agent
        is precisely the per-idle-agent cost the hosted model deletes. It shares
        the process's Redis client, so creating one is a queue and a task.
        """
        event_publisher = AgentEventPublisher(
            get_async_redis(), settings.data_plane.events_channel, agent_id=agent_id
        )
        await event_publisher.start()
        ctx = AvaContext(
            ops_pool=self._pool,
            llm=runtime.llm,
            event_publisher=event_publisher,
            # The dispatcher owns subscriptions; an empty claim ends this task.
        )
        try:
            return await self._invoke_until_done(agent_id, ctx)
        finally:
            await event_publisher.aclose()

    async def _invoke_until_done(self, agent_id: int, ctx: AvaContext) -> bool:
        """Run until a durable lifecycle command or idle state ends this turn.

        Normal return flushes before applying lifecycle; restart retains its
        successor pointer, termination returns terminal intent, idle releases
        the task without manufacturing an inbound or model call.
        Reset only transient flags; halted/message/plugin state survives cold admission.
        """
        tags = ["ava", f"agent-{agent_id}", "hosted"]
        metadata: dict[str, object] = {"agent_id": agent_id, "hosted": True}
        config: RunnableConfig = _graph_config(agent_id, tags, metadata)
        turn = 0
        pending_failure: PendingTurnFailure | None = None
        while True:
            try:
                async with database_phase():
                    await settle_checkpoint(self._graph, agent_id, activate_accepted=False)
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
                        return False
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
                        return False
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
                        kind = await apply_hosted_lifecycle(self._control_pool, incarnation)
                    logger.info(
                        "hosted lifecycle return settled",
                        agent_id=agent_id,
                        generation=str(incarnation.generation),
                        command_kind=kind,
                    )
                    return kind == "terminate"
                if result["turn_idle"]:
                    async with database_phase():
                        await settle_checkpoint(self._graph, agent_id)
                    return False
            except (psycopg.OperationalError, PoolTimeout):
                incarnation = current_incarnation(agent_id)
                if incarnation is None:
                    raise
                await recover_database(
                    pool=self._control_pool,
                    checkpointer=self._checkpointer,
                    graph=self._graph,
                    incarnation=incarnation,
                )
            except Exception as exc:
                _emit_error_event(
                    ctx, agent_id, f"{type(exc).__name__}: {exc}", error_class=type(exc).__name__
                )
                raise
            finally:
                flush_node_exit_aggregate(agent_id)

    async def aclose(self) -> None:
        """Drop every cached runtime. The pool, checkpointer and graph belong to
        the daemon that built them and are closed there."""
        self._runtimes.clear()
        await release_hosted_owner(self._control_pool, self._machine, self._owner, self._in_flight)

    async def renew_ownership(self) -> None:
        """Existing daemon health beat also proves idle runtime responsibility."""
        await renew_hosted_owner(self._control_pool, self._machine, self._owner)
