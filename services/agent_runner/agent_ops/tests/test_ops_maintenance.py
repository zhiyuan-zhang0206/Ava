"""Actual request and executor lifetime must outlive a disconnected awaiter, and a stopped
generation still answers a status probe through the real dispatch."""

import asyncio
import gc
import json
import socket
import struct
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from base.daemon.health import stop_health_server
from base.daemon.http_transport import start_daemon_http
from base.db import Database
from base.deploy.lifecycle import start_serving
from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from base.deploy.state import host_deploy_state
from services.agent_runner.agent_ops import daemon, health
from services.agent_runner.agent_ops import maintenance as activity
from tests.components.agent.test_maintenance import WHEN
from tests.components.agent.test_maintenance import isolate as isolate


async def test_same_kind_requests_remain_counted_and_stop_refuses_new_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    requests: activity.RequestTokens = set()
    workers: activity.WorkerFutures = set()
    finishes = [asyncio.Event(), asyncio.Event()]
    entered = asyncio.Event()
    count = 0

    async def dispatch(*_args: Any, **_kwargs: Any) -> tuple[str, dict[str, object]]:
        nonlocal count
        index = count
        count += 1
        if count == 2:
            entered.set()
        await finishes[index].wait()
        return "completed", {}

    dispatch_sem = asyncio.Semaphore(3)
    monkeypatch.setattr(daemon, "_dispatch", dispatch)
    before = pause_owner.begin_maintenance("ops", WHEN).snapshot
    assert before.maintenance is not None
    draining = MaintenanceHold(MaintenancePhase.DRAINING)
    pause_owner.change_maintenance("ops", WHEN, before.maintenance, draining)
    tasks = [
        asyncio.create_task(
            daemon._ops_route(
                b'{"kind":"config_read","payload":{}}',
                active_ops={},
                dispatch_sem=dispatch_sem,
                workers=workers,
                requests=requests,
                pool=dispatch_pool,
            )
        )
        for _ in range(2)
    ]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert activity.progress(requests=requests, workers=workers)["requests"] == 2
        finishes[0].set()
        await tasks[0]
        assert activity.progress(requests=requests, workers=workers)["requests"] == 1
        pause_owner.change_maintenance(
            "ops", WHEN, draining, MaintenanceHold(MaintenancePhase.STOPPING)
        )
        status, body, _ = await daemon._ops_route(
            b'{"kind":"config_read","payload":{}}',
            active_ops={},
            dispatch_sem=dispatch_sem,
            workers=workers,
            requests=requests,
            pool=dispatch_pool,
        )
        assert status == 200
        assert b'"status": "failed"' in body
        assert b"stopping" in body
        assert count == 2
    finally:
        for event in finishes:
            event.set()
        await asyncio.gather(*tasks)
    assert activity.progress(requests=requests, workers=workers)["requests"] == 0


async def test_cancelled_same_kind_await_does_not_hide_running_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    requests: activity.RequestTokens = set()
    workers: activity.WorkerFutures = set()
    active_ops: daemon.ActiveOps = {}
    entered = [threading.Event(), threading.Event()]
    finish = [threading.Event(), threading.Event()]

    def arm(
        _kind: str, payload: dict[str, Any], *, pool: ConnectionPool
    ) -> tuple[str, dict[str, object]]:
        assert pool is dispatch_pool
        index = int(payload["index"])
        entered[index].set()
        if not finish[index].wait(5):
            raise TimeoutError("test worker was not released")
        return "completed", {}

    with ThreadPoolExecutor(max_workers=2) as executor:
        monkeypatch.setattr(daemon, "_op_executor", executor)
        monkeypatch.setattr(daemon, "_dispatch_sync", arm)
        tasks = [
            asyncio.create_task(
                daemon._run_arm(
                    "same",
                    {"index": index},
                    active_ops=active_ops,
                    workers=workers,
                    pool=dispatch_pool,
                )
            )
            for index in range(2)
        ]
        try:
            async with asyncio.timeout(2):
                while not all(event.is_set() for event in entered):
                    await asyncio.sleep(0.01)
            assert activity.progress(requests=requests, workers=workers)["workers"] == 2
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            assert activity.progress(requests=requests, workers=workers)["workers"] == 2
        finally:
            for event in finish:
                event.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            async with asyncio.timeout(2):
                while activity.progress(requests=requests, workers=workers)["workers"]:
                    await asyncio.sleep(0.01)


async def test_server_close_after_client_reset_is_not_request_completion() -> None:
    requests: activity.RequestTokens = set()
    workers: activity.WorkerFutures = set()
    entered, finish, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def route(_body: bytes) -> tuple[int, bytes, str]:
        with activity.admission("probe", requests=requests):
            entered.set()
            await finish.wait()
            returned.set()
            return 200, b"{}", "application/json"

    server = await start_daemon_http(
        host="127.0.0.1",
        port=0,
        health_response=lambda: (200, b"{}"),
        extra_routes={("POST", "/ops"): route},
    )
    peer = socket.socket()
    peer.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        await loop.sock_connect(peer, ("127.0.0.1", server.sockets[0].getsockname()[1]))
        await loop.sock_sendall(
            peer, b"POST /ops HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}"
        )
        await asyncio.wait_for(entered.wait(), 2)
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        peer.close()
        await asyncio.wait_for(stop_health_server(server), 2)
        assert not returned.is_set()
        assert activity.progress(requests=requests, workers=workers)["requests"] == 1
    finally:
        peer.close()
        finish.set()
        await asyncio.wait_for(returned.wait(), 2)
        await stop_health_server(server)
    assert activity.progress(requests=requests, workers=workers)["requests"] == 0


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database: Database) -> None:
    """A stopped generation: the update hold is published and the host is paused."""
    monkeypatch.setattr(start_serving, "state_path", lambda: tmp_path / "serving.json")
    pause_owner.begin_maintenance("update", WHEN)
    pause_owner.change_maintenance(
        "update", WHEN, MaintenanceHold(), MaintenanceHold(MaintenancePhase.STOPPED)
    )
    host_deploy_state.set_posture(database, "paused")
    start_serving.begin_start()


@pytest.mark.usefixtures("held")
async def test_real_ops_status_reports_the_hold_without_releasing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real dispatch, executor, PostgreSQL posture and journal; no service is launched.
    requests: activity.RequestTokens = set()
    workers: activity.WorkerFutures = set()
    with (
        Database.from_settings().pool(min_size=1, max_size=2) as pool,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        dispatch_pool: ConnectionPool = pool
        monkeypatch.setattr(daemon, "_op_executor", executor)
        dispatch_sem = asyncio.Semaphore(2)

        async def request(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
            status, raw, _ = await daemon._ops_route(
                json.dumps({"kind": kind, "payload": payload}).encode(),
                active_ops={},
                dispatch_sem=dispatch_sem,
                workers=workers,
                requests=requests,
                pool=dispatch_pool,
            )
            assert status == 200
            return json.loads(raw)

        status = await request("status_probe", {})
        assert status["status"] == "completed"
        assert status["result"]["paused"] is True
        assert admission.held()


async def test_active_ops_share_health_and_cleanup_within_one_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    workers: activity.WorkerFutures = set()
    active_ops: daemon.ActiveOps = {}
    other_daemon: daemon.ActiveOps = {}
    entered = [threading.Event(), threading.Event()]
    finish = [threading.Event(), threading.Event()]

    def arm(
        _kind: str, payload: dict[str, Any], *, pool: ConnectionPool
    ) -> tuple[str, dict[str, object]]:
        assert pool is dispatch_pool
        index = int(payload["index"])
        entered[index].set()
        if not finish[index].wait(5):
            raise TimeoutError("test worker was not released")
        if index == 1:
            raise RuntimeError("operation failed")
        return "completed", {}

    with ThreadPoolExecutor(max_workers=2) as executor:
        monkeypatch.setattr(daemon, "_op_executor", executor)
        monkeypatch.setattr(daemon, "_dispatch_sync", arm)
        tasks = [
            asyncio.create_task(
                daemon._run_arm(
                    kind,
                    {"index": index},
                    active_ops=active_ops,
                    workers=workers,
                    pool=dispatch_pool,
                )
            )
            for index, kind in enumerate(("config_read", "inventory_read"))
        ]
        try:
            async with asyncio.timeout(2):
                while not all(event.is_set() for event in entered):
                    await asyncio.sleep(0.01)
            assert health.ops_components(active_ops)[1]["progress"] == "2 active"
            assert health.saturation(active_ops, 2) == 1.0
            assert health.ops_components(other_daemon)[1]["progress"] == "0 active"
            finish[0].set()
            await tasks[0]
            assert set(active_ops) == {"inventory_read"}
            assert health.saturation(active_ops, 2) == 0.5
            finish[1].set()
            with pytest.raises(RuntimeError, match="operation failed"):
                await tasks[1]
            assert active_ops == {}
            assert health.ops_components(active_ops)[1]["progress"] == "0 active"
            assert health.saturation(active_ops, 2) == 0.0
        finally:
            for event in finish:
                event.set()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_worker_future_is_retained_until_completion_and_isolated_per_daemon() -> None:
    requests: activity.RequestTokens = set()
    workers: activity.WorkerFutures = set()
    other_workers: activity.WorkerFutures = set()
    future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
    reference = weakref.ref(future)
    activity.track_worker(future, workers=workers)
    del future
    gc.collect()
    retained = reference()
    assert retained is not None
    assert activity.progress(requests=requests, workers=workers)["workers"] == 1
    assert activity.progress(requests=requests, workers=other_workers)["workers"] == 0
    retained.set_result(None)
    await asyncio.sleep(0)
    assert activity.progress(requests=requests, workers=workers)["workers"] == 0
