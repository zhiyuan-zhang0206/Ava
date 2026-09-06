"""Actual request and executor lifetime must outlive a disconnected awaiter."""

import asyncio
import socket
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from services.agent_ops import daemon
from services.agent_ops import maintenance as activity
from shared import pause_owner
from shared.daemon_health import stop_health_server
from shared.daemon_http import start_daemon_http
from shared.maintenance_state import MaintenanceHold
from tests.agent.test_maintenance import WHEN
from tests.agent.test_maintenance import isolate as isolate


@pytest.fixture(autouse=True)
def isolate_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity, "_requests", set[object]())
    monkeypatch.setattr(activity, "_workers", set[asyncio.Future[Any]]())


async def test_same_kind_requests_remain_counted_and_stop_refuses_new_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(daemon, "_dispatch_sem", asyncio.Semaphore(3))
    monkeypatch.setattr(daemon, "_dispatch", dispatch)
    before = pause_owner.begin_maintenance("ops", WHEN)
    assert before.maintenance is not None
    draining = MaintenanceHold("draining")
    pause_owner.change_maintenance("ops", WHEN, before.maintenance, draining)
    tasks = [
        asyncio.create_task(daemon._ops_route(b'{"kind":"probe","payload":{}}')) for _ in range(2)
    ]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert activity.progress()["requests"] == 2
        finishes[0].set()
        await tasks[0]
        assert activity.progress()["requests"] == 1
        pause_owner.change_maintenance("ops", WHEN, draining, MaintenanceHold("stopping"))
        with pytest.raises(RuntimeError, match="stopping"):
            await daemon._ops_route(b'{"kind":"probe","payload":{}}')
        assert count == 2
    finally:
        for event in finishes:
            event.set()
        await asyncio.gather(*tasks)
    assert activity.progress()["requests"] == 0


async def test_cancelled_same_kind_await_does_not_hide_running_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = [threading.Event(), threading.Event()]
    finish = [threading.Event(), threading.Event()]

    def arm(_kind: str, payload: dict[str, Any]) -> tuple[str, dict[str, object]]:
        index = int(payload["index"])
        entered[index].set()
        if not finish[index].wait(5):
            raise TimeoutError("test worker was not released")
        return "completed", {}

    with ThreadPoolExecutor(max_workers=2) as executor:
        monkeypatch.setattr(daemon, "_op_executor", executor)
        monkeypatch.setattr(daemon, "_dispatch_sync", arm)
        tasks = [
            asyncio.create_task(daemon._run_arm("same", {"index": index})) for index in range(2)
        ]
        try:
            async with asyncio.timeout(2):
                while not all(event.is_set() for event in entered):
                    await asyncio.sleep(0.01)
            assert activity.progress()["workers"] == 2
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            assert activity.progress()["workers"] == 2
        finally:
            for event in finish:
                event.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            async with asyncio.timeout(2):
                while activity.progress()["workers"]:
                    await asyncio.sleep(0.01)


async def test_server_close_after_client_reset_is_not_request_completion() -> None:
    entered, finish, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def route(_body: bytes) -> tuple[int, bytes, str]:
        with activity.admission():
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
        assert activity.progress()["requests"] == 1
    finally:
        peer.close()
        finish.set()
        await asyncio.wait_for(returned.wait(), 2)
        await stop_health_server(server)
    assert activity.progress()["requests"] == 0
