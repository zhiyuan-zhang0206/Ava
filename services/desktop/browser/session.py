"""Log in and inject a gateway session cookie into the shared managed Chrome.

The gateway reverse-proxies agent-served pages under auth (session cookie or
Bearer token), so the managed browser needs its own ``meiva_session`` cookie to
open page URLs. This module obtains an opaque server-side session through the
gateway's normal login endpoint, then injects that returned cookie over CDP.

Injection path: ``Network.setCookie`` on a page target's websocket. Chrome
only exposes the Network domain on page targets (the browser-level target
answers ``-32601``), so we create a throwaway ``about:blank`` tab through the
browser-level websocket, set the cookie on it, and close it again. The cookie
is host-only for the configured gateway URL (no Domain attribute, matching the
login cookie), so it is never sent to any other host.

Runs inside the browser-mcp daemon process, which shares the machine with
Chrome and holds its launch-delivered runner API token (the login credential).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections.abc import Iterator
from http.cookies import SimpleCookie
from typing import Any

import httpx
import websockets

from base.host.net.auth import MANAGED_BROWSER_USER_AGENT, cookie_name
from base.log import logger

# CDP endpoint timeouts: Chrome is local, so these only bound a wedged browser.
_HTTP_TIMEOUT_S = 5.0
_WS_TIMEOUT_S = 10.0


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


async def http_json(url: str) -> Any:
    """GET ``url`` and return the parsed JSON body."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def cdp_call(
    ws: Any,
    ids: Iterator[int],
    method: str,
    params: dict[str, Any],
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Send one CDP request on an open websocket and return its ``result``.

    ``session_id`` addresses a flattened target session on the browser-level
    websocket. Messages that are not the reply to this request (CDP events, or
    a reply on another session's traffic) are skipped.

    Raises RuntimeError on a protocol-level error response.
    """
    request_id = next(ids)
    request: dict[str, Any] = {"id": request_id, "method": method, "params": params}
    if session_id is not None:
        request["sessionId"] = session_id
    await ws.send(json.dumps(request))
    # quiesce-exempt: bounded wait for one CDP reply to a local browser; no database
    while True:
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT_S))
        if resp.get("id") == request_id:
            break
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
    version = await http_json(f"http://127.0.0.1:{cdp_port}/json/version")
    browser_ws_url = version["webSocketDebuggerUrl"]

    ids = itertools.count(1)  # request ids are per connection
    async with websockets.connect(browser_ws_url) as browser_ws:
        # A page target is required for the Network domain; use a throwaway
        # about:blank tab so we never depend on (or disturb) anyone's tabs.
        created = await cdp_call(browser_ws, ids, "Target.createTarget", {"url": "about:blank"})
        target_id = created["targetId"]
        try:
            page_ws_url = f"ws://127.0.0.1:{cdp_port}/devtools/page/{target_id}"
            async with websockets.connect(page_ws_url) as page_ws:
                result = await cdp_call(
                    page_ws,
                    ids,
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
            try:
                await cdp_call(browser_ws, ids, "Target.closeTarget", {"targetId": target_id})
            except Exception:
                logger.opt(exception=True).warning(
                    "[browser-mcp] could not close the cookie-injection tab {}; "
                    "it stays open in the shared Chrome",
                    target_id,
                )

    if not result.get("success"):
        raise RuntimeError(f"Chrome rejected the gateway session cookie: {result}")
