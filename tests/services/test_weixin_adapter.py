"""`services.im_bridge.adapters.weixin` — the first regression net for the
largest adapter (audit round 2, P2 noted it had zero test files).

Covered here: the outbound idempotency key. iLink dedups sendmessage by
``client_id``; a timed-out send (server processed, response lost) is
retried by ``push_watchdog.send_with_retry`` with a fresh ``send()`` call —
the retry must reuse the failed attempt's client_id or the user sees the
message twice (audit round 2, P1).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from services.im_bridge.adapters import weixin
from services.im_bridge.adapters.weixin import WeixinAdapter, _outbound_message
from shared.config import settings

_ACCOUNT = {
    "account_id": "acct-1",
    "base_url": "https://ilink.example",
    "bot_token": "tok",
    "user_id": "owner-1",
}


class _FakeHTTP:
    """Scripted httpx stand-in: a queue of outcomes (exception or response)."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    async def post(
        self, url: str, json: dict[str, Any] | None = None, headers: Any = None, timeout: Any = None
    ) -> httpx.Response:
        del headers, timeout
        self.post_calls.append((url, json or {}))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(200, json=outcome)


def _adapter(http: _FakeHTTP, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> WeixinAdapter:
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    monkeypatch.setattr(weixin, "load_account", lambda: dict(_ACCOUNT))
    a = WeixinAdapter(None)  # type: ignore[arg-type]
    a._client = http  # type: ignore[attr-defined]
    a._owns_client = False
    return a


def _client_ids(http: _FakeHTTP) -> list[str]:
    return [
        call[1]["msg"]["client_id"] for call in http.post_calls if call[0].endswith("sendmessage")
    ]


def test_retry_reuses_client_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Regression (audit round 2, P1): the retry of a timed-out send reuses
    the failed attempt's client_id — iLink dedups by it, so without the
    reuse the user would see the message twice."""
    http = _FakeHTTP(
        [
            httpx.TimeoutException("timed out"),  # first attempt: response lost
            {"ret": 0, "errcode": 0},  # retry: delivered
        ]
    )
    a = _adapter(http, monkeypatch, tmp_path)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="timed out"):
            await a.send("peer-1", "hello")
        await a.send("peer-1", "hello")  # the immediate retry

    asyncio.run(scenario())
    ids = _client_ids(http)
    assert len(ids) == 2
    assert ids[0] == ids[1], "the retry must reuse the failed attempt's client_id"


def test_success_clears_pending_and_new_send_gets_fresh_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A stale pending id must never be reused for a later, different message
    (iLink would dedup-drop it)."""
    http = _FakeHTTP(
        [
            httpx.TimeoutException("timed out"),
            {"ret": 0, "errcode": 0},
            {"ret": 0, "errcode": 0},
        ]
    )
    a = _adapter(http, monkeypatch, tmp_path)

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            await a.send("peer-1", "first")
        await a.send("peer-1", "first")  # retry succeeds, pending cleared
        await a.send("peer-1", "second")  # a different message

    asyncio.run(scenario())
    ids = _client_ids(http)
    assert ids[0] == ids[1]
    assert ids[1] != ids[2], "a new message must get a fresh client_id"


def test_outbound_message_uses_given_client_id() -> None:
    msg = _outbound_message("peer-1", "hi", None, "idem-42")
    assert msg["msg"]["client_id"] == "idem-42"
