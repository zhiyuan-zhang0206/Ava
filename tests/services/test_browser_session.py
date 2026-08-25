"""Unit tests for services/browser.session — the CDP gateway-session injector.

The CDP and HTTP surfaces are faked; nothing here dials a real browser. Also
covers the browser-mcp daemon's injection wiring (best-effort guards, refresh
loop), which shares this module's file for proximity.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from services.browser import mcp_daemon
from services.browser import session as sess

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


def _install_fake_gateway_login(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_login(gateway_url: str, secret: str) -> tuple[str, str, int]:
        assert gateway_url == GATEWAY
        assert secret == SECRET
        return "ava_session", "opaque-session", 1_800_000_000

    monkeypatch.setattr(sess, "gateway_session_login", fake_login)


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

    _install_fake_gateway_login(monkeypatch)
    monkeypatch.setattr(sess, "_http_json", fake_http_json)
    monkeypatch.setattr(sess.websockets, "connect", fake_connect)
    return {"browser": browser_ws, "page": page_ws}


def _install_login_response(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_code: int = 200,
    set_cookie: str | None = "ava_session=opaque-session; HttpOnly; Max-Age=86400; Path=/",
) -> list[tuple[str, dict[str, str], dict[str, str]]]:
    calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            assert timeout == 5.0

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def post(
            self, url: str, *, json: dict[str, str], headers: dict[str, str]
        ) -> httpx.Response:
            calls.append((url, json, headers))
            headers = {"Set-Cookie": set_cookie} if set_cookie is not None else {}
            return httpx.Response(status_code, headers=headers)

    monkeypatch.setattr(sess.httpx, "AsyncClient", FakeClient)
    return calls


async def test_gateway_session_login_returns_server_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_login_response(monkeypatch)
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)

    result = await sess.gateway_session_login(GATEWAY, SECRET)

    assert result == ("ava_session", "opaque-session", 1_700_086_400)
    url, body, headers = calls[0]
    assert (url, body) == (f"{GATEWAY}/api/auth/login", {"password": SECRET})
    # the managed-browser marker lets the sessions list label this row
    assert headers == {"User-Agent": sess.MANAGED_BROWSER_USER_AGENT}


async def test_gateway_session_login_rejects_non_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_login_response(monkeypatch, status_code=401)

    with pytest.raises(RuntimeError, match="HTTP 401"):
        await sess.gateway_session_login(GATEWAY, SECRET)


@pytest.mark.parametrize(
    "set_cookie",
    [
        None,
        "other=value; Max-Age=86400",
        "ava_session=opaque-session; Path=/",
        "ava_session=opaque-session; Max-Age=invalid",
    ],
)
async def test_gateway_session_login_rejects_malformed_cookie(
    monkeypatch: pytest.MonkeyPatch,
    set_cookie: str | None,
) -> None:
    _install_login_response(monkeypatch, set_cookie=set_cookie)

    with pytest.raises(RuntimeError, match="session cookie"):
        await sess.gateway_session_login(GATEWAY, SECRET)


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
    assert params["value"] == "opaque-session"
    assert params["expires"] == 1_800_000_000


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
    async def boom(url: str) -> dict[str, Any]:
        raise httpx.ConnectError("connection refused")

    _install_fake_gateway_login(monkeypatch)
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


async def test_gateway_session_is_valid_accepts_authenticated_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            assert timeout == 5.0

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
            calls.append((url, headers))
            return httpx.Response(200, json={"authenticated": True})

    monkeypatch.setattr(sess.httpx, "AsyncClient", FakeClient)

    assert await sess.gateway_session_is_valid(GATEWAY, "opaque-session") is True
    assert calls == [
        (
            f"{GATEWAY}/api/auth/check",
            {"Cookie": f"{sess.cookie_name()}=opaque-session"},
        )
    ]


async def test_gateway_session_is_valid_rejects_unauthenticated_or_non_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [
        httpx.Response(200, json={"authenticated": False}),
        httpx.Response(500, text="boom"),
        httpx.Response(200, json={}),
    ]

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
            return answers.pop(0)

    monkeypatch.setattr(sess.httpx, "AsyncClient", FakeClient)

    for expected in (False, False, False):
        assert await sess.gateway_session_is_valid(GATEWAY, "opaque-session") is expected


async def test_inject_records_last_injected_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    sess._last_injected_cookie[0] = None
    try:
        _install_fake_cdp(monkeypatch)
        await sess.inject_session_cookie(9222, GATEWAY, SECRET)

        assert sess.last_injected_cookie() == ("ava_session", "opaque-session")
    finally:
        sess._last_injected_cookie[0] = None


# --- daemon early-refresh wiring ---


async def test_verify_once_injects_when_cookie_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injected: list[tuple[int, str, str]] = []

    async def fake_inject(port: int, url: str, secret: str) -> None:
        injected.append((port, url, secret))

    monkeypatch.setattr(mcp_daemon, "gateway_api_base", lambda: GATEWAY)
    monkeypatch.setattr(mcp_daemon.settings.data_plane, "cluster_secret", SECRET)
    monkeypatch.setattr(mcp_daemon, "inject_session_cookie", fake_inject)
    monkeypatch.setattr(mcp_daemon, "last_injected_cookie", lambda: ("ava_session", "stale"))
    monkeypatch.setattr(mcp_daemon, "gateway_session_is_valid", _async_false)

    await mcp_daemon._verify_gateway_session_once()
    assert injected == [(mcp_daemon.settings.services.browser_cdp_port, GATEWAY, SECRET)]


async def test_verify_once_skips_injection_when_cookie_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injected: list[tuple[int, str, str]] = []

    async def fake_inject(port: int, url: str, secret: str) -> None:
        injected.append((port, url, secret))

    monkeypatch.setattr(mcp_daemon, "gateway_api_base", lambda: GATEWAY)
    monkeypatch.setattr(mcp_daemon.settings.data_plane, "cluster_secret", SECRET)
    monkeypatch.setattr(mcp_daemon, "inject_session_cookie", fake_inject)
    monkeypatch.setattr(mcp_daemon, "last_injected_cookie", lambda: ("ava_session", "fresh"))
    monkeypatch.setattr(mcp_daemon, "gateway_session_is_valid", _async_true)

    await mcp_daemon._verify_gateway_session_once()
    assert injected == []


async def test_verify_once_injects_when_no_cookie_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injected: list[tuple[int, str, str]] = []

    async def fake_inject(port: int, url: str, secret: str) -> None:
        injected.append((port, url, secret))

    monkeypatch.setattr(mcp_daemon, "gateway_api_base", lambda: GATEWAY)
    monkeypatch.setattr(mcp_daemon.settings.data_plane, "cluster_secret", SECRET)
    monkeypatch.setattr(mcp_daemon, "inject_session_cookie", fake_inject)
    monkeypatch.setattr(mcp_daemon, "last_injected_cookie", lambda: None)

    await mcp_daemon._verify_gateway_session_once()
    assert injected == [(mcp_daemon.settings.services.browser_cdp_port, GATEWAY, SECRET)]


async def test_verify_once_swallows_check_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injected: list[tuple[int, str, str]] = []

    async def fake_inject(port: int, url: str, secret: str) -> None:
        injected.append((port, url, secret))

    async def boom(url: str, value: str) -> bool:
        raise RuntimeError("gateway down")

    monkeypatch.setattr(mcp_daemon, "gateway_api_base", lambda: GATEWAY)
    monkeypatch.setattr(mcp_daemon.settings.data_plane, "cluster_secret", SECRET)
    monkeypatch.setattr(mcp_daemon, "inject_session_cookie", fake_inject)
    monkeypatch.setattr(mcp_daemon, "last_injected_cookie", lambda: ("ava_session", "stale"))
    monkeypatch.setattr(mcp_daemon, "gateway_session_is_valid", boom)

    await mcp_daemon._verify_gateway_session_once()  # must not raise
    assert injected == []


async def test_verify_once_skips_when_gateway_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_missing() -> str:
        raise RuntimeError("gateway_url unset")

    monkeypatch.setattr(mcp_daemon, "gateway_api_base", raise_missing)
    await mcp_daemon._verify_gateway_session_once()  # must not raise


async def test_spawn_verify_tracks_task(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_verify_once() -> None:
        pass

    monkeypatch.setattr(mcp_daemon, "_verify_gateway_session_once", fake_verify_once)
    mcp_daemon._verify_tasks.clear()
    mcp_daemon._spawn_verify()
    assert len(mcp_daemon._verify_tasks) == 1
    await asyncio.gather(*mcp_daemon._verify_tasks)
    assert mcp_daemon._verify_tasks == set()


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("navigate_page", {"url": f"{GATEWAY}/some/page"}, True),
        ("new_page", {"url": f"{GATEWAY}/"}, True),
        ("navigate_page", {"url": "http://other.example/page"}, False),
        ("navigate_page", {"url": "https://gateway.example:9999/page"}, False),
        ("new_page", {"url": "about:blank"}, False),
        ("navigate_page", {"type": "reload"}, False),
        ("take_snapshot", {}, False),
        ("new_page", {"url": 42}, False),
    ],
)
def test_navigates_to_gateway(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: dict[str, Any],
    expected: bool,
) -> None:
    monkeypatch.setattr(mcp_daemon, "gateway_api_base", lambda: GATEWAY)
    assert mcp_daemon._navigates_to_gateway(name, args) is expected


def test_navigates_to_gateway_false_when_gateway_base_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_missing() -> str:
        raise RuntimeError("gateway_url unset")

    monkeypatch.setattr(mcp_daemon, "gateway_api_base", raise_missing)
    assert mcp_daemon._navigates_to_gateway("navigate_page", {"url": f"{GATEWAY}/x"}) is False


async def _async_true(url: str, value: str) -> bool:
    return True


async def _async_false(url: str, value: str) -> bool:
    return False
