"""Feishu websocket work belongs to the daemon's service task scope."""

import asyncio
import importlib
import secrets

import pytest

from services.entrypoints.im_bridge.adapters.feishu import FeishuAdapter
from services.entrypoints.im_bridge.adapters.tests.test_feishu_adapter import (
    FakeCore,
    FakeWsClient,
    PatchingAdapter,
    make_event,
)
from services.entrypoints.im_bridge.tests.slices import feishu_config
from services.entrypoints.im_bridge.types import InboundMessage


@pytest.mark.parametrize("fault", [RuntimeError("WS handler bug"), KeyError("missing input")])
async def test_ws_callback_fault_fails_the_service_group_without_marking_seen(
    fault: Exception,
) -> None:
    class FaultingCore(FakeCore):
        async def handle_inbound(self, message: InboundMessage) -> None:
            raise fault

    adapter = FeishuAdapter(FaultingCore(), feishu_config())
    with pytest.raises(ExceptionGroup) as caught:
        async with asyncio.TaskGroup() as tasks:
            adapter._main_loop = asyncio.get_running_loop()
            adapter._tasks = tasks
            adapter._accepting_events = True
            try:
                adapter._on_im_message(make_event())
                await asyncio.wait_for(asyncio.Event().wait(), timeout=2)
            finally:
                adapter.begin_shutdown()
    assert caught.value.exceptions == (fault,)
    assert not adapter._seen_messages


async def test_ws_closing_rejects_queued_events_and_ends_owner_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    adapter = FeishuAdapter(FakeCore(), feishu_config())
    handler = AsyncMock()
    monkeypatch.setattr(adapter, "_handle_event", handler)

    async def lifecycle() -> None:
        async with asyncio.TaskGroup() as tasks:
            adapter._main_loop = asyncio.get_running_loop()
            adapter._tasks = tasks
            adapter._ws_exit = adapter._main_loop.create_future()
            adapter._accepting_events = True
            tasks.create_task(adapter._observe_ws_exit(adapter._ws_exit))
            adapter._on_im_message(make_event())
            adapter.begin_shutdown()
            adapter._on_im_message(make_event())
            await asyncio.sleep(0)
        handler.assert_not_called()
        assert adapter._ws_exit.cancelled()

    await asyncio.wait_for(lifecycle(), timeout=2)


async def test_ws_thread_failure_reaches_the_same_service_owner() -> None:
    importlib.import_module("lark_oapi.ws.client")

    fault = RuntimeError("websocket implementation bug")

    class FaultingWsClient(FakeWsClient):
        def start(self) -> None:
            raise fault

    adapter = PatchingAdapter(
        FakeCore(),
        feishu_config(
            feishu_app_id="cli_x",
            feishu_app_secret=secrets.token_urlsafe(16),
            feishu_poll_interval_seconds=0,
        ),
        FaultingWsClient(),
    )
    with pytest.raises(ExceptionGroup) as caught:
        async with asyncio.TaskGroup() as tasks:
            try:
                await adapter.start(tasks)
                await asyncio.wait_for(asyncio.Event().wait(), timeout=5)
            finally:
                adapter.begin_shutdown()
    assert caught.value.exceptions == (fault,)
    assert adapter._ws_thread is not None
    adapter._ws_thread.join(timeout=1)
    assert not adapter._ws_thread.is_alive()


async def test_active_task_creation_fault_reaches_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FeishuAdapter(FakeCore(), feishu_config())
    fault = RuntimeError("active service task creation failed")
    with pytest.raises(ExceptionGroup) as caught:
        async with asyncio.TaskGroup() as tasks:
            adapter._main_loop = asyncio.get_running_loop()
            adapter._tasks = tasks
            adapter._ws_exit = adapter._main_loop.create_future()
            adapter._accepting_events = True
            tasks.create_task(adapter._observe_ws_exit(adapter._ws_exit))

            def fail_create(*args: object, **kwargs: object) -> None:
                raise fault

            monkeypatch.setattr(tasks, "create_task", fail_create)
            try:
                adapter._on_im_message(make_event())
                await asyncio.wait_for(asyncio.Event().wait(), timeout=2)
            finally:
                adapter.begin_shutdown()
    assert caught.value.exceptions == (fault,)
    assert not adapter.core.received
