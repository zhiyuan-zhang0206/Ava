"""Log in and inject a gateway session cookie into the shared managed Chrome.

The gateway reverse-proxies agent-served pages under auth (session cookie or
Bearer token), so the managed browser needs its own ``ava_session`` cookie to
open page URLs. This module obtains an opaque server-side session through the
gateway's normal login endpoint, then injects that returned cookie over CDP.

Injection path: ``Network.setCookie`` on a page target's websocket. Chrome
only exposes the Network domain on page targets (the browser-level target
answers ``-32601``), so we create a throwaway ``about:blank`` tab through the
browser-level websocket, set the cookie on it, and close it again. The cookie
is host-only for the configured gateway URL (no Domain attribute, matching the
login cookie), so it is never sent to any other host.

Runs inside the browser-mcp daemon process, which shares the machine with
Chrome and the cluster secret.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from contextlib import suppress
from http.cookies import SimpleCookie
from typing import Any

import httpx
import websockets

from shared.cluster_auth import MANAGED_BROWSER_USER_AGENT, cookie_name

# CDP endpoint timeouts: Chrome is local, so these only bound a wedged browser.
_HTTP_TIMEOUT_S = 5.0
_WS_TIMEOUT_S = 10.0

# The cookie most recently handed to Chrome, as (name, value). The daemon's
# early-refresh path checks it against the gateway after a gateway-URL
# navigation, so a revoked or expired managed session heals immediately instead
# of waiting out the next scheduled refresh tick. A one-element list keeps the
# slot mutable without a `global` statement in the injector.
_last_injected_cookie: list[tuple[str, str] | None] = [None]

# IDs are per-connection in practice, but a monotonically increasing global id
# is valid on any connection too and keeps the helper stateless.
_CDP_IDS = itertools.count(1)


async def gateway_session_login(gateway_url: str, secret: str) -> tuple[str, str, int]:
    """Log in and return ``(cookie_name, value, expires_unix)``.

    The login carries the managed-browser user-agent marker, so the gateway's
    sessions list can label this session row as managed Chrome.
    """
    url = f"{gateway_url.rstrip('/')}/api/auth/login"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
        response = await client.post(
            url,
            json={"password": secret},
            headers={"User-Agent": MANAGED_BROWSER_USER_AGENT},
        )
    if response.status_code != 200:
        raise RuntimeError(f"gateway login failed with HTTP {response.status_code}")

    parsed = SimpleCookie()
    set_cookie = response.headers.get("set-cookie")
    if set_cookie:
        parsed.load(set_cookie)
    name = cookie_name()
    morsel = parsed.get(name)
    if morsel is None or not morsel.value:
        raise RuntimeError("gateway login response has no session cookie")
    try:
        max_age = int(morsel["max-age"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("gateway session cookie has invalid Max-Age") from exc
    if max_age <= 0:
        raise RuntimeError("gateway session cookie has invalid Max-Age")
    return name, morsel.value, int(time.time()) + max_age


async def _http_json(url: str) -> Any:
    """GET ``url`` and return the parsed JSON body."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def _cdp_call(ws: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Send one CDP request on an open websocket and return its ``result``.

    Raises RuntimeError on a protocol-level error response.
    """
    await ws.send(json.dumps({"id": next(_CDP_IDS), "method": method, "params": params}))
    raw = await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT_S)
    resp = json.loads(raw)
    if "error" in resp:
        raise RuntimeError(f"CDP {method} failed: {resp['error']}")
    return resp.get("result") or {}


async def inject_session_cookie(cdp_port: int, gateway_url: str, secret: str) -> None:
    """Log in to the gateway and set its session cookie in managed Chrome.

    The cookie is host-only for ``gateway_url`` with the same flags the login
    flow sets (HttpOnly, SameSite=Lax, Path=/, expiry from Max-Age).

    Raises RuntimeError when Chrome is unreachable or rejects the cookie.
    """
    name, value, expires = await gateway_session_login(gateway_url, secret)
    version = await _http_json(f"http://127.0.0.1:{cdp_port}/json/version")
    browser_ws_url = version["webSocketDebuggerUrl"]

    async with websockets.connect(browser_ws_url) as browser_ws:
        # A page target is required for the Network domain; use a throwaway
        # about:blank tab so we never depend on (or disturb) anyone's tabs.
        created = await _cdp_call(browser_ws, "Target.createTarget", {"url": "about:blank"})
        target_id = created["targetId"]
        try:
            page_ws_url = f"ws://127.0.0.1:{cdp_port}/devtools/page/{target_id}"
            async with websockets.connect(page_ws_url) as page_ws:
                result = await _cdp_call(
                    page_ws,
                    "Network.setCookie",
                    {
                        "name": name,
                        "value": value,
                        "url": gateway_url.rstrip("/") + "/",
                        "path": "/",
                        "httpOnly": True,
                        "sameSite": "Lax",
                        "expires": expires,
                    },
                )
        finally:
            with suppress(Exception):
                await _cdp_call(browser_ws, "Target.closeTarget", {"targetId": target_id})

    if not result.get("success"):
        raise RuntimeError(f"Chrome rejected the gateway session cookie: {result}")
    _last_injected_cookie[0] = (name, value)


def last_injected_cookie() -> tuple[str, str] | None:
    """The ``(name, value)`` of the most recently injected gateway cookie, or None."""
    return _last_injected_cookie[0]


async def gateway_session_is_valid(gateway_url: str, cookie_value: str) -> bool:
    """Whether the gateway still accepts ``cookie_value`` as a live session.

    Calls the auth-check endpoint (itself exempt from auth) with the cookie.
    Any non-200 answer or an unauthenticated body counts as invalid; network
    errors propagate to the caller, which retries on its next trigger.
    """
    url = f"{gateway_url.rstrip('/')}/api/auth/check"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
        response = await client.get(
            url,
            headers={"Cookie": f"{cookie_name()}={cookie_value}"},
        )
    if response.status_code != 200:
        return False
    return bool(response.json().get("authenticated"))
