"""The push watermark survives a daemon restart, and what the agent said while
the bridge was down is pushed from the saved position.

The SSE feed is a live tail: an agent reply published while no subscription
listened is never re-delivered by it. Before the watermark was durable, a
restarted bridge had no position, so the replies of the downtime were either
never pushed (no later snapshot) or pushed together with already-delivered
history (a later snapshot carrying the whole window).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from services.im_bridge.core import IMBridgeCore
from services.im_bridge.tests.slices import im_bridge_config
from services.im_bridge.types import ChatState, IMAdapter, InboundMessage

_KEY = ("telegram", "12345")


@pytest.fixture(autouse=True)
def _own_ava_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The switch state file lives under AVA_HOME: keep it per test."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))


@pytest.fixture
def pool() -> Iterator[ConnectionPool[Any]]:
    """The daemon's shape: one pool on the cluster DB, shared across "restarts"."""
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def _item(item_id: str, text: str, kind: str = "agent_chat") -> dict[str, Any]:
    return {"kind": kind, "item_id": item_id, "payload": text}


class _Gateway:
    def __init__(self, timeline: list[dict[str, Any]] | None = None) -> None:
        self.timeline = timeline or []
        self.timeline_calls = 0
        self.sent: list[tuple[int, str, str | None]] = []

    async def get_timeline(self, agent_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        del agent_id, limit
        self.timeline_calls += 1
        return self.timeline

    async def send_message(
        self, agent_id: int, text: str, *, idempotency_key: str | None = None
    ) -> None:
        self.sent.append((agent_id, text, idempotency_key))

    async def stream_events(self, agent_id: int) -> Any:
        del agent_id
        await asyncio.Event().wait()
        yield None  # pragma: no cover - unreachable


class _Adapter(IMAdapter):
    channel = "telegram"

    def __init__(self) -> None:
        super().__init__(core=None)
        self.sent: list[str] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        del chat_id, buttons, markdown
        self.sent.append(text)


def _core(
    gateway: _Gateway, pool: ConnectionPool[Any] | None = None
) -> tuple[IMBridgeCore, _Adapter]:
    core = IMBridgeCore(im_bridge_config(), gateway, db_pool=pool)  # type: ignore[arg-type]
    adapter = _Adapter()
    core.register(adapter)
    return core, adapter


def _bound_state(core: IMBridgeCore) -> ChatState:
    state = core._get_or_create_state(*_KEY)
    state.current_agent_id = 405
    return state


def test_watermark_survives_restart_so_a_snapshot_pushes_only_what_is_new(
    pool: ConnectionPool,
) -> None:
    async def scenario() -> None:
        core, adapter = _core(_Gateway(), pool)
        state = _bound_state(core)
        await core._push_snapshot(_KEY, state, {"items": [_item("1.0", "old")]})
        assert adapter.sent == ["[Ava #405] old"]

        core2, adapter2 = _core(_Gateway(), pool)  # daemon restart
        await core2.restore_subscriptions()
        state2 = _bound_state(core2)
        await core2._push_snapshot(
            _KEY, state2, {"items": [_item("1.0", "old"), _item("2.0", "new")]}
        )
        assert adapter2.sent == ["[Ava #405] new"]

    asyncio.run(scenario())


def test_restore_pushes_replies_that_arrived_while_the_bridge_was_down(
    pool: ConnectionPool,
) -> None:
    async def scenario() -> None:
        core, _ = _core(_Gateway(), pool)
        state = _bound_state(core)
        core._persist_switch(state)
        await core._push_snapshot(_KEY, state, {"items": [_item("1.0", "seen")]})

        # the bridge is down; the agent says two more things (timeline only)
        gateway2 = _Gateway(
            [_item("1.0", "seen"), _item("2.0", "missed one"), _item("3.0", "missed two")]
        )
        core2, adapter2 = _core(gateway2, pool)
        await core2.restore_subscriptions()
        await asyncio.sleep(0.05)

        assert adapter2.sent == ["[Ava #405] missed one", "[Ava #405] missed two"]
        for task in core2._subscriptions.values():
            task.cancel()

    asyncio.run(scenario())


def test_restore_without_a_saved_watermark_pushes_nothing(pool: ConnectionPool[Any]) -> None:
    """A chat that was never pushed to has no position to resume from: the
    catch-up must not dump the timeline window on it."""

    async def scenario() -> None:
        core, _ = _core(_Gateway(), pool)
        state = _bound_state(core)
        core._persist_switch(state)

        gateway2 = _Gateway([_item("1.0", "history")])
        core2, adapter2 = _core(gateway2, pool)
        await core2.restore_subscriptions()
        await asyncio.sleep(0.05)

        assert adapter2.sent == []
        assert gateway2.timeline_calls == 0
        for task in core2._subscriptions.values():
            task.cancel()

    asyncio.run(scenario())


def test_idempotency_key_reaches_the_gateway() -> None:
    """The adapter's platform-stable key is the gateway delivery key, so a
    message the bridge re-reads after a restart cannot become a second inbound."""

    async def scenario() -> None:
        gateway = _Gateway()
        core, _ = _core(gateway)
        _bound_state(core)
        await core.handle_inbound(
            InboundMessage(
                channel="telegram",
                chat_id="12345",
                text="hello",
                message_id="m1",
                idempotency_key="telegram:m1",
            )
        )
        for task in core._subscriptions.values():
            task.cancel()
        assert gateway.sent == [(405, "hello", "telegram:m1")]

    asyncio.run(scenario())
