"""The hosted agent-runner's turn runner — `services/agent_host/host.py`.

`test_turn_dispatcher.py` locks WHEN an agent runs. This file locks WHAT running
means, and the contracts here are the ones that only exist because many agents
now share one process:

1. **Isolation** — two agents' turns running concurrently must not see each
   other's identity, framework config, plugin config, model, or event
   publisher. In process mode the OS gave this for free; here it is bought by
   three contextvar binds around the invocation, and this file is what proves
   they hold under real overlap rather than in sequence.
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
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field

import ava._boot
from services.agent_host.dispatcher import TurnScheduler
from services.agent_host.host import AgentHost, _config_fingerprint
from shared.config.turn_view import turn_settings
from shared.context import AvaContext
from shared.lm.factory import validate_model_config
from shared.plugin_config_registry import _PLUGIN_CONFIG_CLASSES, _PLUGIN_CONFIGS
from shared.plugin_config_view import turn_plugin_config


class _HostPluginConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    marker: str = Field(default="disk-default", json_schema_extra={"per_agent": True})


@pytest.fixture
def host_plugin() -> Iterator[None]:
    """One registered plugin, so plugin-scope overlay routing has an owner."""
    snap_classes = dict(_PLUGIN_CONFIG_CLASSES)
    snap_configs = dict(_PLUGIN_CONFIGS)
    _PLUGIN_CONFIG_CLASSES["hostplug"] = _HostPluginConfig
    _PLUGIN_CONFIGS["hostplug"] = _HostPluginConfig()
    yield
    _PLUGIN_CONFIG_CLASSES.clear()
    _PLUGIN_CONFIG_CLASSES.update(snap_classes)
    _PLUGIN_CONFIGS.clear()
    _PLUGIN_CONFIGS.update(snap_configs)


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
    def __init__(self, rows: dict[int, _Row]) -> None:
        self._rows = rows

    async def execute(self, _sql: str, params: tuple[Any, ...]) -> _FakeCursor:
        row = self._rows.get(params[0])
        return _FakeCursor(row.tuple if row is not None else None)


class _FakeConnCtx:
    """The `async with pool.connection()` shape, counting borrows."""

    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConn:
        self._pool.reads += 1
        return _FakeConn(self._pool.rows)

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakePool:
    """Enough of `AsyncConnectionPool` for the host's one query.

    Deliberately not a live pool: every contract in this file is about
    contextvars, asyncio ordering and cache bookkeeping, none of which a real
    Postgres would exercise differently. What a real DB WOULD add — that an
    overlay write lands in the column this reads — is one `UPDATE` away from
    trivial and is not what has ever broken.
    """

    def __init__(self, rows: dict[int, _Row]) -> None:
        self.rows = rows
        self.reads = 0

    def connection(self) -> _FakeConnCtx:
        return _FakeConnCtx(self)


class _PendingScanCursor:
    def __init__(self, rows: list[tuple[int, bool]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple[int, bool]]:
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

    def __init__(self, rows: list[tuple[int, bool]]) -> None:
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

    async def start(self) -> None: ...

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
    ) -> None:
        self.agent_id = agent_id
        self.model = model
        self.plugin_marker = plugin_marker
        self.llm = llm
        self.publisher = publisher


class _FakeGraph:
    """Stands in for the compiled graph.

    `ainvoke` reads the turn's ambient state from inside a CHILD TASK, which is
    where a LangGraph node actually runs — a bind that only survives in the
    calling coroutine would pass a naive assertion and fail in production, so
    the observation deliberately takes the harder path.
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
        plugin_cfg = cast(_HostPluginConfig, turn_plugin_config("hostplug"))
        return _Observation(
            agent_id=ava._boot.agent_id(),
            model=turn_settings.lm.llm_model,
            plugin_marker=plugin_cfg.marker,
            llm=cast(_Model, context.llm),
            publisher=cast(_Publisher, context.event_publisher),
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


_Build = Callable[..., "tuple[AgentHost, _FakeGraph, _FakePool]"]


def _stub_host_transitions(
    monkeypatch: pytest.MonkeyPatch,
    flip: Callable[..., Awaitable[bool]],
) -> None:
    import services.agent_host.host as host_mod
    from shared.runtime_incarnation import RuntimeIncarnation

    async def admit(
        pool: object,
        agent_id: int,
        _machine: str,
        owner: UUID,
        *,
        expected_from: str,
    ) -> RuntimeIncarnation | None:
        if not await flip(pool, agent_id, "running", expected_from=expected_from):
            return None
        return RuntimeIncarnation(agent_id, uuid4(), owner)

    async def settle(
        pool: object, incarnation: RuntimeIncarnation, *, release: bool = False
    ) -> bool:
        return await flip(pool, incarnation.agent_id, "idling", expected_from="running")

    monkeypatch.setattr(host_mod, "admit_hosted_runtime", admit)
    monkeypatch.setattr(host_mod, "settle_hosted_runtime", settle)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, host_plugin: None) -> _Build:
    """An `AgentHost` over fakes, with the per-agent build stubbed.

    The per-agent build is stubbed because it needs a live key.
    `boot_agent_scope` is replaced by a build that reads
    `turn_settings.lm.llm_model` exactly as the real one does, so a test can
    still tell whether the config bind was in effect when the model was built.
    """
    import services.agent_host.host as host_mod

    async def _noop_reconcile(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(host_mod, "_reconcile_claimed_inbounds_at_startup", _noop_reconcile)
    monkeypatch.setattr(host_mod, "_repair_dangling_tool_use_at_startup", _noop_reconcile)

    async def _fake_boot_agent_scope(_agent_id: int) -> _Model:
        return _Model(turn_settings.lm.llm_model)

    monkeypatch.setattr(host_mod, "boot_agent_scope", _fake_boot_agent_scope)

    def _allow_model_config(*, model: str | None = None) -> None:
        """Keep fake host tests independent of installed provider credentials."""

    monkeypatch.setattr(host_mod, "validate_model_config", _allow_model_config)

    def _fake_redis() -> object:
        """The publisher below never touches it; the host only passes it through."""
        return object()

    monkeypatch.setattr(host_mod, "get_async_redis", _fake_redis)

    monkeypatch.setattr(host_mod, "AgentEventPublisher", _Publisher)

    async def _flip_hosted_status(*_args: object, **_kwargs: object) -> bool:
        return True

    _stub_host_transitions(monkeypatch, _flip_hosted_status)

    monkeypatch.setattr(host_mod, "release_hosted_owner", _noop_reconcile)

    def _swallow_notify(_agent_id: int) -> None:
        """Overridden per-test where the notify itself is the contract."""

    monkeypatch.setattr(host_mod, "_notify_exit", _swallow_notify)

    def _build(
        rows: dict[int, _Row], results: dict[int, list[dict[str, Any]]] | None = None
    ) -> tuple[AgentHost, _FakeGraph, _FakePool]:
        graph = _FakeGraph(results or {})
        pool = _FakePool(rows)
        host = AgentHost(
            pool=pool,  # pyright: ignore[reportArgumentType]
            checkpointer=object(),  # pyright: ignore[reportArgumentType]
            graph=graph,  # pyright: ignore[reportArgumentType]
            machine="this-box",
        )
        return host, graph, pool

    return _build


# ── 1. isolation ─────────────────────────────────────────────────────────────


class TestPendingInboundBackstop:
    async def test_stale_running_rows_qualified_by_the_scan(self) -> None:
        """The hosted dispatcher scans only this machine's runnable rows. A
        fresh pending inbound wakes its agent; only the database predicate marks
        a long-silent one stale enough for cancellation recovery."""
        pool = _PendingScanPool([(17, True), (23, False)])
        host = AgentHost(
            pool=pool,  # pyright: ignore[reportArgumentType]
            checkpointer=object(),  # pyright: ignore[reportArgumentType]
            graph=object(),  # pyright: ignore[reportArgumentType]
            machine="this-box",
        )

        candidates = await host.pending_inbound_wakes(180.0)

        assert [(candidate.agent_id, candidate.stale) for candidate in candidates] == [
            (17, True),
            (23, False),
        ]
        assert pool.params == (180.0, 180.0, 180.0, 180.0, "this-box")
        assert "m.status = 'idling'" in pool.sql
        assert "m.status = 'running'" in pool.sql
        assert "m.machine = %s" in pool.sql
        assert "pending.status = 'pending'" in pool.sql


class TestConcurrentAgentIsolation:
    async def test_two_overlapping_turns_each_see_their_own_everything(self, wired: _Build) -> None:
        """The load-bearing test of the whole hosted model.

        Both turns are held INSIDE the graph at the same time, so neither can
        pass by running to completion before the other starts — which is exactly
        how a process-per-agent assumption would sneak through.
        """
        # Ids 11/22, never 1: tests/conftest.py pins the session-global process
        # slot `ava._boot._agent_id = 1` as a placeholder, so an agent numbered 1
        # would read back correctly even if the turn bind did nothing at all.
        rows = {
            11: _Row(overlay={"llm_model": "model-for-11", "marker": "plug-for-11"}),
            22: _Row(overlay={"llm_model": "model-for-22", "marker": "plug-for-22"}),
        }
        host, graph, _ = wired(rows)
        graph.gate(11)
        graph.gate(22)

        t1 = asyncio.create_task(host.run_turn(11))
        t2 = asyncio.create_task(host.run_turn(22))
        await asyncio.wait_for(graph.arrival(11).wait(), 2)
        await asyncio.wait_for(graph.arrival(22).wait(), 2)

        # Both are parked in their own turn right now — overlap is real.
        graph.gates[11].set()
        graph.gates[22].set()
        await asyncio.wait_for(asyncio.gather(t1, t2), 2)

        seen = {o.agent_id: o for o in graph.observations}
        assert set(seen) == {11, 22}, "identity must not leak between concurrent turns"
        assert seen[11].model == "model-for-11"
        assert seen[22].model == "model-for-22"
        assert seen[11].plugin_marker == "plug-for-11"
        assert seen[22].plugin_marker == "plug-for-22"
        # Per-agent handles, not one shared object.
        assert seen[11].llm is not seen[22].llm
        assert seen[11].llm.name == "model-for-11"  # pyright: ignore[reportAttributeAccessIssue]
        assert seen[11].publisher.agent_id == 11  # pyright: ignore[reportAttributeAccessIssue]
        assert seen[22].publisher.agent_id == 22  # pyright: ignore[reportAttributeAccessIssue]

    async def test_nothing_leaks_after_a_turn_ends(self, wired: _Build) -> None:
        """The binds are scoped to the turn, so the host itself is never left
        wearing an agent's identity — otherwise host-level code (eviction, the
        stats route, the daemon's own logging) would attribute itself to whoever
        ran last.

        Asserted on the turn contextvar rather than `ava._boot.agent_id()`,
        because that read legitimately falls through to the process bootstrap
        slot — which tests/conftest.py pins to 1 for the whole session, and which
        the real host never sets at all (it never calls `establish`).
        """
        from shared.turn_identity import current_turn_agent_id

        host, _, _ = wired({11: _Row(overlay={"llm_model": "model-for-11"})})
        assert current_turn_agent_id() is None
        await asyncio.wait_for(host.run_turn(11), 2)
        assert current_turn_agent_id() is None


# ── 2. the config rebind ─────────────────────────────────────────────────────


class TestConfigRebind:
    async def test_a_changed_overlay_rebuilds_the_runtime_and_the_turn_sees_it(
        self, wired: _Build
    ) -> None:
        """Write overlay -> wake -> the turn runs on the new value.

        The second turn must NOT reuse the cached runtime: it was built from the
        old config, so reusing it would keep the agent on its old model while
        `agents_meta` says otherwise.
        """
        rows = {1: _Row(overlay={"llm_model": "before"})}
        host, graph, _ = wired(rows)

        await asyncio.wait_for(host.run_turn(1), 2)
        assert graph.observations[-1].model == "before"
        assert host.stats.cache_misses == 1

        # The overlay write, as `ava.self.restart(config_overlay)` performs it.
        rows[1] = _Row(overlay={"llm_model": "after"})
        await asyncio.wait_for(host.run_turn(1), 2)

        assert graph.observations[-1].model == "after"
        assert graph.observations[-1].llm.name == "after", (  # pyright: ignore[reportAttributeAccessIssue]
            "the model must be REBUILT, not just re-read from settings"
        )
        assert host.stats.cache_misses == 2, "a changed config must miss the cache"
        assert host.stats.cache_hits == 0

    async def test_an_unchanged_overlay_hits_the_cache(self, wired: _Build) -> None:
        """The other half: re-reading the same row must not look like a change,
        or every turn would pay a cold build."""
        host, _, _ = wired({1: _Row(overlay={"llm_model": "steady"})})
        await asyncio.wait_for(host.run_turn(1), 2)
        await asyncio.wait_for(host.run_turn(1), 2)
        assert (host.stats.cache_misses, host.stats.cache_hits) == (1, 1)

    def test_the_fingerprint_ignores_key_order(self) -> None:
        """JSONB gives no key-order guarantee, so a fingerprint that depended on
        it would rebuild at random."""
        a = _config_fingerprint({"x": 1, "y": 2}, None)
        b = _config_fingerprint({"y": 2, "x": 1}, None)
        assert a == b
        assert a != _config_fingerprint({"x": 1, "y": 3}, None)

    def test_the_fingerprint_separates_the_two_maps(self) -> None:
        """`birth_config` and `config_overlay` are different columns with
        different provenance; a fingerprint that flattened them would call two
        genuinely different states equal."""
        assert _config_fingerprint({"x": 1}, None) != _config_fingerprint(None, {"x": 1})

    async def test_the_row_is_re_read_every_turn(self, wired: _Build) -> None:
        """The rebind only works if the read is per turn — caching the row with
        the runtime would make an overlay land only after an eviction."""
        host, _, pool = wired({1: _Row()})
        await asyncio.wait_for(host.run_turn(1), 2)
        await asyncio.wait_for(host.run_turn(1), 2)
        assert pool.reads == 2


# ── 3. the four-way turn loop ────────────────────────────────────────────────


class TestTurnLoop:
    async def test_a_turn_flips_status_running_then_idling(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        flips: list[tuple[int, str, str]] = []

        async def _flip(_pool: object, agent_id: int, to: str, *, expected_from: str) -> bool:
            flips.append((agent_id, to, expected_from))
            return True

        _stub_host_transitions(monkeypatch, _flip)
        host, _, _ = wired({1: _Row(status="idling")})

        await asyncio.wait_for(host.run_turn(1), 2)

        assert flips == [(1, "running", "idling"), (1, "idling", "running")]

    async def test_a_crashing_turn_restores_idling(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        flips: list[tuple[int, str, str]] = []

        async def _flip(_pool: object, agent_id: int, to: str, *, expected_from: str) -> bool:
            flips.append((agent_id, to, expected_from))
            return True

        async def _boom(*_args: object, **_kwargs: object) -> dict[str, Any]:
            raise RuntimeError("turn exploded")

        _stub_host_transitions(monkeypatch, _flip)
        host, graph, _ = wired({1: _Row(status="idling")})
        graph.ainvoke = _boom  # pyright: ignore[reportAttributeAccessIssue]

        with pytest.raises(RuntimeError, match="turn exploded"):
            await asyncio.wait_for(host.run_turn(1), 2)

        assert flips[-1] == (1, "idling", "running")

    async def test_exit_requested_does_not_restore_idling(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        flips: list[tuple[int, str, str]] = []

        async def _flip(_pool: object, agent_id: int, to: str, *, expected_from: str) -> bool:
            flips.append((agent_id, to, expected_from))
            return True

        _stub_host_transitions(monkeypatch, _flip)
        host, _, _ = wired(
            {1: _Row(status="idling")},
            {1: [{"exit_requested": True, "turn_idle": False, "restart_requested": False}]},
        )

        await asyncio.wait_for(host.run_turn(1), 2)

        assert flips == [(1, "running", "idling")]

    async def test_a_losing_start_flip_skips_the_turn(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        flips: list[tuple[int, str, str]] = []

        async def _flip(_pool: object, agent_id: int, to: str, *, expected_from: str) -> bool:
            flips.append((agent_id, to, expected_from))
            return False

        _stub_host_transitions(monkeypatch, _flip)
        host, graph, _ = wired({1: _Row(status="idling")})

        await asyncio.wait_for(host.run_turn(1), 2)

        assert flips == [(1, "running", "idling")]
        assert graph.observations == []
        assert host._runtimes == {}
        assert host.stats.cache_misses == 0

    async def test_restart_requested_restores_idling(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        flips: list[tuple[int, str, str]] = []

        async def _flip(_pool: object, agent_id: int, to: str, *, expected_from: str) -> bool:
            flips.append((agent_id, to, expected_from))
            return True

        _stub_host_transitions(monkeypatch, _flip)
        host, _, _ = wired(
            {1: _Row(status="idling")},
            {1: [{"exit_requested": False, "turn_idle": False, "restart_requested": True}]},
        )

        await asyncio.wait_for(host.run_turn(1), 2)

        assert flips[-1] == (1, "idling", "running")

    async def test_turn_idle_ends_the_task(self, wired: _Build) -> None:
        host, graph, _ = wired(
            {1: _Row()},
            {1: [{"exit_requested": False, "turn_idle": True, "restart_requested": False}]},
        )
        await asyncio.wait_for(host.run_turn(1), 2)
        assert len(graph.observations) == 1

    async def test_a_turn_boundary_re_invokes_on_the_same_thread(self, wired: _Build) -> None:
        """Neither flag set means "turn over, more may be pending" — the host
        goes round again rather than ending the task, so a burst drains in one
        task instead of needing a wake per turn."""
        host, graph, _ = wired(
            {1: _Row()},
            {
                1: [
                    {"exit_requested": False, "turn_idle": False, "restart_requested": False},
                    {"exit_requested": False, "turn_idle": False, "restart_requested": False},
                    {"exit_requested": False, "turn_idle": True, "restart_requested": False},
                ]
            },
        )
        await asyncio.wait_for(host.run_turn(1), 2)
        assert len(graph.observations) == 3

    async def test_exit_requested_is_terminal_and_notifies(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A terminate/restart winner ends the task AND tells the gateway, which
        is what a process did on its way out. Without the notify the row would
        sit `running` forever with nothing running."""
        import services.agent_host.host as host_mod

        notified: list[int] = []
        monkeypatch.setattr(host_mod, "_notify_exit", notified.append)
        host, graph, _ = wired(
            {1: _Row()},
            {1: [{"exit_requested": True, "turn_idle": False, "restart_requested": False}]},
        )
        await asyncio.wait_for(host.run_turn(1), 2)
        assert len(graph.observations) == 1
        assert notified == [1]

    async def test_exit_drops_the_cached_runtime(self, wired: _Build) -> None:
        """The next wake must start clean — the fresh-process half of a restart."""
        host, _, _ = wired(
            {1: _Row()},
            {1: [{"exit_requested": True, "turn_idle": False, "restart_requested": False}]},
        )
        await asyncio.wait_for(host.run_turn(1), 2)
        assert 1 not in host._runtimes

    async def test_restart_requested_drops_runtime_without_notify(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hosted restart answer: end the task, drop the cached runtime (the
        fresh-process half), and do NOT tell the gateway — there is no process
        exiting and the row must stay runnable for the next wake."""
        import services.agent_host.host as host_mod

        notified: list[int] = []
        monkeypatch.setattr(host_mod, "_notify_exit", notified.append)
        host, graph, _ = wired(
            {1: _Row()},
            {1: [{"exit_requested": False, "turn_idle": False, "restart_requested": True}]},
        )
        await asyncio.wait_for(host.run_turn(1), 2)
        assert len(graph.observations) == 1
        assert notified == []
        assert 1 not in host._runtimes

    async def test_a_crashing_turn_drops_the_runtime(self, wired: _Build) -> None:
        """A crash is the hosted equivalent of a process dying mid-turn, and a
        respawn re-runs the startup reconcile. Keeping the cached runtime would
        skip it on the retry and leave `claimed` rows unresolved."""
        host, graph, _ = wired({1: _Row()})

        async def _boom(*_a: object, **_k: object) -> dict[str, Any]:
            raise RuntimeError("turn exploded")

        await asyncio.wait_for(host.run_turn(1), 2)
        assert 1 in host._runtimes
        graph.ainvoke = _boom  # pyright: ignore[reportAttributeAccessIssue]
        with pytest.raises(RuntimeError, match="turn exploded"):
            await asyncio.wait_for(host.run_turn(1), 2)
        assert 1 not in host._runtimes


# ── 4. runnability ───────────────────────────────────────────────────────────


class TestRunnability:
    async def test_another_machines_agent_is_never_run(self, wired: _Build) -> None:
        """The dispatcher's PSUBSCRIBE is cluster-wide, so on a multi-runner
        cluster every runner sees every wake. Nothing but this check keeps two
        runners from both claiming one agent's inbound."""
        host, graph, _ = wired({1: _Row(machine="some-other-box")})
        await asyncio.wait_for(host.run_turn(1), 2)
        assert graph.observations == []
        assert host.stats.wakes_skipped == 1
        assert host.stats.turns_started == 0

    @pytest.mark.parametrize("status", ["terminated", "restarting"])
    async def test_unrunnable_statuses_are_skipped(self, wired: _Build, status: str) -> None:
        """A terminated agent's wake belongs to the delivery watchdog's resurrect
        path; `restarting` belongs to the respawn path. Either state means someone
        else owns this row right now."""
        host, graph, _ = wired({1: _Row(status=status)})
        await asyncio.wait_for(host.run_turn(1), 2)
        assert graph.observations == []
        assert host.stats.wakes_skipped == 1

    async def test_a_missing_row_is_skipped(self, wired: _Build) -> None:
        host, graph, _ = wired({})
        await asyncio.wait_for(host.run_turn(99), 2)
        assert graph.observations == []
        assert host.stats.wakes_skipped == 1

    async def test_a_skipped_wake_builds_no_runtime(self, wired: _Build) -> None:
        """The rejection must come BEFORE the cold build, or a burst of foreign
        wakes would evict every local agent's runtime."""
        host, _, _ = wired({1: _Row(machine="elsewhere")})
        await asyncio.wait_for(host.run_turn(1), 2)
        assert host._runtimes == {}


class TestRejectedModelConfig:
    async def test_an_unknown_model_is_rejected_before_the_runtime_build(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real registry rejects an unknown model without a provider key."""
        import services.agent_host.host as host_mod

        monkeypatch.setattr(host_mod, "validate_model_config", validate_model_config)
        boot_calls: list[int] = []
        error_events: list[str] = []

        async def _record_boot(agent_id: int) -> _Model:
            boot_calls.append(agent_id)
            return _Model(turn_settings.lm.llm_model)

        def _record_error(_message: str, *, event: str, **_details: object) -> None:
            error_events.append(event)

        monkeypatch.setattr(host_mod, "boot_agent_scope", _record_boot)
        monkeypatch.setattr(host_mod.logger, "error", _record_error)
        host, graph, _ = wired({1: _Row(overlay={"llm_model": "fable"})})

        await asyncio.wait_for(host.run_turn(1), 2)

        assert boot_calls == []
        assert graph.observations == []
        assert host.stats.config_rejected == 1
        assert host.stats.as_payload()["config_rejected"] == 1
        assert host.stats.turns_started == 0
        assert error_events == ["host_config_rejected"]

    async def test_a_fixed_model_config_builds_on_the_next_wake(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid replacement clears the rejection note and resumes the turn."""
        import services.agent_host.host as host_mod

        boot_calls: list[int] = []
        validated_models: list[str] = []

        def _validate_model(*, model: str) -> None:
            validated_models.append(model)
            if model == "fable":
                raise ValueError("unknown model 'fable'")

        async def _record_boot(agent_id: int) -> _Model:
            boot_calls.append(agent_id)
            return _Model(turn_settings.lm.llm_model)

        monkeypatch.setattr(host_mod, "validate_model_config", _validate_model)
        monkeypatch.setattr(host_mod, "boot_agent_scope", _record_boot)
        rows = {1: _Row(overlay={"llm_model": "fable"})}
        host, graph, _ = wired(rows)

        await asyncio.wait_for(host.run_turn(1), 2)
        assert boot_calls == []
        assert host._rejected_configs

        rows[1] = _Row(overlay={"llm_model": "gpt-5.6-sol"})
        await asyncio.wait_for(host.run_turn(1), 2)

        assert boot_calls == [1]
        assert len(graph.observations) == 1
        assert host.stats.turns_started == 1
        assert host.stats.config_rejected == 1
        assert host._rejected_configs == {}
        assert validated_models == ["fable", "gpt-5.6-sol"]

    async def test_the_same_rejected_config_logs_once_per_config_state(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated pending wakes stay quiet until the stored config changes."""
        import services.agent_host.host as host_mod

        error_events: list[str] = []

        def _reject_model(*, model: str) -> None:
            raise ValueError(f"unknown model '{model}'")

        def _record_error(_message: str, *, event: str, **_details: object) -> None:
            error_events.append(event)

        monkeypatch.setattr(host_mod, "validate_model_config", _reject_model)
        monkeypatch.setattr(host_mod.logger, "error", _record_error)
        rows = {1: _Row(overlay={"llm_model": "fable"})}
        host, _, _ = wired(rows)

        await asyncio.wait_for(host.run_turn(1), 2)
        await asyncio.wait_for(host.run_turn(1), 2)
        rows[1] = _Row(overlay={"llm_model": "fable-2"})
        await asyncio.wait_for(host.run_turn(1), 2)

        assert host.stats.config_rejected == 3
        assert error_events == ["host_config_rejected", "host_config_rejected"]


# ── 5. the bounds ────────────────────────────────────────────────────────────


class TestBounds:
    async def test_concurrent_turns_are_capped(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pool is sized as a statement about this bound, so the bound has
        to actually hold."""
        from shared.config import settings

        monkeypatch.setattr(settings.daemon, "host_max_concurrent_turns", 2)
        rows = {i: _Row() for i in (1, 2, 3)}
        host, graph, _ = wired(rows)
        for i in (1, 2, 3):
            graph.gate(i)
            graph.arrival(i)

        tasks = [asyncio.create_task(host.run_turn(i)) for i in (1, 2, 3)]
        await asyncio.wait_for(graph.arrival(1).wait(), 2)
        await asyncio.wait_for(graph.arrival(2).wait(), 2)
        for _ in range(8):
            await asyncio.sleep(0)
        assert not graph.arrived[3].is_set(), "the third turn must wait for a slot"

        graph.gates[1].set()
        await asyncio.wait_for(graph.arrival(3).wait(), 2)
        graph.gates[2].set()
        graph.gates[3].set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert host.stats.turns_started == 3

    async def test_the_lru_cap_evicts_the_least_recently_used(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from shared.config import settings

        monkeypatch.setattr(settings.daemon, "host_agent_cache_size", 2)
        host, _, _ = wired({i: _Row() for i in (1, 2, 3)})
        for i in (1, 2):
            await asyncio.wait_for(host.run_turn(i), 2)
        await asyncio.wait_for(host.run_turn(1), 2)  # 1 is now the most recent
        await asyncio.wait_for(host.run_turn(3), 2)
        assert set(host._runtimes) == {1, 3}, "2 was least recently used"

    async def test_the_idle_ttl_drops_a_silent_agent(
        self, wired: _Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The size cap alone keeps a long-silent agent warm forever on a
        lightly loaded runner; the TTL is the other half."""
        from shared.config import settings

        host, _, _ = wired({i: _Row() for i in (1, 2)})
        await asyncio.wait_for(host.run_turn(1), 2)
        monkeypatch.setattr(settings.daemon, "host_agent_idle_ttl_seconds", -1.0)
        await asyncio.wait_for(host.run_turn(2), 2)
        assert set(host._runtimes) == {2}, "1 aged out; 2 was just used"


# ── 6. the gate ──────────────────────────────────────────────────────────────


class TestGateFailsClosed:
    def _spec(self):
        from ops.spec import build_services

        return next(s for s in build_services() if s.session == "agent-host")

    def test_process_mode_gates_the_host_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops.spec import _gate_reason
        from shared.config import settings

        # Explicit process override: hosted is the default since 2026-09.
        monkeypatch.setattr(settings.daemon, "runner_mode", "process")
        assert _gate_reason(self._spec()) == "disabled (AVA_RUNNER_MODE is process)"

    def test_hosted_mode_lets_it_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops.spec import _gate_reason
        from shared.config import settings

        monkeypatch.setattr(settings.daemon, "runner_mode", "hosted")
        assert _gate_reason(self._spec()) is None

    def test_an_unreadable_mode_gates_OUT_rather_than_in(  # noqa: N802 — the direction is the point
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_gate_reason`'s own `except` fails OPEN — right for the 2026-08-08
        incident that installed it (a raising plugin gate killed the watchdog's
        whole roster), wrong here: failing open would START hosted mode on a
        cluster that never opted in, where every agent already has a process and
        the host would become a second claimant for the same inbound rows.

        So the read is made structurally unable to raise. This test breaks the
        setting read and asserts the service is still gated OUT — and that the
        fail-open wrapper never even saw an exception.
        """
        import ops.spec as spec_mod

        class _Exploding:
            def __getattr__(self, _name: str) -> object:
                raise RuntimeError("config domain unavailable in this process profile")

        monkeypatch.setattr(spec_mod.settings, "daemon", _Exploding())
        assert spec_mod._gate_reason(self._spec()) == "disabled (AVA_RUNNER_MODE is process)"

    def test_the_daemon_refuses_in_process_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The second line of the same defence: a hand-started daemon on a
        process cluster exits instead of double-serving agents that already have
        processes of their own."""
        from services.agent_host.daemon import _refuse_in_process_mode
        from shared.config import settings

        monkeypatch.setattr(settings.daemon, "runner_mode", "process")
        with pytest.raises(SystemExit) as exc:
            _refuse_in_process_mode()
        assert exc.value.code == 0

    def test_the_daemons_mode_read_also_cannot_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The daemon carries its own copy of the read rather than importing the
        gate's — an import is itself a thing that can fail, and a failed import
        inside `_gate_reason` would be caught by its fail-OPEN wrapper. Both
        copies must answer `process` when the setting is unreadable."""
        import services.agent_host.daemon as daemon_mod

        class _Exploding:
            def __getattr__(self, _name: str) -> object:
                raise RuntimeError("config domain unavailable")

        monkeypatch.setattr(daemon_mod.settings, "daemon", _Exploding())
        assert daemon_mod._runner_mode() == "process"


# ── the scheduler seam ───────────────────────────────────────────────────────


class TestSchedulerIntegration:
    async def test_a_wake_race_during_a_hosted_turn_still_runs_the_agent(
        self, wired: _Build
    ) -> None:
        """The dispatcher's wake-pending flag and the host's turn loop have to
        compose: a wake landing while a turn is in flight must produce another
        turn, not a lost one. `test_turn_dispatcher.py` proves the flag against a
        stub `run_turn`; this proves it against the real one.
        """
        host, graph, _ = wired({1: _Row()})
        graph.gate(1)
        sched = TurnScheduler(host.run_turn)

        sched.wake(1)
        await asyncio.wait_for(graph.arrival(1).wait(), 2)
        graph.arrived[1].clear()
        # The wake lands while the turn is parked inside the graph.
        sched.wake(1)
        graph.gates[1].set()
        await asyncio.wait_for(graph.arrival(1).wait(), 2)
        graph.gates[1].set()
        for _ in range(8):
            await asyncio.sleep(0)
        assert len(graph.observations) >= 2, "the wake during the turn must be served"
        await sched.aclose()
