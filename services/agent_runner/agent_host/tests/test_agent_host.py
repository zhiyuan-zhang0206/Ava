"""The hosted agent-runner's turn runner — `services/agent_runner/agent_host/host.py`.

`test_turn_dispatcher.py` locks WHEN an agent runs. This file locks WHAT running
means, and the contracts here are the ones that only exist because many agents
now share one process:

1. **Isolation** — two agents' turns running concurrently must not see each
   other's identity, framework config, plugin config, model, or event
   publisher. In process mode the OS gave this for free; here it is bought by
   explicit invocation contexts, and this file proves isolation under real overlap.
2. **The config rebind** — a turn reads the agent's stored config fresh, so an
   overlay written between turns takes effect at the next one, and the cached
   per-agent runtime is rebuilt rather than reused. This is the hosted
   replacement for "the process exits and boots with the merged config"; a
   cache that missed it would run an agent on a model the DB says it left.
3. **The four-way turn loop** — `exit_requested` is terminal, `restart_requested` drops the runtime without a notify, `turn_idle` ends
   the task, and neither means re-invoke on the same thread.
4. **Runnability** — a wake for another machine's agent, or for a terminated
   one, must not start a turn. The dispatcher's subscription is cluster-wide, so
   this is the only thing that keeps a runner to its own agents.
5. **The bounds** — the concurrent-turn semaphore, the LRU cap, and the idle TTL
   are what make "an idle agent costs nothing" true of the cache too.
6. **The gate fails CLOSED** — unreadable config must leave hosted mode OFF.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

import ava
from agent.ownership.hosted import TurnFatalStamp, TurnSettlement
from base.agents.context import AvaContext
from base.agents.incarnation.resource_admission import DRAINED_RESOURCES
from base.db import Database
from base.events.live.bus import EventBus
from base.events.live.tests.fakes import patch_async_redis
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host import settlement
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.runtime import TurnOutcome
from tests.fixtures.pin_agent import pin_agent, pin_no_identity


def _host(*, catalog: ModelCatalog, **kwargs: Any) -> AgentHost:
    """An `AgentHost` on this box with the handles the tests share."""
    return AgentHost(
        machine="this-box",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=catalog,
        **kwargs,
    )


class _HostPluginConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    marker: str = Field(default="disk-default", json_schema_extra={"per_agent": True})


@pytest.fixture
def host_plugin() -> dict[str, BaseModel]:
    """One host's plugin config image, passed into its composition root."""
    return {"hostplug": _HostPluginConfig()}


# ── fakes ────────────────────────────────────────────────────────────────────


class _Row:
    """One `agents_meta` row as `_read_stored_config` selects it."""

    def __init__(
        self,
        machine: str = "this-box",
        status: str = "running",
        overlay: dict[str, Any] | None = None,
        birth: dict[str, Any] | None = None,
    ) -> None:
        self.tuple = (machine, status, overlay, birth)


class _FakeCursor:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _FakeConn:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None]:
        yield

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _FakeCursor:
        if sql == "SET TRANSACTION READ WRITE":
            return _FakeCursor(None)
        if sql == "SELECT id FROM agents_meta WHERE id=%s FOR UPDATE":
            return _FakeCursor((params[0],) if params[0] in self._pool.rows else None)
        if sql == (
            "SELECT i.id FROM agents_meta m JOIN inbound_messages i "
            "ON i.id=m.lifecycle_command_id WHERE m.id=%s "
            "AND m.runtime_generation=%s AND m.runtime_owner=%s "
            "AND i.agent_id=m.id AND i.target_generation=%s AND i.target_owner=%s "
            "AND i.kind IN ('restart','terminate') AND i.status='claimed' "
            "AND i.applied_at IS NULL"
        ):
            # Legacy graph-return cases carry one synthetic claimed command.
            return _FakeCursor((params[0],) if params[0] in self._pool.rows else None)
        if sql == (
            "SELECT 1 FROM agents_meta m JOIN inbound_messages i "  # noqa: S608 -- constant owner SQL
            "ON i.id=m.lifecycle_command_id AND i.agent_id=m.id "
            "WHERE m.id=%s AND i.id=%s AND i.kind='restart' "
            "AND i.status='claimed' AND i.applied_at IS NOT NULL "
            "AND i.target_owner=%s "
            "AND i.observed_at IS NULL AND m.runtime_owner IS NULL "
            f"AND {DRAINED_RESOURCES}"
        ):
            # A failed orchestration turn has no certified maintenance receipt.
            return _FakeCursor(None)
        compact_pending = (
            "SELECT acceptance,outcome,attempt_id,execution,result,attempt_provider "
            "FROM native_compact_commands WHERE agent_id=%s AND released_at IS NULL"
        )
        compact_closed = (
            "SELECT c.acceptance,c.outcome,c.attempt_id,c.execution,c.result,c.attempt_provider "
            "FROM native_compact_commands c JOIN agents_meta m ON m.id=c.agent_id "
            "JOIN native_graph_work w ON w.id=m.native_work_id "
            "WHERE m.id=%s AND c.execution->>'work_id'=w.id::text "
            "AND c.released_at IS NOT NULL AND w.phase='settled' AND w.ended_at IS NOT NULL"
        )
        if sql in (compact_pending, compact_pending + " FOR UPDATE", compact_closed):
            # These orchestration agents have no guarded compact command.
            return _FakeCursor(None)
        if sql == (
            "SELECT machine, status, config_overlay, birth_config FROM agents_meta WHERE id = %s"
        ):
            self._pool.reads += 1
            row = self._pool.rows.get(params[0])
            return _FakeCursor(row.tuple if row is not None else None)
        raise AssertionError(f"unexpected host fixture SQL: {sql}")


class _FakeConnCtx:
    """The `async with pool.connection()` shape."""

    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._pool)

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakePool:
    """Host orchestration SQL without native commands; count stored-config reads.

    Deliberately not a live pool: every contract in this file is about
    contextvars, asyncio ordering and cache bookkeeping, none of which a real
    Postgres would exercise differently. What a real DB WOULD add — that an
    overlay write lands in the column this reads — is one `UPDATE` away from
    trivial and is not what has ever broken.
    """

    def __init__(self, rows: dict[int, _Row]) -> None:
        self.rows = rows
        self.reads = 0

    def connection(self, timeout: float | None = None) -> _FakeConnCtx:
        return _FakeConnCtx(self)


class _PendingScanCursor:
    def __init__(self, rows: list[tuple[int, bool, bool]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple[int, bool, bool]]:
        return self._rows


class _PendingScanConn:
    def __init__(self, pool: _PendingScanPool) -> None:
        self._pool = pool

    async def execute(self, sql: str, params: tuple[object, ...]) -> _PendingScanCursor:
        self._pool.sql = sql
        self._pool.params = params
        return _PendingScanCursor(self._pool.rows)


class _PendingScanPool:
    """One host backstop query with captured SQL and returned candidates."""

    def __init__(self, rows: list[tuple[int, bool, bool]]) -> None:
        self.rows = rows
        self.sql = ""
        self.params: tuple[object, ...] = ()

    def connection(self) -> _PendingScanCtx:
        return _PendingScanCtx(self)


class _PendingScanCtx:
    def __init__(self, pool: _PendingScanPool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _PendingScanConn:
        return _PendingScanConn(self._pool)

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Model:
    """A stand-in chat model that remembers which model name built it."""

    def __init__(self, name: str) -> None:
        self.name = name


class _Publisher:
    """A stand-in `AgentEventPublisher` that remembers whose turn built it."""

    def __init__(self, _redis: object, _channel: str, *, agent_id: int) -> None:
        self.agent_id = agent_id

    async def start(self, _tasks: asyncio.TaskGroup) -> None: ...

    async def aclose(self) -> None: ...


class _Observation:
    """What one turn saw from inside a node task."""

    def __init__(
        self,
        agent_id: int | None,
        model: str,
        plugin_marker: str,
        llm: _Model,
        publisher: _Publisher,
        ops_pool: object,
    ) -> None:
        self.agent_id = agent_id
        self.model = model
        self.plugin_marker = plugin_marker
        self.llm = llm
        self.publisher = publisher
        self.ops_pool = ops_pool


class _FakeGraph:
    """Stands in for the compiled graph.

    `ainvoke` reads the explicit invocation context from inside a child task,
    where a LangGraph node actually runs.
    """

    def __init__(self, results: dict[int, list[dict[str, Any]]]) -> None:
        self._results = results
        self.observations: list[_Observation] = []
        self.gates: dict[int, asyncio.Event] = {}
        self.arrived: dict[int, asyncio.Event] = {}

    def gate(self, agent_id: int) -> asyncio.Event:
        return self.gates.setdefault(agent_id, asyncio.Event())

    def arrival(self, agent_id: int) -> asyncio.Event:
        return self.arrived.setdefault(agent_id, asyncio.Event())

    async def _observe(self, _agent_id: int, context: AvaContext) -> _Observation:
        agent = context.require_agent()
        plugin_cfg = cast(_HostPluginConfig, agent.plugin_config("hostplug"))
        return _Observation(
            agent_id=context.require_identity().agent_id,
            model=agent.brain.llm_model,
            plugin_marker=plugin_cfg.marker,
            llm=cast(_Model, context.llm),
            publisher=cast(_Publisher, context.event_publisher),
            ops_pool=context.ops_pool,
        )

    async def ainvoke(
        self, _input: dict[str, Any], *, config: dict[str, Any], context: AvaContext
    ) -> dict[str, Any]:
        agent_id = int(config["configurable"]["thread_id"])
        self.observations.append(await asyncio.create_task(self._observe(agent_id, context)))
        self.arrival(agent_id).set()
        gate = self.gates.get(agent_id)
        if gate is not None:
            await gate.wait()
            gate.clear()
        queued = self._results.get(agent_id)
        if queued:
            return queued.pop(0)
        return {"exit_requested": False, "turn_idle": True, "restart_requested": False}


class _GatedGraph:
    """`ainvoke` that blocks on an event, optionally swallowing its own
    cancellation (the C-call-blocked shape the bounded unwind cannot interrupt).
    """

    def __init__(self, *, refuse_cancel: bool = False) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.refuse_cancel = refuse_cancel
        self.cancel_seen = False
        self.contexts: list[AvaContext] = []

    async def ainvoke(
        self, _input: dict[str, Any], *, config: dict[str, Any], context: AvaContext
    ) -> dict[str, Any]:
        self.contexts.append(context)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_seen = True
            if self.refuse_cancel:
                # Model the blocked-in-a-C-call shape: the cancellation lands
                # but the task cannot honour it.
                await self.release.wait()
            raise
        return {"exit_requested": False, "turn_idle": True, "restart_requested": False}


_Build = Callable[..., "tuple[AgentHost, _FakeGraph, _FakePool]"]


def _stub_host_transitions(
    monkeypatch: pytest.MonkeyPatch, flip: Callable[..., Awaitable[bool]]
) -> list[int]:
    import services.agent_runner.agent_host.host as host_mod
    from base.native_process.runtime_incarnation import RuntimeIncarnation
    from services.agent_runner.agent_host.invocation import native_work

    stamps: list[int] = []

    async def admit(
        pool: object, agent_id: int, _machine: str, owner: UUID, *, expected_from: str, db: object
    ) -> RuntimeIncarnation | None:
        if not await flip(pool, agent_id, "running", expected_from=expected_from):
            return None
        return RuntimeIncarnation(agent_id, uuid4(), owner)

    async def settle_and_stamp(
        pool: object,
        incarnation: RuntimeIncarnation,
        *,
        bus: object,
        exited: bool,
        crashed: bool,
        resources: object,
    ) -> TurnSettlement:
        if crashed:
            stamps.append(incarnation.agent_id)
        if not exited:
            await flip(pool, incarnation.agent_id, "idling", expected_from="running")
        return TurnSettlement(
            stamp=TurnFatalStamp(applied=crashed, recrash=False), settled=not exited
        )

    monkeypatch.setattr(host_mod, "admit_hosted_runtime", admit)
    # These orchestration fixtures expose no managed native-work capability.
    # Strong command/checkpoint/transfer facts use actual PG tests in native_cancel/.
    monkeypatch.setattr(host_mod, "recover_native_cancel", AsyncMock(return_value=True))
    monkeypatch.setattr(native_work, "prepare_native_invocation", AsyncMock(return_value=None))
    monkeypatch.setattr(native_work, "settle_checkpoint", AsyncMock(return_value=False))
    monkeypatch.setattr(settlement, "settle_and_stamp_turn", settle_and_stamp)
    return stamps


@pytest.fixture
def wired(
    monkeypatch: pytest.MonkeyPatch, host_plugin: dict[str, BaseModel], model_catalog: ModelCatalog
) -> _Build:
    """An `AgentHost` over fakes, with the per-agent build stubbed.

    The per-agent build is stubbed because it needs a live key.
    `boot_agent_scope` is replaced by a build that returns a model named as the
    host asked, so a test can tell which model the host prepared.
    """
    # This suite's agents/ownership live exclusively in _FakePool. Real lease
    # SQL is covered by takeover integration tests, so its no-lease baseline
    # must be fake too instead of querying a second, empty database.
    monkeypatch.setattr(
        "services.agent_runner.agent_host.host.active_lease", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        "services.agent_runner.agent_host.invocation.settle_checkpoint",
        AsyncMock(return_value=False),
    )
    import services.agent_runner.agent_host.host as host_mod
    import services.agent_runner.agent_host.runtime as runtime_mod

    async def _noop_reconcile(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(host_mod, "reconcile_claimed_inbounds_at_startup", _noop_reconcile)
    monkeypatch.setattr(settlement, "reconcile_claimed_inbounds_at_startup", _noop_reconcile)
    monkeypatch.setattr(host_mod, "repair_dangling_tool_use_at_startup", _noop_reconcile)
    monkeypatch.setattr(host_mod, "publish_agent_updated", _noop_reconcile)

    async def _fake_boot_agent_scope(
        _agent_id: int, llm_model: str, *_: object, **_kwargs: object
    ) -> tuple[_Model, None]:
        return _Model(llm_model), None

    monkeypatch.setattr(runtime_mod, "boot_agent_scope", _fake_boot_agent_scope)

    def _allow_model_config(
        *,
        model: str | None = None,
        catalog: ModelCatalog,
        llm_override: str,
        overrides: object = None,
    ) -> None:
        """Keep fake host tests independent of installed provider credentials."""

    monkeypatch.setattr(runtime_mod, "validate_model_config", _allow_model_config)

    # The publisher below never touches the client; the host only passes it through.
    patch_async_redis(monkeypatch, object)

    monkeypatch.setattr(host_mod, "AgentEventPublisher", _Publisher)

    async def _flip_hosted_status(*_args: object, **_kwargs: object) -> bool:
        return True

    _stub_host_transitions(monkeypatch, _flip_hosted_status)

    monkeypatch.setattr(host_mod, "release_hosted_owner", _noop_reconcile)

    async def _apply_lifecycle(_pool: object, _incarnation: object, **_kwargs: object) -> str:
        """Host orchestration fake; real durable effects use the PG contract tests."""
        return "terminate"

    monkeypatch.setattr(host_mod, "apply_hosted_lifecycle", _apply_lifecycle)
    monkeypatch.setattr(
        "services.agent_runner.agent_host.invocation.apply_hosted_lifecycle", _apply_lifecycle
    )

    async def _no_force(*_args: object, **_kwargs: object) -> bool:
        # These cache/context fixtures carry no force command. Real owner/pointer
        # settlement and refusal are covered by test_hosted_force_quiescence.
        return False

    monkeypatch.setattr("base.agents.incarnation.hosted_force.original_host_force", _no_force)

    def _build(
        rows: dict[int, _Row],
        results: dict[int, list[dict[str, Any]]] | None = None,
        *,
        catalog: ModelCatalog | None = None,
    ) -> tuple[AgentHost, _FakeGraph, _FakePool]:
        graph = _FakeGraph(results or {})
        pool = _FakePool(rows)
        host = _host(
            pool=pool,
            checkpointer=object(),
            graph=graph,
            plugin_configs=host_plugin,
            catalog=model_catalog if catalog is None else catalog,
        )
        return host, graph, pool

    return _build


# ── 1. isolation ─────────────────────────────────────────────────────────────


class TestPendingInboundBackstop:
    async def test_stale_running_rows_qualified_by_the_scan(
        self, *, model_catalog: ModelCatalog
    ) -> None:
        """The hosted dispatcher scans only this machine's runnable rows. A
        fresh pending inbound wakes its agent; database timestamps identify backlog, while current turn progress must
        independently authorize cancellation."""
        pool = _PendingScanPool([(17, True, False), (23, False, True)])
        host = _host(catalog=model_catalog, pool=pool, checkpointer=object(), graph=object())

        candidates = await host.pending_inbound_wakes(180.0)

        assert [(c.agent_id, c.stale, c.recovery) for c in candidates] == [
            (17, True, False),
            (23, False, True),
        ]
        assert pool.params == (
            180.0,
            180.0,
            host._owner,
            host._owner,
            180.0,
            180.0,
            host._owner,
            "this-box",
        )
        assert "m.runtime_owner=%s" in pool.sql
        assert "force.target_generation=m.runtime_generation" in pool.sql
        assert "m.status = 'idling'" in pool.sql
        assert "m.status = 'running'" in pool.sql
        assert "m.machine = %s" in pool.sql
        assert "pending.status = 'pending'" in pool.sql

    async def test_scan_uses_the_reserved_control_pool(
        self, *, model_catalog: ModelCatalog
    ) -> None:
        """Turn-query saturation must not starve the durable recovery scan."""

        class _ForbiddenTurnPool:
            def connection(self) -> object:
                raise AssertionError("pending scan borrowed from the turn pool")

        control_pool = _PendingScanPool([(17, True, False)])
        host = _host(
            catalog=model_catalog,
            pool=cast(AsyncConnectionPool[Any], _ForbiddenTurnPool()),
            control_pool=cast(AsyncConnectionPool[Any], control_pool),
            checkpointer=object(),
            graph=object(),
        )

        candidates = await host.pending_inbound_wakes(180.0)

        assert [candidate.agent_id for candidate in candidates] == [17]


class _Drive(Protocol):
    def __call__(
        self,
        agent: int,
        runtime: object,
        slices: object,
        /,
        *,
        incarnation: object,
        resources: object,
    ) -> Awaitable[TurnOutcome]: ...


class TestSettlementReconciles:
    """A settlement disposes the inbounds its turn claimed.

    The settled abort disposes them (task #3615); a finished non-crashed
    turn runs the same pass at its own settlement too (#3999). This locks
    WHEN each pass dispatches — after the settle, and never for a crash.
    Their own gates live in `test_agent_host_abort_reconcile.py` /
    `test_agent_host_turn_reconcile.py`; the row-visible split in
    `services/agent_runner/agent_host/recovery/tests/test_reconcile_after_abort.py`.
    """

    async def _run_ending(
        self,
        wired: _Build,
        monkeypatch: pytest.MonkeyPatch,
        drive: _Drive,
        order: list[str],
    ) -> None:
        host, _, _ = wired({1: _Row()})

        async def settle_and_stamp(
            _pool: object,
            _incarnation: object,
            *,
            bus: object,
            exited: bool,
            crashed: bool,
            resources: object,
        ) -> TurnSettlement:
            order.append("settle")
            return TurnSettlement(
                stamp=TurnFatalStamp(applied=crashed, recrash=False), settled=not exited
            )

        async def reconcile(
            _pool: object, _checkpointer: object, _incarnation: object, *, resources: object
        ) -> None:
            order.append("reconcile")

        async def reconcile_turn(
            _pool: object, _checkpointer: object, _incarnation: object, *, resources: object
        ) -> None:
            order.append("reconcile-turn")

        monkeypatch.setattr(settlement, "settle_and_stamp_turn", settle_and_stamp)
        monkeypatch.setattr(settlement, "reconcile_inbounds_after_abort", reconcile)
        monkeypatch.setattr(settlement, "reconcile_inbounds_after_turn", reconcile_turn)
        monkeypatch.setattr(host, "_runtime_for", AsyncMock(return_value=object()))
        monkeypatch.setattr(host, "_drive_turns", drive)
        await asyncio.wait_for(host.run_turn(1), 2)

    async def test_settled_abort_reconciles_after_the_settle(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def drive(
            _agent: int,
            _runtime: object,
            _slices: object,
            *,
            incarnation: object,
            resources: object,
            checkpoints: Any = None,
        ) -> TurnOutcome:
            return TurnOutcome(exited=False, crashed=True, aborted=True)

        order: list[str] = []
        await self._run_ending(wired, monkeypatch, drive, order)
        assert order == ["settle", "reconcile"]

    async def test_unclassified_crash_leaves_the_reconcile_to_the_next_admission(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the fatal-abort settlement proves the checkpoint settled; an
        unclassified crash drops the runtime instead, and the next admission
        (or boot) reconciles."""

        async def drive(
            _agent: int,
            _runtime: object,
            _slices: object,
            *,
            incarnation: object,
            resources: object,
            checkpoints: Any = None,
        ) -> TurnOutcome:
            raise ValueError("unclassified crash")

        order: list[str] = []
        with pytest.raises(ValueError):
            await self._run_ending(wired, monkeypatch, drive, order)
        assert order == ["settle"]

    async def test_finished_turn_reconciles_after_the_settle(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The finished turn's flushed checkpoint settles its claimed rows
        (#3999): the pass runs right after the settle, never for a crash.
        A cancelled turn carries the same outcome shape without the flush —
        its unconfirmable claims re-deliver at-least-once (see the pass's
        docstring)."""

        async def drive(
            _agent: int,
            _runtime: object,
            _slices: object,
            *,
            incarnation: object,
            resources: object,
            checkpoints: Any = None,
        ) -> TurnOutcome:
            return TurnOutcome(exited=False, crashed=False)

        order: list[str] = []
        await self._run_ending(wired, monkeypatch, drive, order)
        assert order == ["settle", "reconcile-turn"]


def _assert_sdk_context(expected: AvaContext | None, *, bound: bool) -> None:
    assert getattr(ava, "context", None) is expected
    assert hasattr(ava, "context") is bound


class TestConcurrentAgentIsolation:
    @pytest.mark.parametrize("sdk_bound", [False, True])
    async def test_two_overlapping_turns_each_see_their_own_everything(
        self, wired: _Build, sdk_bound: bool
    ) -> None:
        """The load-bearing test of the whole hosted model.

        Both turns are held INSIDE the graph at the same time, so neither can
        pass by running to completion before the other starts — which is exactly
        how a process-per-agent assumption would sneak through.
        """
        if sdk_bound:
            pin_agent(999)
        else:
            pin_no_identity()
        rows = {
            11: _Row(overlay={"llm_model": "model-for-11", "marker": "plug-for-11"}),
            22: _Row(overlay={"llm_model": "model-for-22", "marker": "plug-for-22"}),
        }
        host, graph, _ = wired(rows)
        graph.gate(11)
        graph.gate(22)

        child_context = getattr(ava, "context", None)
        _assert_sdk_context(child_context, bound=sdk_bound)
        t1 = asyncio.create_task(host.run_turn(11))
        t2 = asyncio.create_task(host.run_turn(22))
        await asyncio.wait_for(graph.arrival(11).wait(), 2)
        await asyncio.wait_for(graph.arrival(22).wait(), 2)

        _assert_sdk_context(child_context, bound=sdk_bound)
        # Both are parked in their own turn right now — overlap is real.
        graph.gates[11].set()
        graph.gates[22].set()
        await asyncio.wait_for(asyncio.gather(t1, t2), 2)
        _assert_sdk_context(child_context, bound=sdk_bound)

        seen = {o.agent_id: o for o in graph.observations}
        assert set(seen) == {11, 22}, "identity must not leak between concurrent turns"
        assert seen[11].model == "model-for-11"
        assert seen[22].model == "model-for-22"
        assert seen[11].plugin_marker == "plug-for-11"
        assert seen[22].plugin_marker == "plug-for-22"
        # Per-agent handles, not one shared object.
        assert seen[11].llm is not seen[22].llm
        assert seen[11].llm.name == "model-for-11"
        assert seen[11].publisher.agent_id == 11
        assert seen[22].publisher.agent_id == 22

    async def test_nothing_leaks_after_a_turn_ends(self, wired: _Build) -> None:
        """Host turns carry their identity explicitly and never change the shared SDK slot."""
        host, _, _ = wired({11: _Row(overlay={"llm_model": "model-for-11"})})
        child_context = getattr(ava, "context", None)
        await asyncio.wait_for(host.run_turn(11), 2)
        assert getattr(ava, "context", None) is child_context


# ── 2. the config rebind ─────────────────────────────────────────────────────


# ── 3. the four-way turn loop ────────────────────────────────────────────────


# ── 4. runnability ───────────────────────────────────────────────────────────


# ── 5. the bounds ────────────────────────────────────────────────────────────


# ── the scheduler seam ───────────────────────────────────────────────────────


__all__ = ["_GatedGraph"]
