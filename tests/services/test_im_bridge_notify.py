"""`IMBridgeCore.notify_user` + the daemon's `POST /send` RPC (event-system W10a).

The ops-alerts pipeline's IM fan-out: the gateway POSTs one message to the
im_bridge daemon's health-port `/send` route; the core fans it out to every
loaded adapter's `send_to_owner`. This module locks the fan-out contract —
all adapters receive the text, a channel that cannot resolve an owner chat is
skipped, one failing channel does not stop the others, and the `/send` route
handler validates the body and returns per-channel results.
"""

from __future__ import annotations

import json
from typing import Any

from services.im_bridge.core import IMBridgeCore
from services.im_bridge.types import IMAdapter


class _RecordingAdapter(IMAdapter):
    """IMAdapter stand-in recording send_to_owner calls."""

    _next_channel = 0

    def __init__(self, *, error: Exception | None = None, skipped: bool = False) -> None:
        super().__init__(core=None)  # type: ignore[arg-type] — duck-typed, never used
        _RecordingAdapter._next_channel += 1
        self.channel = f"rec{_RecordingAdapter._next_channel}"
        self.error = error
        self.skipped = skipped
        self.sent: list[str] = []

    async def start(self) -> None:  # pragma: no cover - abstract contract
        return None

    async def stop(self) -> None:  # pragma: no cover - abstract contract
        return None

    async def send(  # pragma: no cover - abstract contract
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        del chat_id, text, buttons, markdown

    async def send_to_owner(self, text: str, *, markdown: bool = False) -> None:
        del markdown
        if self.skipped:
            raise NotImplementedError("no owner chat")
        if self.error is not None:
            raise self.error
        self.sent.append(text)


def test_notify_user_fans_out_to_all_adapters() -> None:
    """Every loaded adapter gets the text; results report per-channel ok."""
    core = IMBridgeCore()
    a1, a2 = _RecordingAdapter(), _RecordingAdapter()
    core.register(a1)
    core.register(a2)

    async def run() -> dict[str, str]:
        return await core.notify_user(
            "🚨 Alert test-rule (P1)"  # emoji-ok: mirrors production IM text
        )

    results = asyncio_run(run())
    assert a1.sent == ["🚨 Alert test-rule (P1)"]  # emoji-ok: mirrors production IM text
    assert a2.sent == ["🚨 Alert test-rule (P1)"]  # emoji-ok: mirrors production IM text
    assert set(results.values()) == {"ok"}


def test_notify_user_skips_and_isolates_failures() -> None:
    """A channel without an owner chat is skipped; a failing channel does not
    stop the others from receiving the message."""
    core = IMBridgeCore()
    skipped, broken, ok = (
        _RecordingAdapter(skipped=True),
        _RecordingAdapter(error=RuntimeError("platform down")),
        _RecordingAdapter(),
    )
    core.register(skipped)
    core.register(broken)
    core.register(ok)

    async def run() -> dict[str, str]:
        return await core.notify_user("hi")

    results = asyncio_run(run())
    assert ok.sent == ["hi"]
    assert skipped.sent == []
    assert broken.sent == []
    assert results[skipped.channel] == "skipped"
    assert results[broken.channel].startswith("error:")


def test_notify_user_empty_core() -> None:
    """No adapters loaded -> empty results, no error (daemon serves nothing)."""
    core = IMBridgeCore()

    async def run() -> dict[str, str]:
        return await core.notify_user("hi")

    assert asyncio_run(run()) == {}


# -- daemon /send route handler ----------------------------------------------


def _make_handler() -> Any:
    """Import the daemon's route factory with a recording core."""
    from services.im_bridge import daemon

    class _Core:
        def __init__(self) -> None:
            self.received: list[str] = []

        async def notify_user(self, text: str) -> dict[str, str]:
            self.received.append(text)
            return {"telegram": "ok"}

    core = _Core()

    # the factory is async (returns the handler) — resolve it
    async def build() -> Any:
        return await daemon._handle_send(core)  # type: ignore[attr-defined]

    return asyncio_run(build()), core


def test_send_route_validates_and_forwards() -> None:
    handler, core = _make_handler()

    async def run() -> tuple[int, bytes, str]:
        return await handler(b'{"text": "alert line"}')

    status, body, ctype = asyncio_run(run())
    assert status == 200
    assert ctype == "application/json"
    assert json.loads(body) == {"results": {"telegram": "ok"}}
    assert core.received == ["alert line"]


def test_send_route_rejects_bad_bodies() -> None:
    handler, _core = _make_handler()

    async def run(body: bytes) -> tuple[int, bytes, str]:
        return await handler(body)

    status, _body, _ = asyncio_run(run(b"not json"))
    assert status == 400
    status, _body, _ = asyncio_run(run(b'{"text": ""}'))
    assert status == 400
    status, _body, _ = asyncio_run(run(b"{}"))
    assert status == 400


def test_send_route_502_when_no_channel_delivered() -> None:
    """All adapters failed (or none loaded) -> 502, so the caller (shared/alerts.py)
    keeps notified_at NULL instead of stamping a message that never landed."""
    from services.im_bridge import daemon

    class _Core:
        async def notify_user(self, text: str) -> dict[str, str]:
            del text
            return {"telegram": "error: RuntimeError", "feishu": "skipped"}

    async def build() -> Any:
        return await daemon._handle_send(_Core())  # type: ignore[attr-defined]

    handler = asyncio_run(build())
    status, body, ctype = asyncio_run(handler(b'{"text": "alert line"}'))
    assert status == 502
    assert ctype == "application/json"
    assert json.loads(body) == {"results": {"telegram": "error: RuntimeError", "feishu": "skipped"}}


def test_send_route_502_when_no_adapter_loaded() -> None:
    """Empty results (no adapters loaded) -> 502, not a fake 200."""
    from services.im_bridge import daemon

    class _Core:
        async def notify_user(self, text: str) -> dict[str, str]:
            del text
            return {}

    async def build() -> Any:
        return await daemon._handle_send(_Core())  # type: ignore[attr-defined]

    handler = asyncio_run(build())
    status, body, _ = asyncio_run(handler(b'{"text": "alert line"}'))
    assert status == 502
    assert json.loads(body) == {"results": {}}


def test_send_route_200_when_at_least_one_channel_delivered() -> None:
    """One ok channel among failures is still a delivery -> 200 (the caller
    stamps notified_at; the failed channels are reported for diagnostics)."""
    from services.im_bridge import daemon

    class _Core:
        async def notify_user(self, text: str) -> dict[str, str]:
            del text
            return {"telegram": "ok", "feishu": "error: RuntimeError"}

    async def build() -> Any:
        return await daemon._handle_send(_Core())  # type: ignore[attr-defined]

    handler = asyncio_run(build())
    status, body, _ = asyncio_run(handler(b'{"text": "alert line"}'))
    assert status == 200
    assert json.loads(body) == {"results": {"telegram": "ok", "feishu": "error: RuntimeError"}}


def asyncio_run(awaitable: Any) -> Any:
    """Run one awaitable on a fresh loop (the handler/core are loop-agnostic)."""

    import asyncio

    return asyncio.run(awaitable)
