"""Unit tests for agent/loop.py:main() + _write_effective_config (PR follow-up).

mutmut loop baseline: `main()` covers 77/84 no-tests mutants (whole section without dedicated test),
`_write_effective_config_to_restart_completed` covers remaining 7. This file uses boundary-fn mock and db_conn fixture to each cover a section.

main() test strategy — boundary mock:
- Actually running main loop needs LangGraph + Redis + LLM + DB full chain fixture, too much work;
  instead monkeypatch all external fns in the defining modules' namespaces — boot
  phase: `agent._process_boot` (`_MCPDaemon` / `build_chat_model` /
  `psycopg.AsyncConnection.connect` / `get_async_redis` / `AsyncPostgresSaver` /
  `build_graph` / `_write_effective_config_to_restart_completed` /
  `init_agent_process` / `_reconcile_claimed_inbounds_at_startup`), run loop:
  `agent.loop._invoke_graph_with_lifecycle_logging`, exit notify:
  `agent.lifecycle._notify_exit` — into spies, run main(id) assert
  order + finally runs all cleanup without missing any + exception path still runs cleanup.

Locked contract:
1. order: init_agent_process → set AGENT_ID → wraps.scan_and_load →
   MCP start → build_chat_model → create pool → connect redis →
   construct checkpointer → build_graph → (overlay) → write_effective_config → graph.ainvoke
2. finally block runs inbound_listener.close +
   _notify_exit + mcp_daemon.stop, whether ainvoke returns normally or raises (redis is process singleton, no aclose).
   shell/watcher session no longer reaped on exit (durable background work, PR1)
3. overlay non-None → apply_config_overlay called; None → not called
4. exception path: graph.ainvoke raise → exception propagates out of main, but finally runs fully
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest

import ava
from agent.loop import _write_effective_config_to_restart_completed, main
from shared.env_registry import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV
from tests.conftest import spawn_agent


@pytest.fixture(autouse=True)
def _restore_process_identity() -> Iterator[None]:
    """Snapshot and restore the process-global agent identity that a real
    ``main()`` mutates, so no test in this module leaks it into the rest of the
    session.

    ``main()`` calls ``ava._boot.establish`` (sets ``_agent_id`` / ``_owns_loop``)
    and forwards the id into ``os.environ["AVA_AGENT_ID"]`` (agent/loop.py) for
    child processes. None of that is otherwise undone. A leaked ``AVA_AGENT_ID``
    re-establishes identity via ``_boot._try_establish_from_env`` in any later
    test that reads ``agent_id()`` while ``_agent_id`` is None — which flips
    ``_owns_loop`` to False as a side effect. That single leak breaks 14 tests in
    tests/ava: the ``*_before_identity`` path tests (identity re-appears, paths
    resolve under ``workspaces/<id>`` instead of $HOME) and every
    ``ava.self.update()`` test (its owns-loop guard now refuses).
    """
    from ava import _boot

    saved_agent_id = _boot._agent_id
    saved_owns_loop = _boot._owns_loop
    saved_env = os.environ.get("AVA_AGENT_ID")
    try:
        yield
    finally:
        _boot._agent_id = saved_agent_id
        _boot._owns_loop = saved_owns_loop
        if saved_env is None:
            os.environ.pop("AVA_AGENT_ID", None)
        else:
            os.environ["AVA_AGENT_ID"] = saved_env


def _make_async_cm(value: object) -> object:
    """Build an async context manager that yields `value`."""

    class _ACM:
        async def __aenter__(self) -> object:
            return value

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    return _ACM()


def _setup_main_boundary_mocks(  # noqa: PLR0915 — single helper concentrating all main() boundary mock setup
    monkeypatch: pytest.MonkeyPatch,
    *,
    ainvoke_side_effect: BaseException | None = None,
    mcp_started: bool = True,
) -> dict[str, MagicMock]:
    """Patch every boundary fn `agent.loop.main` touches; return spy dict."""
    calls: list[tuple] = []

    def _spy(name: str, *, ret: object = None, is_async: bool = False) -> MagicMock:
        m = AsyncMock(return_value=ret) if is_async else MagicMock(return_value=ret)
        m._spy_name = name  # debug aid
        return m

    # Sync boundary fns
    fake_init_proc = _spy("init_agent_process")
    fake_build_chat = _spy("build_chat_model", ret=MagicMock(name="llm"))
    fake_write_eff = _spy("_write_effective_config_to_restart_completed")
    fake_mark_term = _spy("_notify_exit", ret=[])
    fake_hibernate = _spy("_notify_hibernate")
    fake_scan_load = _spy("scan_and_load")
    fake_apply_overlay = _spy("apply_config_overlay")

    # MCP daemon — class fake
    mcp_instance = MagicMock(name="mcp_daemon_instance")
    mcp_instance.spawn = AsyncMock(name="mcp_spawn")
    mcp_instance.await_ready = AsyncMock(name="mcp_await_ready")
    mcp_instance.stop = AsyncMock(name="mcp_stop")
    mcp_instance.started = mcp_started
    mcp_instance.socket_path = "/tmp/test-mcp.sock"  # noqa: S108
    fake_mcp_cls = MagicMock(name="_MCPDaemon", return_value=mcp_instance)

    # One LoggingConnectionPool (shared saver + ops) + one RedisInboundListener are
    # built at process start. Fake them by patching the imported class names;
    # the pool instance must support `async with`.
    db_pool = MagicMock(name="db_pool")

    def _pool_call(_url: str, **_kwargs: object) -> object:
        return _make_async_cm(db_pool)

    # MagicMock(side_effect=_pool_call) so the fake also has any attr the
    # real class exposes. `__getitem__` is wired to return the same mock so
    # subscript syntax `LoggingConnectionPool[psycopg.AsyncConnection](...)`
    # (PEP 695 generic) calls back through `side_effect`; without this the
    # subscript produces a fresh MagicMock whose calls bypass our factory.
    fake_pool_cls = MagicMock(name="LoggingConnectionPool", side_effect=_pool_call)
    fake_pool_cls.__getitem__ = MagicMock(return_value=fake_pool_cls)

    listener = MagicMock(name="inbound_listener")
    fake_listener_cls = MagicMock(name="RedisInboundListener", return_value=listener)

    redis_client = AsyncMock(name="redis_client")

    def _fake_get_async_redis() -> AsyncMock:
        return redis_client

    # AsyncPostgresSaver is now constructed directly (`AsyncPostgresSaver(conn=pool)`),
    # no longer via `from_conn_string` classmethod CM. Patch the class itself
    # to return our checkpointer stub.
    checkpointer = MagicMock(name="checkpointer")
    checkpoint_setup = AsyncMock(
        side_effect=AssertionError("agent boot must never run checkpoint schema DDL")
    )
    checkpointer.setup = checkpoint_setup
    # Startup reconcile reads state.messages via aget(); returning None means
    # "no prior checkpoint" → reconcile sees an empty committed-set, no-ops
    # on the pool stubs.
    checkpointer.aget = AsyncMock(return_value=None)
    fake_saver_cls = MagicMock(name="AsyncPostgresSaver", return_value=checkpointer)

    # build_graph + _invoke_graph_with_lifecycle_logging
    graph = MagicMock(name="graph")
    # _repair_dangling_tool_use_at_startup reads/writes graph state right after
    # build_graph; an empty checkpoint makes it a no-op for these orchestration tests.
    graph.aget_state = AsyncMock(return_value=MagicMock(values={"messages": []}))
    graph.aupdate_state = AsyncMock()
    fake_build_graph = _spy("build_graph", ret=graph)
    fake_invoke = AsyncMock(name="_invoke_graph_with_lifecycle_logging")
    if ainvoke_side_effect is not None:
        fake_invoke.side_effect = ainvoke_side_effect

    # Order spy — wrap every spy to record order
    def _wrap(spy: MagicMock, name: str) -> MagicMock:
        orig_call = spy.side_effect

        def _record(*a: object, **kw: object) -> object:
            calls.append((name, a, kw))  # pyright: ignore[reportUnknownMemberType]
            if orig_call is not None:
                if isinstance(orig_call, BaseException):
                    raise orig_call
                return orig_call(*a, **kw)
            return spy.return_value

        async def _arecord(*a: object, **kw: object) -> object:
            calls.append((name, a, kw))  # pyright: ignore[reportUnknownMemberType]
            if orig_call is not None:
                if isinstance(orig_call, BaseException):
                    raise orig_call
                if inspect.iscoroutinefunction(orig_call):
                    return await orig_call(*a, **kw)
                return orig_call(*a, **kw)
            return spy.return_value

        spy.side_effect = _arecord if isinstance(spy, AsyncMock) else _record
        return spy

    fake_init_proc = _wrap(fake_init_proc, "init_agent_process")
    fake_build_chat = _wrap(fake_build_chat, "build_chat_model")
    fake_build_graph = _wrap(fake_build_graph, "build_graph")
    fake_write_eff = _wrap(fake_write_eff, "_write_effective_config_to_restart_completed")
    fake_mark_term = _wrap(fake_mark_term, "_notify_exit")
    fake_hibernate = _wrap(fake_hibernate, "_notify_hibernate")
    fake_scan_load = _wrap(fake_scan_load, "scan_and_load")
    fake_apply_overlay = _wrap(fake_apply_overlay, "apply_config_overlay")
    fake_invoke = _wrap(fake_invoke, "_invoke_graph_with_lifecycle_logging")

    # Wrap mcp methods so order is recorded
    async def _mcp_spawn_wrapped() -> None:
        calls.append(("mcp.spawn", (), {}))  # pyright: ignore[reportUnknownMemberType]

    async def _mcp_await_ready_wrapped() -> None:
        calls.append(("mcp.await_ready", (), {}))  # pyright: ignore[reportUnknownMemberType]

    async def _mcp_stop_wrapped() -> None:
        calls.append(("mcp.stop", (), {}))  # pyright: ignore[reportUnknownMemberType]

    mcp_instance.spawn = AsyncMock(side_effect=_mcp_spawn_wrapped)
    mcp_instance.await_ready = AsyncMock(side_effect=_mcp_await_ready_wrapped)
    mcp_instance.stop = AsyncMock(side_effect=_mcp_stop_wrapped)

    async def _listener_close_wrapped() -> None:
        calls.append(("inbound_listener.close", (), {}))  # pyright: ignore[reportUnknownMemberType]

    listener.close = AsyncMock(side_effect=_listener_close_wrapped)

    # initialize_tracing is imported inside main() (lazy to defer the OTel /
    # OpenLLMetry import cost when disabled). Patch on the source module so the local import in
    # main() resolves to this fake — guards against test process having
    # AVA_TRACE_ENABLED=true in env from a developer shell.
    fake_initialize_tracing = _spy("initialize_tracing")
    monkeypatch.setattr("shared.trace.initialize_tracing", fake_initialize_tracing)

    # Apply all monkeypatches against the defining modules' namespaces
    monkeypatch.setattr("agent._process_boot.init_agent_process", fake_init_proc)
    monkeypatch.setattr("agent._process_boot._MCPDaemon", fake_mcp_cls)
    monkeypatch.setattr("agent._process_boot.build_chat_model", fake_build_chat)
    monkeypatch.setattr("agent._process_boot.build_graph", fake_build_graph)
    monkeypatch.setattr(
        "agent._process_boot._write_effective_config_to_restart_completed",
        fake_write_eff,
    )
    monkeypatch.setattr("agent.lifecycle._notify_exit", fake_mark_term)
    monkeypatch.setattr("agent.lifecycle._notify_hibernate", fake_hibernate)
    monkeypatch.setattr("agent.loop._invoke_graph_with_lifecycle_logging", fake_invoke)
    monkeypatch.setattr("ava._extend.scan_and_load", fake_scan_load)

    # Boundary patches: pool / listener / saver / async-redis
    monkeypatch.setattr("agent.loop.LoggingConnectionPool", fake_pool_cls)
    monkeypatch.setattr("agent._process_boot.RedisInboundListener", fake_listener_cls)
    monkeypatch.setattr("agent._process_boot.AsyncPostgresSaver", fake_saver_cls)
    monkeypatch.setattr("agent._process_boot.get_async_redis", _fake_get_async_redis)
    # Startup reconcile would attempt real pool SQL; stub it out — its
    # logic has dedicated coverage in tests/agent/test_db.py.
    monkeypatch.setattr("agent._process_boot._reconcile_claimed_inbounds_at_startup", AsyncMock())

    # apply_config_overlay is imported INSIDE main() (lazy) — patch shared.plugin_config_registry attr
    monkeypatch.setattr("shared.plugin_config_registry.apply_config_overlay", fake_apply_overlay)

    # main() establishes this process's identity (ava._boot.establish + the
    # AVA_AGENT_ID env forward). The autouse `_restore_process_identity` fixture
    # snapshots and restores all of that, so no per-test cleanup is needed here.

    return {
        "calls": calls,
        "init_proc": fake_init_proc,
        "mcp_cls": fake_mcp_cls,
        "mcp_instance": mcp_instance,
        "build_chat": fake_build_chat,
        "build_graph": fake_build_graph,
        "write_eff": fake_write_eff,
        "mark_term": fake_mark_term,
        "hibernate": fake_hibernate,
        "scan_load": fake_scan_load,
        "apply_overlay": fake_apply_overlay,
        "invoke": fake_invoke,
        "checkpointer": checkpointer,
        "checkpoint_setup": checkpoint_setup,
        "db_pool": db_pool,
        "listener": listener,
        "listener_cls": fake_listener_cls,
        "saver_cls": fake_saver_cls,
        "redis_client": redis_client,
        "graph": graph,
    }


# ───────────────────────────────────────────────────────────────────────────
# main() happy path
# ───────────────────────────────────────────────────────────────────────────


async def test_build_checkpointer_never_probes_or_mutates_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent boot treats checkpoint schema as a deployment precondition."""
    from agent._process_boot import _build_checkpointer

    pool = MagicMock(name="least_privilege_pool")
    pool.connection = MagicMock(
        side_effect=AssertionError("agent boot must not probe schema to decide whether to DDL")
    )
    checkpointer = MagicMock(name="checkpointer")
    checkpoint_setup = AsyncMock(
        side_effect=AssertionError("agent boot must never run checkpoint schema DDL")
    )
    checkpointer.setup = checkpoint_setup
    monkeypatch.setattr(
        "agent._process_boot.AsyncPostgresSaver", MagicMock(return_value=checkpointer)
    )
    monkeypatch.setattr("agent._process_boot._reconcile_claimed_inbounds_at_startup", AsyncMock())

    built = await _build_checkpointer(pool, 42)  # pyright: ignore[reportArgumentType]

    assert built is checkpointer
    pool.connection.assert_not_called()
    checkpoint_setup.assert_not_awaited()


class TestMainHappyPath:
    """main() normal return path (graph.ainvoke returns due to terminate inbound) — verify full order + finally."""

    async def test_calls_boundary_fns_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Boot order: claim → init_proc → scan_load → MCP start →
        build_chat → create pool → get_async_redis → construct checkpointer →
        build_graph → write_eff → invoke; finally:
        inbound_listener.close → mark_term → mcp.stop (redis is process-wide singleton, no aclose)."""
        spies = _setup_main_boundary_mocks(monkeypatch)

        await main(agent_id=42)

        spies["checkpoint_setup"].assert_not_awaited()

        names = [c[0] for c in spies["calls"]]
        # Boot phase ordering — index must be monotonic
        # claim_agent_row runs early in __main__.py before the heavy
        # import chain; agent.loop does not even import it, so main() cannot
        # call it — the old not-called spy assertion is now structural.
        boot_expected = [
            "init_agent_process",
            "mcp.spawn",
            "scan_and_load",
            "build_chat_model",
            "build_graph",
            "_write_effective_config_to_restart_completed",
            "mcp.await_ready",
            "_invoke_graph_with_lifecycle_logging",
        ]
        last_idx = -1
        for step in boot_expected:
            assert step in names, f"missing boot step {step!r}; got {names}"
            idx = names.index(step, last_idx + 1)  # pyright: ignore[reportUnknownMemberType]
            assert idx > last_idx, f"step {step!r} should appear after previous boot step"
            last_idx = idx

        # Finally phase — every cleanup must fire
        for cleanup in (
            "inbound_listener.close",
            "_notify_exit",
            "mcp.stop",
        ):
            assert cleanup in names, f"finally cleanup {cleanup!r} did not run; got {names}"

    async def test_exit_does_not_reap_shell_sessions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Durable background work (PR1): agent process exit must NOT tear down
        the agent's shell / monitor sessions. They survive
        terminate/restart/update (the PTY supervisor stays up) so a handed-off task
        or a resurrect-on-event watcher outlives the process. Guard that the
        finally block never invokes the session reaper (`ava.shell.kill_all`)."""
        _setup_main_boundary_mocks(monkeypatch)
        reaper = MagicMock(name="kill_all", return_value=0)
        monkeypatch.setattr("ava.shell.kill_all", reaper)
        await main(agent_id=42)
        reaper.assert_not_called()

    async def test_notify_exit_after_db_close(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_notify_exit after inbound_listener.close — listener first releases its exclusive
        Redis subscription conn, then notify gateway to finalize. Gateway side does status flip + close page +
        publish PageClosed, agent side only sends POST, no longer publishes itself."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=42)
        names = [c[0] for c in spies["calls"]]
        assert names.index("inbound_listener.close") < names.index("_notify_exit")  # pyright: ignore[reportUnknownMemberType]

    async def test_mcp_stop_last(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MCP stop is last in finally — comment says "will close Chrome/Playwright" slow, left till last."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=42)
        names = [c[0] for c in spies["calls"]]
        assert names.index("mcp.stop") == len(names) - 1  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    async def test_sets_ava_agent_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ava.self.AGENT_ID is set to the passed-in agent_id inside main (replacing module-load default 1)."""
        _setup_main_boundary_mocks(monkeypatch)

        original = ava._boot._agent_id
        try:
            await main(agent_id=777)
            assert ava.self.AGENT_ID == 777
        finally:
            ava._boot._agent_id = original

    async def test_build_chat_uses_settings_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """build_chat_model receives settings.lm.llm_model — verify wire."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        from shared.config import settings

        await main(agent_id=42)
        # build_chat called once with settings.lm.llm_model as first positional
        spy = spies["build_chat"]
        assert spy.call_count == 1
        args = spy.call_args.args
        assert args[0] == settings.lm.llm_model

    async def test_write_effective_config_called_with_agent_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=555)
        spies["write_eff"].assert_called_once()
        assert spies["write_eff"].call_args.args[0] == 555

    async def test_notify_exit_called_with_agent_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=111)
        spies["mark_term"].assert_called_once()
        assert spies["mark_term"].call_args.args[0] == 111

    async def test_invoke_called_with_agent_id_and_ctx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_invoke_graph_with_lifecycle_logging(graph, agent_id, ctx) — verify
        positional shape so a kwarg swap mutation is caught."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=333)
        # invoke is wrapped — recorded under "calls"
        invoke_calls = [c for c in spies["calls"] if c[0] == "_invoke_graph_with_lifecycle_logging"]
        assert len(invoke_calls) == 1  # pyright: ignore[reportUnknownArgumentType]
        # Current code uses positional args (graph, agent_id, ctx)
        _name, args, _kwargs = invoke_calls[0]
        # graph + agent_id + ctx (positional). Verify exact shape so
        # arg-replace-with-None mutations get caught.
        assert args[0] is spies["graph"], f"first arg should be graph; got {args[0]!r}"
        assert args[1] == 333, f"second arg should be agent_id=333; got {args[1]!r}"
        # third arg is AvaContext — verify its fields wire correctly
        ctx = args[2]
        assert ctx is not None
        assert ctx.ops_pool is spies["db_pool"]  # pyright: ignore[reportUnknownMemberType]
        assert ctx.llm is spies["build_chat"].return_value  # pyright: ignore[reportUnknownMemberType]

    async def test_init_agent_process_called_with_agent_id_kwarg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """init_agent_process(agent_id=N) — kwarg=None mutation should fail."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=42)
        assert spies["init_proc"].call_count == 1
        # init_agent_process is called with keyword arg agent_id
        kwargs = spies["init_proc"].call_args.kwargs
        assert kwargs.get("agent_id") == 42, (
            f"init_agent_process should get agent_id=42; got kwargs={kwargs!r}"
        )

    async def test_mcp_daemon_constructed_with_agent_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_MCPDaemon(agent_id) — None mutation would lose per-agent socket separation."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=42)
        assert spies["mcp_cls"].call_count == 1
        args = spies["mcp_cls"].call_args.args
        assert args[0] == 42, f"_MCPDaemon should get agent_id=42; got {args!r}"

    async def test_pools_and_listener_use_settings_db_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shared LoggingConnectionPool wires from settings.data_plane.db_url and
        the RedisInboundListener wires from settings.data_plane.redis_url — swapped-URL
        mutation would route the process to the wrong DB/Redis.

        Redis URL is now via `shared.redis_client.get_async_redis()` for the
        event publisher and via `RedisInboundListener` for inbound wake
        (process singleton from settings.data_plane.redis_url); event-publisher wiring
        is covered in tests/shared/test_redis_client.py.
        """
        from shared.config import settings

        captured_pool_urls: list[str] = []

        def _capture_pool(url: str, **_kw: object) -> object:
            captured_pool_urls.append(url)
            return _make_async_cm(MagicMock(name="captured_pool"))

        listener_urls: list[str] = []

        def _capture_redis_listener(redis_url: str, agent_id: int) -> MagicMock:
            listener_urls.append(redis_url)
            instance = MagicMock(name="captured_redis_listener")
            instance.close = AsyncMock()
            return instance

        _setup_main_boundary_mocks(monkeypatch)
        pool_mock = MagicMock(name="LoggingConnectionPool", side_effect=_capture_pool)
        pool_mock.__getitem__ = MagicMock(return_value=pool_mock)
        monkeypatch.setattr("agent.loop.LoggingConnectionPool", pool_mock)
        monkeypatch.setattr(
            "agent._process_boot.RedisInboundListener",
            MagicMock(name="RedisInboundListener", side_effect=_capture_redis_listener),
        )

        await main(agent_id=42)
        # One pool construction (db_url) + one listener construction (redis_url).
        assert captured_pool_urls == [settings.data_plane.db_url]
        assert listener_urls == [settings.data_plane.redis_url]

    async def test_async_postgres_saver_constructed_with_db_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AsyncPostgresSaver(conn=db_pool) — the saver must share the single
        agent pool; wiring it elsewhere would silently double the per-agent
        connection budget."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=42)
        assert spies["saver_cls"].call_count == 1
        call = spies["saver_cls"].call_args
        assert call.kwargs.get("conn") is spies["db_pool"], (
            f"AsyncPostgresSaver should receive conn=db_pool; got call={call!r}"
        )

    async def test_build_graph_called_with_checkpointer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_graph(checkpointer) — None mutation would build orphaned graph."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=42)
        assert spies["build_graph"].call_count == 1
        args = spies["build_graph"].call_args.args
        assert args[0] is spies["checkpointer"], (
            f"build_graph should get the checkpointer instance; got {args!r}"
        )

    async def test_process_exit_log_carries_agent_id_and_reason(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        """finally block process_exit log: agent_id + reason + pid all correctly filled.
        kwarg=None mutation can lose/mistake structured fields; log capture can catch."""
        _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=4242)
        # find the process_exit record
        exit_records = [
            r
            for r in loguru_records
            if r.get("extra", {}).get("event") == "process_exit"  # pyright: ignore[reportUnknownMemberType]
        ]
        assert len(exit_records) >= 1, (  # pyright: ignore[reportUnknownArgumentType]
            f"expected at least 1 process_exit log; got {[r['message'] for r in loguru_records]}"
        )
        rec = exit_records[0]
        extra = rec["extra"]
        assert extra["agent_id"] == 4242, f"agent_id wrong: {extra}"
        assert extra["reason"] == "normal", f"reason should be 'normal'; got {extra}"
        assert extra["pid"] == os.getpid(), f"pid should be self pid; got {extra}"


# ───────────────────────────────────────────────────────────────────────────
# config_overlay branch
# ───────────────────────────────────────────────────────────────────────────


class TestMainConfigOverlay:
    async def test_overlay_none_skips_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config_overlay=None → apply_config_overlay not called."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=42, config_overlay=None)
        spies["apply_overlay"].assert_not_called()

    async def test_overlay_empty_dict_skips_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty dict is falsy → not call apply (code uses `if config_overlay`)."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=42, config_overlay={})
        spies["apply_overlay"].assert_not_called()

    async def test_overlay_with_data_invokes_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-empty overlay dict → apply_config_overlay(overlay, scope=...) called twice:
        once with scope='framework' (before build_chat_model) and once with
        scope='plugin' (after build_graph's bind_from_disk)."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        overlay: dict[str, object] = {"llm_model": "deepseek-v3.2"}
        await main(agent_id=42, config_overlay=overlay)
        assert spies["apply_overlay"].call_count == 2
        for call in spies["apply_overlay"].call_args_list:
            assert call.args[0] == overlay
        scopes = [call.kwargs.get("scope") for call in spies["apply_overlay"].call_args_list]
        assert scopes == ["framework", "plugin"]

    async def test_overlay_apply_before_write_effective(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """apply_config_overlay before _write_effective_config — snapshot reflects post-overlay state (not pre-overlay)."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=42, config_overlay={"k": "v"})
        names = [c[0] for c in spies["calls"]]
        assert names.index("apply_config_overlay") < names.index(  # pyright: ignore[reportUnknownMemberType]
            "_write_effective_config_to_restart_completed"
        )


# ───────────────────────────────────────────────────────────────────────────
# birth_config branch — the second stored per-agent map (frozen-lifecycle fields)
# ───────────────────────────────────────────────────────────────────────────


class TestMainBirthConfig:
    async def test_none_skips_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=42, birth_config=None)
        spies["apply_overlay"].assert_not_called()

    async def test_applied_framework_scope_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The frozen set is framework-only, so the birth stamp has no plugin-scope
        half — one call, not the overlay's two."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        birth: dict[str, object] = {"llm_model": "deepseek-v4-pro"}
        await main(agent_id=42, birth_config=birth)
        assert spies["apply_overlay"].call_count == 1
        call = spies["apply_overlay"].call_args_list[0]
        assert call.args[0] == birth
        assert call.kwargs.get("scope") == "framework"

    async def test_birth_applied_before_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """THE resolution order: `config_overlay > birth_config`. Both write the same
        settings singleton via set_field, so whichever applies LAST wins — the overlay
        must be second."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        birth: dict[str, object] = {"llm_model": "born-with"}
        overlay: dict[str, object] = {"llm_model": "chosen"}
        await main(agent_id=42, config_overlay=overlay, birth_config=birth)
        applied = [c.args[0] for c in spies["apply_overlay"].call_args_list]
        assert applied[0] == birth
        assert applied[1] == overlay

    async def test_applied_before_build_chat_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A frozen llm_model must reach the LLM client this process actually builds."""
        spies = _setup_main_boundary_mocks(monkeypatch)
        await main(agent_id=42, birth_config={"llm_model": "born-with"})
        names = [c[0] for c in spies["calls"]]
        assert names.index("apply_config_overlay") < names.index("build_chat_model")  # pyright: ignore[reportUnknownMemberType]


# ───────────────────────────────────────────────────────────────────────────
# Exception path — graph.ainvoke raises → finally must still run
# ───────────────────────────────────────────────────────────────────────────


class TestMainExceptionPath:
    """When ainvoke raises, finally block still runs full cleanup — cannot bypass due to exception.
    `_invoke_graph_with_lifecycle_logging` re-raises ainvoke exception (it logs + publish Error then raises), propagating to main triggers finally."""

    async def test_runtime_error_runs_cleanups_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spies = _setup_main_boundary_mocks(
            monkeypatch,
            ainvoke_side_effect=RuntimeError("boom"),
        )
        with pytest.raises(RuntimeError, match="boom"):
            await main(agent_id=42)
        spies["checkpoint_setup"].assert_not_awaited()
        # All finally cleanup must have run
        names = [c[0] for c in spies["calls"]]
        for cleanup in (
            "inbound_listener.close",
            "_notify_exit",
            "mcp.stop",
        ):
            assert cleanup in names, f"exception path missed cleanup {cleanup!r}"

    async def test_cancelled_error_runs_cleanups_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spies = _setup_main_boundary_mocks(
            monkeypatch,
            ainvoke_side_effect=asyncio.CancelledError(),
        )
        with pytest.raises(asyncio.CancelledError):
            await main(agent_id=42)
        names = [c[0] for c in spies["calls"]]
        for cleanup in (
            "inbound_listener.close",
            "_notify_exit",
            "mcp.stop",
        ):
            assert cleanup in names, f"cancelled path missed cleanup {cleanup!r}"

    async def test_system_exit_runs_cleanups_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SystemExit (signal handler path) also must run finally — guard against 159/160 silent death regression. SystemExit is not Exception subclass, finally still runs."""
        spies = _setup_main_boundary_mocks(
            monkeypatch,
            ainvoke_side_effect=SystemExit("signal:SIGHUP"),
        )
        with pytest.raises(SystemExit):
            await main(agent_id=42)
        names = [c[0] for c in spies["calls"]]
        assert "_notify_exit" in names
        assert "mcp.stop" in names

    async def test_hibernate_signal_routes_to_notify_hibernate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hibernation swap-out signal (SIGUSR1) runs the same finally cleanups
        but routes the process-end notify to _notify_hibernate (park 'hibernating'),
        NOT _notify_exit (finalize 'terminated'). The routing channel is the module
        flag the SIGUSR1 handler sets synchronously before raising (the flag — not
        the SystemExit message — survives asyncio's CancelledError conversion), so
        this test sets it exactly as the handler does."""
        import agent.lifecycle as _lc

        spies = _setup_main_boundary_mocks(
            monkeypatch,
            ainvoke_side_effect=SystemExit("signal:SIGUSR1"),
        )
        try:
            _lc._hibernate_requested = True  # what the SIGUSR1 handler sets
            with pytest.raises(SystemExit):
                await main(agent_id=42)
        finally:
            _lc._hibernate_requested = False  # one-shot flag: never leak to other tests
        names = [c[0] for c in spies["calls"]]
        assert "inbound_listener.close" in names
        assert "mcp.stop" in names
        assert "_notify_hibernate" in names
        assert "_notify_exit" not in names  # the swap-out path must not finalize 'terminated'


# ───────────────────────────────────────────────────────────────────────────
# _write_effective_config_to_restart_completed integration test
# ───────────────────────────────────────────────────────────────────────────


class TestWriteEffectiveConfigToRestartCompleted:
    """UPDATE inbound_messages SET payload = jsonb_set(...) WHERE id = (SELECT
    latest restart_completed). Directly INSERT row + call fn + SELECT verify payload."""

    def test_updates_latest_restart_completed_payload(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There is a pending restart_completed → payload.effective_config is filled into snapshot."""
        agent_id = spawn_agent()
        # INSERT a restart_completed payload (containing config_overlay)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
                "VALUES (%s, %s, 'restart_completed', 'system', %s::jsonb)",
                (agent_id, "restart completed", json.dumps({"config_overlay": {"a": 1}})),
            )
        db_conn.commit()

        _write_effective_config_to_restart_completed(agent_id)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM inbound_messages "
                "WHERE agent_id = %s AND kind = 'restart_completed' "
                "ORDER BY id DESC LIMIT 1",
                (agent_id,),
            )
            row = cur.fetchone()
        assert row is not None
        payload = row[0]
        assert "effective_config" in payload, f"missing effective_config; got {payload}"
        assert "config_overlay" in payload, (
            "original config_overlay should not be overwritten (jsonb_set only modifies effective_config key)"
        )
        assert payload["config_overlay"] == {"a": 1}

    def test_updates_only_latest_when_multiple_restart_completed(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple restart_completed: ORDER BY id DESC LIMIT 1 — only modifies the latest,
        older payload untouched (mutation changing DESC → ASC would modify older, this test can kill)."""
        agent_id = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
                "VALUES (%s, 'older', 'restart_completed', 'system', %s::jsonb) RETURNING id",
                (agent_id, json.dumps({"marker": "older"})),
            )
            older_id = cur.fetchone()[0]  # type: ignore[index]
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
                "VALUES (%s, 'newer', 'restart_completed', 'system', %s::jsonb) RETURNING id",
                (agent_id, json.dumps({"marker": "newer"})),
            )
            newer_id = cur.fetchone()[0]  # type: ignore[index]
        db_conn.commit()
        assert older_id < newer_id

        _write_effective_config_to_restart_completed(agent_id)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, payload FROM inbound_messages "
                "WHERE agent_id = %s AND kind = 'restart_completed' ORDER BY id",
                (agent_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 2
        older_payload = rows[0][1]
        newer_payload = rows[1][1]
        # older should not have effective_config
        assert "effective_config" not in older_payload, (
            f"DESC LIMIT 1 broken — older row got updated: {older_payload}"
        )
        # newer should have
        assert "effective_config" in newer_payload

    def test_no_restart_completed_is_noop(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No pending restart_completed (spawn/resurrect boot path) → UPDATE rowcount=0
        doesn't raise. Also don't accidentally affect other kind."""
        agent_id = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
                "VALUES (%s, 'chat msg', 'chat', 'user', %s::jsonb)",
                (agent_id, json.dumps({"keep": True})),
            )
        db_conn.commit()

        # Should not raise even though there's no restart_completed row
        _write_effective_config_to_restart_completed(agent_id)

        # The chat row's payload was not touched
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM inbound_messages WHERE agent_id = %s AND kind = 'chat'",
                (agent_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == {"keep": True}
        assert "effective_config" not in row[0]

    def test_filters_by_agent_id(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple agent restart_completed rows in same table: only modify this agent_id's, not accidentally others.
        mutation removing WHERE agent_id = %s would cross-modify; this test can kill."""
        agent_a = spawn_agent()
        agent_b = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
                "VALUES (%s, 'a', 'restart_completed', 'system', '{}'::jsonb)",
                (agent_a,),
            )
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
                "VALUES (%s, 'b', 'restart_completed', 'system', '{}'::jsonb)",
                (agent_b,),
            )
        db_conn.commit()

        _write_effective_config_to_restart_completed(agent_a)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT agent_id, payload FROM inbound_messages "
                "WHERE kind = 'restart_completed' ORDER BY agent_id",
            )
            rows = cur.fetchall()
        by_agent = {r[0]: r[1] for r in rows}
        assert "effective_config" in by_agent[agent_a]
        assert "effective_config" not in by_agent[agent_b], (
            f"_write mistakenly modified agent_b payload: {by_agent[agent_b]}"
        )


# ───────────────────────────────────────────────────────────────────────────
# run() — config-overlay JSON parsing + arg propagation
# ───────────────────────────────────────────────────────────────────────────


class TestRunOverlayParsing:
    """`run()`'s `$AVA_AGENT_CONFIG_OVERLAY` JSON parsing + propagation to main().
    The overlay arrives in the env, not on argv: it may set any Settings field
    (a provider api_key included) and `ps` shows argv to any local user (#974).
    Lock mutmut survivors: overlay parsing truthy branch / dict validation / asyncio.run
    receives main() call indeed with config_overlay= not None."""

    def _patch_run_chain(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        """Patch assert_schema_current / signal handler / main / asyncio.run,
        capture all calls so test can assert."""
        from agent.loop import run

        calls: list[tuple] = []
        captured_coro: list[object] = []

        def _fake_assert_schema(url: object) -> None:
            calls.append(("assert_schema_current", url))  # pyright: ignore[reportUnknownMemberType]

        def _fake_install_handlers() -> None:
            calls.append(("install_handlers", None))  # pyright: ignore[reportUnknownMemberType]

        async def _coro_placeholder() -> None:
            pass

        def _fake_main_sync(
            agent_id: int, config_overlay: object = None, birth_config: object = None
        ) -> object:
            calls.append(("main", agent_id, config_overlay, birth_config))  # pyright: ignore[reportUnknownMemberType]
            return _coro_placeholder()

        def _fake_run(coro: object) -> None:
            captured_coro.append(coro)
            import contextlib

            with contextlib.suppress(Exception):
                coro.close()  # type: ignore[attr-defined]

        monkeypatch.setattr("shared.migrations.assert_schema_current", _fake_assert_schema)
        monkeypatch.setattr("agent.loop._install_lifecycle_signal_handlers", _fake_install_handlers)
        monkeypatch.setattr("agent.loop.main", _fake_main_sync)
        monkeypatch.setattr("agent.loop.asyncio.run", _fake_run)

        return {"calls": calls, "coros": captured_coro, "run": run}

    def test_overlay_json_parsed_and_propagated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`$AVA_AGENT_CONFIG_OVERLAY='{"k":"v"}'` → main() receives dict {"k": "v"}."""
        bundle = self._patch_run_chain(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("sys.argv", ["python -m agent", "--agent-id", "1"])
        monkeypatch.setenv(AGENT_CONFIG_OVERLAY_ENV, '{"k": "v"}')
        bundle["run"]()
        main_calls = [c for c in bundle["calls"] if c[0] == "main"]
        assert len(main_calls) == 1  # pyright: ignore[reportUnknownArgumentType]
        agent_id, overlay = main_calls[0][1], main_calls[0][2]
        assert agent_id == 1
        assert overlay == {"k": "v"}

    def test_no_overlay_arg_passes_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No overlay env var → main() receives None (`if config_overlay:` falsy path)."""
        bundle = self._patch_run_chain(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("sys.argv", ["python -m agent", "--agent-id", "1"])
        monkeypatch.delenv(AGENT_CONFIG_OVERLAY_ENV, raising=False)
        bundle["run"]()
        main_calls = [c for c in bundle["calls"] if c[0] == "main"]
        assert len(main_calls) == 1  # pyright: ignore[reportUnknownArgumentType]
        assert main_calls[0][2] is None

    def test_birth_config_json_parsed_and_propagated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`$AVA_AGENT_BIRTH_CONFIG` is the replay channel: whatever the launcher
        read off the row must arrive at main() as its own argument, not merged
        into the overlay."""
        bundle = self._patch_run_chain(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("sys.argv", ["python -m agent", "--agent-id", "1"])
        monkeypatch.setenv(AGENT_CONFIG_OVERLAY_ENV, '{"k": "v"}')
        monkeypatch.setenv(AGENT_BIRTH_CONFIG_ENV, '{"llm_model": "born-with"}')
        bundle["run"]()
        main_calls = [c for c in bundle["calls"] if c[0] == "main"]
        assert main_calls[0][2] == {"k": "v"}
        assert main_calls[0][3] == {"llm_model": "born-with"}

    def test_no_birth_config_arg_passes_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bundle = self._patch_run_chain(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("sys.argv", ["python -m agent", "--agent-id", "1"])
        monkeypatch.delenv(AGENT_BIRTH_CONFIG_ENV, raising=False)
        bundle["run"]()
        main_calls = [c for c in bundle["calls"] if c[0] == "main"]
        assert main_calls[0][3] is None

    def test_birth_config_non_dict_raises_system_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = self._patch_run_chain(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("sys.argv", ["python -m agent", "--agent-id", "1"])
        monkeypatch.setenv(AGENT_BIRTH_CONFIG_ENV, '"hello"')
        with pytest.raises(SystemExit, match="must be a JSON object"):
            bundle["run"]()
        assert not any(c[0] == "main" for c in bundle["calls"])  # pyright: ignore[reportUnknownArgumentType]

    def test_birth_config_env_var_is_popped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run() pops it, so the agent's own children (shell sessions, watchers,
        which inherit this env) never carry the birth config onward."""
        bundle = self._patch_run_chain(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("sys.argv", ["python -m agent", "--agent-id", "1"])
        monkeypatch.setenv(AGENT_BIRTH_CONFIG_ENV, '{"llm_model": "born-with"}')
        bundle["run"]()
        assert AGENT_BIRTH_CONFIG_ENV not in os.environ

    def test_overlay_non_dict_raises_system_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`$AVA_AGENT_CONFIG_OVERLAY='"a string"'` (valid JSON, not an object) → SystemExit.
        mutmut changing `if not isinstance` → `if isinstance` should be caught by this test."""
        bundle = self._patch_run_chain(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("sys.argv", ["python -m agent", "--agent-id", "1"])
        monkeypatch.setenv(AGENT_CONFIG_OVERLAY_ENV, '"hello"')
        with pytest.raises(SystemExit, match="must be a JSON object"):
            bundle["run"]()
        assert not any(c[0] == "main" for c in bundle["calls"])  # pyright: ignore[reportUnknownArgumentType]

    def test_overlay_non_dict_error_message_includes_actual_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SystemExit message includes actual type name — `type(None).__name__` mutation changed to 'NoneType', this test can catch."""
        bundle = self._patch_run_chain(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("sys.argv", ["python -m agent", "--agent-id", "1"])
        monkeypatch.setenv(AGENT_CONFIG_OVERLAY_ENV, "[1,2,3]")
        with pytest.raises(SystemExit, match=r"list"):
            bundle["run"]()

    def test_assert_schema_called_with_db_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """assert_schema_current(settings.data_plane.db_url) — None mutation would skip
        schema check or pass garbage."""
        from shared.config import settings

        bundle = self._patch_run_chain(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("sys.argv", ["python -m agent", "--agent-id", "1"])
        bundle["run"]()
        schema_calls = [c for c in bundle["calls"] if c[0] == "assert_schema_current"]
        assert len(schema_calls) == 1  # pyright: ignore[reportUnknownArgumentType]
        assert schema_calls[0][1] == settings.data_plane.db_url

    def test_main_receives_args_agent_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main(args.agent_id) — None mutation would pass wrong id."""
        bundle = self._patch_run_chain(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("sys.argv", ["python -m agent", "--agent-id", "12345"])
        bundle["run"]()
        main_calls = [c for c in bundle["calls"] if c[0] == "main"]
        assert main_calls[0][1] == 12345

    def test_main_receives_overlay_kwarg_not_swapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`asyncio.run(main(args.agent_id, config_overlay=overlay))` —
        mutmut changing `config_overlay=overlay` → `config_overlay=None` would lose the dict,
        this test verifies dict really passed to main."""
        bundle = self._patch_run_chain(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("sys.argv", ["python -m agent", "--agent-id", "1"])
        monkeypatch.setenv(AGENT_CONFIG_OVERLAY_ENV, '{"mark": 1}')
        bundle["run"]()
        main_calls = [c for c in bundle["calls"] if c[0] == "main"]
        assert main_calls[0][2] == {"mark": 1}

    def test_overlay_env_var_is_popped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run() pops it, so the agent's own children (shell sessions, watchers,
        which inherit this env) never carry the overlay onward."""
        bundle = self._patch_run_chain(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("sys.argv", ["python -m agent", "--agent-id", "1"])
        monkeypatch.setenv(AGENT_CONFIG_OVERLAY_ENV, '{"k": "v"}')
        bundle["run"]()
        assert AGENT_CONFIG_OVERLAY_ENV not in os.environ
