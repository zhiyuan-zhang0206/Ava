"""Pre-send failures authorize a full-message retry only before the first acknowledgement."""

from pathlib import Path

import httpx
import pytest

from services.entrypoints.im_bridge.adapters import telegram, weixin
from services.entrypoints.im_bridge.tests.slices import telegram_config
from services.entrypoints.im_bridge.types import SendNotStartedError


@pytest.mark.parametrize("channel", ["telegram", "weixin"])
@pytest.mark.parametrize("acknowledged", [False, True])
async def test_connect_failure_marker_cannot_escape_after_a_successful_prefix(
    channel: str, acknowledged: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if acknowledged and len(calls) == 1:
            return httpx.Response(200, json={"ok": True, "result": {}, "ret": 0})
        raise httpx.ConnectError("not connected", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if channel == "telegram":
            adapter = telegram.TelegramAdapter(
                None,
                telegram_config(
                    telegram_bot_token="test-token",  # noqa: S106 - non-secret test fixture
                    telegram_owner_id=42,
                ),
                client=client,
            )
            text = "a" * 5000
        else:
            monkeypatch.setattr(
                weixin,
                "load_account",
                lambda: {
                    "account_id": "test",
                    "base_url": "https://ilink.example",
                    "bot_token": "test",
                    "user_id": "owner",
                },
            )
            adapter = weixin.WeixinAdapter(None, client=client)
            adapter._chunk_delay_seconds = 0
            text = "a" * 3000
        if acknowledged:
            with pytest.raises(RuntimeError, match="acknowledged chunks") as error:
                await adapter.send("chat", text)
            assert not isinstance(error.value, SendNotStartedError)
        else:
            with pytest.raises(SendNotStartedError):
                await adapter.send("chat", text)
    assert len(calls) == (2 if acknowledged else 1)
