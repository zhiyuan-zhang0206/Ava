"""Unit tests for services/browser.session — the CDP gateway-session injector.

The CDP and HTTP surfaces are faked; nothing here dials a real browser. Also
covers the browser-mcp daemon's injection wiring (best-effort guards, refresh
loop), which shares this module's file for proximity.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from services.browser import mcp_daemon
from services.browser import session as sess
from shared.cluster_auth import verify_session

SECRET = "test-cluster-secret"  # noqa: S105
GATEWAY = "http://gateway.example:8000"


class FakeWS:
    """A websocket that answers queued CDP responses and records requests."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.sent: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeWS:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        return json.dumps(self._responses.pop(0))


def _install_fake_cdp(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeWS]:
    """Point session's HTTP + websocket surfaces at fakes; return them by role.

    The fake websocket factory answers Target.createTarget / Target.closeTarget
    on the browser connection and Network.setCookie on the page connection.
    """
    http_calls: list[str] = []

    async def fake_http_json(url: str) -> dict[str, Any]:
        http_calls.append(url)
        return {"webSocketDebuggerUrl": "ws://browser"}

    browser_ws = FakeWS([{"id": 1, "result": {"targetId": "t-1"}}, {"id": 2, "result": {}}])
    page_ws = FakeWS([{"id": 3, "result": {"success": True}}])

    def fake_connect(url: str, **kwargs: Any) -> FakeWS:
        return browser_ws if "browser" in url else page_ws

    monkeypatch.setattr(sess, "_http_json", fake_http_json)
    monkeypatch.setattr(sess.websockets, "connect", fake_connect)
    return {"browser": browser_ws, "page": page_ws}


def test_gateway_session_cookie_mints_valid_token() -> None:
    name, value, expiry = sess.gateway_session_cookie(SECRET)
    assert name == "ava_session"
    assert verify_session(value, SECRET)
    # expiry mirrors the token's embedded expiry and stays in the 7-day window
    import time

    assert time.time() < expiry <= time.time() + 8 * 24 * 3600
    # a different secret must not verify
    assert not verify_session(value, "other-secret")


async def test_inject_sets_cookie_with_login_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _install_fake_cdp(monkeypatch)
    await sess.inject_session_cookie(9222, GATEWAY, SECRET)

    # create a throwaway tab, set the cookie on it, close it
    browser_calls = [c["method"] for c in ws["browser"].sent]
    assert browser_calls == ["Target.createTarget", "Target.closeTarget"]
    assert ws["browser"].sent[0]["params"] == {"url": "about:blank"}
    assert ws["browser"].sent[1]["params"] == {"targetId": "t-1"}

    set_cookie = ws["page"].sent[0]
    assert set_cookie["method"] == "Network.setCookie"
    params = set_cookie["params"]
    assert params["name"] == "ava_session"
    assert params["url"] == GATEWAY + "/"  # host-only, trailing slash normalized
    assert params["path"] == "/"
    assert params["httpOnly"] is True
    assert params["sameSite"] == "Lax"
    assert verify_session(params["value"], SECRET)
    assert params["expires"] == sess.gateway_session_cookie(SECRET)[2]


async def test_inject_raises_when_browser_rejects_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_cdp(monkeypatch)
    # make the page connection answer "rejected"
    import services.browser.session as s2

    class RejectingPageWS(FakeWS):
        def __init__(self) -> None:
            super().__init__([{"id": 3, "result": {"success": False}}])

    def fake_connect(url: str, **kwargs: Any) -> FakeWS:
        return (
            RejectingPageWS()
            if "browser" not in url
            else FakeWS([{"id": 1, "result": {"targetId": "t-1"}}, {"id": 2, "result": {}}])
        )

    monkeypatch.setattr(s2.websockets, "connect", fake_connect)
    with pytest.raises(RuntimeError, match="rejected"):
        await sess.inject_session_cookie(9222, GATEWAY, SECRET)


async def test_cdp_call_raises_on_protocol_error() -> None:
    ws = FakeWS([{"id": 1, "error": {"code": -32601, "message": "not found"}}])
    with pytest.raises(RuntimeError, match=r"CDP Network\.setCookie failed"):
        await sess._cdp_call(ws, "Network.setCookie", {})


async def test_inject_raises_when_chrome_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    async def boom(url: str) -> dict[str, Any]:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(sess, "_http_json", boom)
    with pytest.raises(httpx.ConnectError):
        await sess.inject_session_cookie(9222, GATEWAY, SECRET)


# --- daemon wiring ---


async def test_inject_once_skips_when_gateway_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_missing() -> str:
        raise RuntimeError("gateway_url unset")

    calls: list[tuple[int, str, str]] = []

    async def fake_inject(port: int, url: str, secret: str) -> None:
        calls.append((port, url, secret))

    monkeypatch.setattr(mcp_daemon, "gateway_api_base", raise_missing)
    monkeypatch.setattr(mcp_daemon, "inject_session_cookie", fake_inject)
    await mcp_daemon._inject_gateway_session_once()  # must not raise
    assert calls == []


async def test_inject_once_skips_on_empty_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, str, str]] = []

    async def fake_inject(port: int, url: str, secret: str) -> None:
        calls.append((port, url, secret))

    monkeypatch.setattr(mcp_daemon, "gateway_api_base", lambda: GATEWAY)
    monkeypatch.setattr(mcp_daemon, "inject_session_cookie", fake_inject)
    monkeypatch.setattr(mcp_daemon.settings.data_plane, "cluster_secret", "")
    await mcp_daemon._inject_gateway_session_once()
    assert calls == []


async def test_inject_once_injects_with_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, str, str]] = []

    async def fake_inject(port: int, url: str, secret: str) -> None:
        calls.append((port, url, secret))

    monkeypatch.setattr(mcp_daemon, "gateway_api_base", lambda: GATEWAY)
    monkeypatch.setattr(mcp_daemon, "inject_session_cookie", fake_inject)
    monkeypatch.setattr(mcp_daemon.settings.data_plane, "cluster_secret", SECRET)
    await mcp_daemon._inject_gateway_session_once()
    assert calls == [(mcp_daemon.settings.services.browser_cdp_port, GATEWAY, SECRET)]


async def test_inject_once_swallows_injection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(port: int, url: str, secret: str) -> None:
        raise RuntimeError("cdp down")

    monkeypatch.setattr(mcp_daemon, "gateway_api_base", lambda: GATEWAY)
    monkeypatch.setattr(mcp_daemon, "inject_session_cookie", boom)
    monkeypatch.setattr(mcp_daemon.settings.data_plane, "cluster_secret", SECRET)
    await mcp_daemon._inject_gateway_session_once()  # must not raise


async def test_session_loop_injects_then_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_inject_once() -> None:
        calls.append("inject")

    async def stop_after_first(stop: asyncio.Event, timeout: float) -> None:
        stop.set()

    monkeypatch.setattr(mcp_daemon, "_inject_gateway_session_once", fake_inject_once)
    monkeypatch.setattr(mcp_daemon, "_await_stop_or_timeout", stop_after_first)
    await mcp_daemon._gateway_session_loop(asyncio.Event())
    assert calls == ["inject"]


async def test_spawn_inject_tracks_task(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_inject_once() -> None:
        pass

    monkeypatch.setattr(mcp_daemon, "_inject_gateway_session_once", fake_inject_once)
    mcp_daemon._inject_tasks.clear()
    mcp_daemon._spawn_inject()
    assert len(mcp_daemon._inject_tasks) == 1
    # let the task finish so its done-callback clears the set
    await asyncio.gather(*mcp_daemon._inject_tasks)
    assert mcp_daemon._inject_tasks == set()
