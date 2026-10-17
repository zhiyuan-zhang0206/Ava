import asyncio
import threading
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.tests.configuration import snapshot_process_config
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host
from base.paths import meiva_home
from gateway import app as gateway_app
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from gateway.http.config import gateway_config_from_boot
from tests.fixtures.gateway_config import gateway_test_process


@pytest.fixture
def flusher_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> asyncio.Queue[asyncio.Task[Any]]:
    started: asyncio.Queue[asyncio.Task[Any]] = asyncio.Queue()

    async def flush(*_args: object) -> None:
        task = asyncio.current_task()
        assert task is not None
        started.put_nowait(task)
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)

    monkeypatch.setattr(gateway_app.latency, "latency_flusher", flush)
    monkeypatch.setattr(gateway_app.rejection_log, "auth401_flusher", flush)

    def register_cron(*, enabled_reader: Callable[[], bool], host: Host) -> None:
        return None

    monkeypatch.setattr(gateway_app, "register_os_cron", register_cron)
    return started


async def test_overlapping_lifespans_join_their_original_flushers(
    db_conn: psycopg.Connection[Any],
    flusher_tasks: asyncio.Queue[asyncio.Task[Any]],
    *,
    machine_identity: MachineIdentity,
) -> None:
    del db_conn
    app = FastAPI()
    with gateway_test_process(machine=machine_identity) as process:
        app.state.gateway_config = gateway_config_from_boot(process.config)
        async with asyncio.timeout(5), AsyncExitStack() as cleanup:
            async with gateway_app.lifespan(app, process=process, process_ownership="borrowed"):
                pipeline = process.event_pipeline()
                assert app.state.gateway_process is process
                assert app.state.machine is machine_identity
                assert app.state.process_image is process.image
                original = [await flusher_tasks.get(), await flusher_tasks.get()]
                cleanup.callback(app.state.runtime_metrics.stop)
                cleanup.push_async_callback(app.state.grafana_client.aclose)
                cleanup.push_async_callback(app.state.insights_client.aclose)
                cleanup.callback(app.state.db_pool.close)
                cleanup.callback(app.state.control_db_pool.close)
                async with gateway_app.lifespan(app, process=process, process_ownership="borrowed"):
                    assert app.state.gateway_process is process
                    assert process.event_pipeline() is pipeline
                    assert app.state.process_image is process.image
                    replacement = [await flusher_tasks.get(), await flusher_tasks.get()]
                assert not pipeline.stopped
                assert all(task.done() for task in replacement)
                assert all(not task.done() for task in original)
            assert all(task.done() for task in original)
        assert not pipeline.stopped
    assert pipeline.stopped


async def test_client_close_failure_still_joins_flushers_before_pool_close(
    db_conn: psycopg.Connection[Any],
    flusher_tasks: asyncio.Queue[asyncio.Task[Any]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    del db_conn
    app = FastAPI()
    failure = RuntimeError("proxy client close failed")
    close_checks: list[bool] = []
    tasks: list[asyncio.Task[Any]] = []
    pools: list[Any] = []

    with gateway_test_process(machine=machine_identity) as process:
        app.state.gateway_config = gateway_config_from_boot(process.config)
        async with asyncio.timeout(5):
            with pytest.raises(RuntimeError) as raised:
                async with gateway_app.lifespan(app, process=process, process_ownership="borrowed"):
                    tasks.extend([await flusher_tasks.get(), await flusher_tasks.get()])
                    pools.extend([app.state.db_pool, app.state.control_db_pool])
                    original_close: Callable[[], Any] = app.state.insights_client.aclose

                    async def failed_close() -> None:
                        await original_close()
                        raise failure

                    monkeypatch.setattr(app.state.insights_client, "aclose", failed_close)
                    for pool in pools:
                        original_pool_close = pool.close

                        def checked_close(close: Callable[[], None] = original_pool_close) -> None:
                            close_checks.append(all(task.done() for task in tasks))
                            close()

                        monkeypatch.setattr(pool, "close", checked_close)

    assert raised.value is failure
    assert close_checks == [True, True]
    assert all(task.done() for task in tasks)
    assert all(pool.closed for pool in pools)
    assert app.state.grafana_client.is_closed


async def test_process_ownership_borrowed_vs_owned_controls_client_close_count(
    db_conn: psycopg.Connection[Any],
    flusher_tasks: asyncio.Queue[asyncio.Task[Any]],
    *,
    machine_identity: MachineIdentity,
) -> None:
    del db_conn
    with gateway_test_process(machine=machine_identity) as borrowed_process:
        borrowed_app = FastAPI()
        borrowed_app.state.gateway_config = gateway_config_from_boot(borrowed_process.config)
        async with (
            asyncio.timeout(5),
            gateway_app.lifespan(
                borrowed_app, process=borrowed_process, process_ownership="borrowed"
            ),
        ):
            _ = [await flusher_tasks.get(), await flusher_tasks.get()]
            assert borrowed_process.clients.exports_deferred() is not None
        assert borrowed_process.clients.exports_deferred() is not None
    assert borrowed_process.clients.exports_deferred() is None

    owned_process = GatewayProcess(
        home=meiva_home(),
        config=snapshot_process_config(),
        image=LOADED_IMAGE,
        machine=machine_identity,
        host=fake_host(),
    )
    owned_app = FastAPI()
    owned_app.state.gateway_config = gateway_config_from_boot(owned_process.config)
    async with (
        asyncio.timeout(5),
        gateway_app.lifespan(owned_app, process=owned_process, process_ownership="owned"),
    ):
        _ = [await flusher_tasks.get(), await flusher_tasks.get()]
        assert owned_process.clients.exports_deferred() is not None
    assert owned_process.clients.exports_deferred() is None


async def test_owned_process_startup_failure_closes_clients_and_preserves_primary(
    *,
    machine_identity: MachineIdentity,
) -> None:
    from base.agents.context.clients import ClientSet

    primary = RuntimeError("event pipeline failed during owned startup")
    cleanup_err = RuntimeError("owned client close failed")
    close_calls = 0

    class _BrokenPipeline:
        def exports_deferred(self) -> bool:
            return False

        def finalize_exports(self) -> None:
            return None

        def stop(self, *, timeout: float = 5.0) -> Any:
            del timeout
            nonlocal close_calls
            close_calls += 1
            raise cleanup_err

    class _FailingOwnedProcess(GatewayProcess):
        def __init__(self) -> None:
            super().__init__(
                home=meiva_home(),
                config=snapshot_process_config(),
                image=LOADED_IMAGE,
                machine=machine_identity,
                host=fake_host(),
            )
            self.clients = ClientSet(
                database=self.database,
                pipeline_factory=_BrokenPipeline,
            )

        def event_pipeline(self) -> Any:
            _ = self.clients.event_pipeline()
            raise primary

    owned_process = _FailingOwnedProcess()
    app = FastAPI()
    app.state.gateway_config = gateway_config_from_boot(owned_process.config)
    with pytest.raises(RuntimeError, match="event pipeline failed during owned startup") as caught:
        async with gateway_app.lifespan(app, process=owned_process, process_ownership="owned"):
            pass

    assert caught.value is primary
    assert close_calls == 1
    assert any("owned client close failed" in note for note in primary.__notes__)


def test_invalid_process_ownership_rejected(*, machine_identity: MachineIdentity) -> None:
    boot = snapshot_process_config()
    process = GatewayProcess(
        home=meiva_home(),
        config=boot,
        image=LOADED_IMAGE,
        machine=machine_identity,
        host=fake_host(),
    )
    bad_ownership: Any = "invalid"
    with pytest.raises(ValueError, match="gateway process ownership must be borrowed or owned"):
        gateway_app.create_app(
            gateway_config_from_boot(boot),
            process=process,
            process_ownership=bad_ownership,
        )


class _RecordingPool:
    def __init__(
        self,
        events: list[tuple[str, bool]],
        name: str,
        ready: Callable[[], bool],
        failure: BaseException | None = None,
    ) -> None:
        self.events = events
        self.name = name
        self.ready = ready
        self.failure = failure
        self.closed = False
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.closed:
            return
        self.closed = True
        self.events.append((self.name, self.ready()))
        if self.failure is not None:
            raise self.failure


class _RecordingPoolsDatabase:
    def __init__(self, process: "_RecordingPoolsProcess") -> None:
        self._process = process
        self._database = GatewayProcess.database(process)

    def pool(self, **kwargs: Any) -> Any:
        if kwargs.get("max_size") == 8:
            return self._process.data_pool
        if kwargs.get("min_size") == 1 and kwargs.get("max_size") == 2:
            if self._process.control_failure is not None:
                raise self._process.control_failure
            return self._process.control_pool
        return self._database.pool(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._database, name)


class _RecordingPoolsProcess(GatewayProcess):
    def __init__(
        self,
        *,
        machine: MachineIdentity,
        events: list[tuple[str, bool]],
        ready: Callable[[], bool],
        control_failure: BaseException | None = None,
        data_close_failure: BaseException | None = None,
    ) -> None:
        self.control_failure = control_failure
        self.data_pool = _RecordingPool(events, "data", ready, data_close_failure)
        self.control_pool = _RecordingPool(events, "control", ready)
        super().__init__(
            home=meiva_home(),
            config=snapshot_process_config(),
            image=LOADED_IMAGE,
            machine=machine,
            host=fake_host(),
        )

    def database(self) -> Any:
        return _RecordingPoolsDatabase(self)


async def test_control_pool_construction_failure_closes_the_data_pool_once(
    db_conn: psycopg.Connection[Any],
    flusher_tasks: asyncio.Queue[asyncio.Task[Any]],
    *,
    machine_identity: MachineIdentity,
) -> None:
    del db_conn, flusher_tasks
    events: list[tuple[str, bool]] = []
    failure = RuntimeError("control pool construction failed")
    process = _RecordingPoolsProcess(
        machine=machine_identity, events=events, ready=lambda: False, control_failure=failure
    )
    app = FastAPI()
    app.state.gateway_config = gateway_config_from_boot(process.config)

    with process.lifetime():
        async with asyncio.timeout(5):
            with pytest.raises(RuntimeError) as raised:
                async with gateway_app.lifespan(app, process=process, process_ownership="borrowed"):
                    pytest.fail("lifespan served without its control pool")

    assert raised.value is failure
    assert events == [("data", False)]
    assert process.data_pool.close_calls == 1


class _LateMachineFailureProcess(GatewayProcess):
    def __init__(
        self, *, machine: MachineIdentity, failure: BaseException, fail_at_read: int
    ) -> None:
        self._failure = failure
        self._fail_at_read = fail_at_read
        self._machine_reads = 0
        self._real_machine = machine
        super().__init__(
            home=meiva_home(),
            config=snapshot_process_config(),
            image=LOADED_IMAGE,
            machine=machine,
            host=fake_host(),
        )

    @property
    def machine(self) -> MachineIdentity:
        if threading.current_thread() is threading.main_thread():
            self._machine_reads += 1
            if self._machine_reads >= self._fail_at_read:
                raise self._failure
        return self._real_machine

    @machine.setter
    def machine(self, _value: MachineIdentity) -> None:
        return None


async def test_failure_after_monitor_closes_started_resources(
    db_conn: psycopg.Connection[Any],
    flusher_tasks: asyncio.Queue[asyncio.Task[Any]],
    *,
    machine_identity: MachineIdentity,
) -> None:
    del db_conn, flusher_tasks
    failure = RuntimeError("upload recovery construction failed")
    process = _LateMachineFailureProcess(machine=machine_identity, failure=failure, fail_at_read=5)
    app = FastAPI()
    app.state.gateway_config = gateway_config_from_boot(process.config)

    with process.lifetime():
        async with asyncio.timeout(5):
            with pytest.raises(RuntimeError) as raised:
                async with gateway_app.lifespan(app, process=process, process_ownership="borrowed"):
                    pytest.fail("lifespan served after the post-monitor failure")

    assert raised.value is failure
    assert app.state.runtime_metrics._stopped
    assert app.state.db_pool.closed
    assert app.state.control_db_pool.closed
    assert app.state.grafana_client.is_closed
    assert app.state.insights_client.is_closed


async def test_failing_pool_release_still_closes_the_remaining_resources(
    db_conn: psycopg.Connection[Any],
    flusher_tasks: asyncio.Queue[asyncio.Task[Any]],
    *,
    machine_identity: MachineIdentity,
) -> None:
    del db_conn
    events: list[tuple[str, bool]] = []
    tasks: list[asyncio.Task[Any]] = []
    failure = RuntimeError("data-plane pool close failed")
    process = _RecordingPoolsProcess(
        machine=machine_identity,
        events=events,
        ready=lambda: all(task.done() for task in tasks),
        data_close_failure=failure,
    )
    app = FastAPI()
    app.state.gateway_config = gateway_config_from_boot(process.config)

    with process.lifetime():
        async with asyncio.timeout(5):
            with pytest.raises(RuntimeError) as raised:
                async with gateway_app.lifespan(app, process=process, process_ownership="borrowed"):
                    tasks.extend([await flusher_tasks.get(), await flusher_tasks.get()])

    assert raised.value is failure
    assert events == [("control", True), ("data", True)]
    assert app.state.runtime_metrics._stopped
    assert app.state.grafana_client.is_closed
    assert app.state.insights_client.is_closed


async def test_cancelled_lifespan_joins_flushers_before_pool_close(
    db_conn: psycopg.Connection[Any],
    flusher_tasks: asyncio.Queue[asyncio.Task[Any]],
    *,
    machine_identity: MachineIdentity,
) -> None:
    del db_conn
    events: list[tuple[str, bool]] = []
    tasks: list[asyncio.Task[Any]] = []
    process = _RecordingPoolsProcess(
        machine=machine_identity,
        events=events,
        ready=lambda: all(task.done() for task in tasks),
    )
    app = FastAPI()
    app.state.gateway_config = gateway_config_from_boot(process.config)
    entered = asyncio.Event()

    async def serve() -> None:
        async with gateway_app.lifespan(app, process=process, process_ownership="borrowed"):
            tasks.extend([await flusher_tasks.get(), await flusher_tasks.get()])
            entered.set()
            await asyncio.Event().wait()

    with process.lifetime():
        async with asyncio.timeout(5):
            running = asyncio.create_task(serve())
            await entered.wait()
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running

    assert events == [("control", True), ("data", True)]
    assert all(task.done() for task in tasks)
