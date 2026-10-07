"""Weixin logical sends must never share a heuristic chat/chunk identity.

A direct send() call is a new intent even immediately after an unknown outcome.
Only a future durable outbound record can identify a retry of an old intent.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from services.entrypoints.im_bridge.adapters import weixin
from services.entrypoints.im_bridge.adapters.weixin import WeixinAdapter, _outbound_message

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
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    monkeypatch.setattr(weixin, "load_account", lambda: dict(_ACCOUNT))
    a = WeixinAdapter(None)  # type: ignore[arg-type]
    a._client = http  # type: ignore[attr-defined]
    a._owns_client = False
    return a


def _client_ids(http: _FakeHTTP) -> list[str]:
    return [
        call[1]["msg"]["client_id"] for call in http.post_calls if call[0].endswith("sendmessage")
    ]


def test_new_message_after_unknown_outcome_gets_fresh_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """An immediate different send cannot reuse the previous failed identity."""
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
        await a.send("peer-1", "another message")  # a deliberate new intent

    asyncio.run(scenario())
    ids = _client_ids(http)
    assert len(ids) == 2
    assert ids[0] != ids[1], "different intents must never share a client_id"


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
    assert ids[0] != ids[1]
    assert ids[1] != ids[2], "a new message must get a fresh client_id"


def test_outbound_message_uses_given_client_id() -> None:
    msg = _outbound_message("peer-1", "hi", None, "idem-42")
    assert msg["msg"]["client_id"] == "idem-42"
